"""Provider that runs Claude models through the user's own Claude Code.

For every agent call the unmodified `claude` program is started in print mode (`-p`, stream-json);
it signs in with Claude Code's own session (the user's Claude subscription). Claude Code's
built-in tools are turned off; the agent's tools are given through a local MCP bridge, so Claude
Code runs the tool loop but every tool call runs in AgentFlight (live watching, approvals). The return
type is requested with `--json-schema`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ..errors import MaxTurnsExceeded, ProviderError
from .base import Provider, Request, Response

PREFIX = "claude-code/"
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _work_dir() -> Path:
    from ..config import settings_dir
    d = settings_dir() / "claude-work"
    d.mkdir(parents=True, exist_ok=True)
    return d


class ClaudeCodeProvider(Provider):
    name = "claude_code"
    session_based = True   # the provider runs the tool loop (see the session loop in agents.py)
    subscription = True    # billed to the user's subscription; not counted in the AgentFlight budget

    def is_ready(self):
        from ..accounts import claude_code
        s = claude_code.status()
        if not s["installed"]:
            return False, "Claude Code is not installed (Settings › Accounts › Claude)"
        if not s["signed_in"]:
            return False, "not signed in to Claude Code (Settings › Accounts › Claude › Connect)"
        return True, ""

    def open_session(self, request: Request, tool_server, max_turns: int) -> ClaudeCodeSession:
        return ClaudeCodeSession(request, tool_server, max_turns)

    def call(self, request: Request, on_text) -> Response:
        """A single call without tools (e.g. the eval judge)."""
        session = ClaudeCodeSession(request, None, 4)
        try:
            last = next((m for m in reversed(request.messages) if m["role"] == "user"), {"content": ""})
            return session.send(last["content"], on_text, None)
        finally:
            session.close()


class ClaudeCodeSession:
    """A Claude Code process that stays open for the duration of one agent call."""

    def __init__(self, request: Request, tool_server, max_turns: int):
        from ..accounts import claude_code

        exe = claude_code.executable()
        if not exe:
            raise ProviderError("Claude Code was not found. Install and connect it in Settings › Accounts › Claude.")
        self.request = request
        self.server = tool_server
        self.model = request.model[len(PREFIX):] if request.model.startswith(PREFIX) else request.model
        self._temp = Path(tempfile.mkdtemp(prefix="agentflight-claude-"))
        system_file = self._temp / "system.md"
        system_file.write_text(request.system or "", encoding="utf-8")
        args = [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--model", self.model, "--system-prompt-file", str(system_file),
            "--tools", "", "--setting-sources", "project", "--disable-slash-commands",
            "--no-session-persistence", "--max-turns", str(max(1, int(max_turns))),
        ]
        if request.effort in _EFFORTS:
            args += ["--effort", request.effort]
        if request.schema is not None:
            args += ["--json-schema", json.dumps(request.schema, ensure_ascii=False)]
        if tool_server is not None:
            mcp_file = self._temp / "mcp.json"
            mcp_file.write_text(json.dumps(tool_server.mcp_config()), encoding="utf-8")
            args += ["--strict-mcp-config", "--mcp-config", str(mcp_file),
                     "--allowedTools", f"mcp__{tool_server.SERVER_NAME}"]
        else:
            args += ["--strict-mcp-config"]
        env = claude_code.env(subscription=True)
        env["MCP_TOOL_TIMEOUT"] = str(24 * 3600 * 1000)  # tools may wait for an approval
        self._stderr_lines: list[str] = []
        try:
            self.process = subprocess.Popen(
                claude_code.command(exe, *args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=str(_work_dir()), env=env,
                creationflags=claude_code.NO_WINDOW,
            )
        except OSError as e:
            self._cleanup()
            raise ProviderError(f"could not start Claude Code: {e}") from e
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()
        self.session_id = ""
        self.rate_limit_info: dict = {}

    def _read_stderr(self):
        for raw in self.process.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr_lines = (self._stderr_lines + [line])[-40:]

    def _write(self, text: str) -> None:
        message = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except OSError as e:
            raise ProviderError("could not write to Claude Code: " + str(self._stderr_summary() or e)) from e

    def _stderr_summary(self) -> str:
        return " | ".join(self._stderr_lines[-4:])[:600]

    def send(self, text: str, on_text, hooks) -> Response:
        """Sends one user message and reads the stream until Claude Code has finished with it."""
        self._write(text)
        texts: list[str] = []
        transcript: list[dict] = []
        input_tokens = output_tokens = 0
        turn_input = turn_output = 0
        stop_reason = ""
        result = None
        for raw in self.process.stdout:
            try:
                o = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            kind = o.get("type")
            if kind == "stream_event":
                e = o.get("event") or {}
                et = e.get("type")
                if et == "message_start":
                    usage = (e.get("message") or {}).get("usage") or {}
                    turn_input = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) \
                        + (usage.get("cache_read_input_tokens") or 0)
                    turn_output = 0
                    texts = []
                    if hooks is not None:
                        hooks.turn_started()
                elif et == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        texts.append(d["text"])
                        on_text(d["text"])
                elif et == "message_delta":
                    turn_output = ((e.get("usage") or {}).get("output_tokens") or turn_output)
                    stop_reason = ((e.get("delta") or {}).get("stop_reason") or stop_reason)
                elif et == "message_stop":
                    input_tokens += turn_input
                    output_tokens += turn_output
                    if hooks is not None:
                        hooks.turn_finished(turn_input, turn_output, stop_reason)
            elif kind == "assistant":
                content = (o.get("message") or {}).get("content") or []
                written = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                calls = []
                for b in content:
                    if b.get("type") != "tool_use":
                        continue
                    name = self.server.tool_name(b.get("name", "")) if self.server else None
                    if name is None and b.get("name") == "StructuredOutput":
                        continue
                    calls.append({"id": b.get("id", ""), "name": name or b.get("name", ""),
                                  "arguments": b.get("input") or {}})
                if written or calls:
                    transcript.append({"role": "assistant", "content": written, "tool_calls": calls})
            elif kind == "user":
                content = (o.get("message") or {}).get("content")
                results = []
                for b in content if isinstance(content, list) else []:
                    if b.get("type") != "tool_result":
                        continue
                    c = b.get("content")
                    written = c if isinstance(c, str) else "".join(
                        x.get("text", "") for x in (c or []) if isinstance(x, dict))
                    if written == "Structured output provided successfully":
                        continue
                    results.append({"id": b.get("tool_use_id", ""), "name": "", "content": written[:4000],
                                    "error": bool(b.get("is_error"))})
                if results:
                    transcript.append({"role": "tool", "results": results})
            elif kind == "system" and o.get("subtype") == "init":
                self.session_id = o.get("session_id", "")
                if self.server is not None:
                    statuses = {s.get("name"): s.get("status") for s in o.get("mcp_servers") or []}
                    if statuses.get(self.server.SERVER_NAME) not in ("connected", None):
                        raise ProviderError("Claude Code could not connect to the agent's tool bridge "
                                            + f"({statuses.get(self.server.SERVER_NAME)})")
            elif kind == "rate_limit_event":
                self.rate_limit_info = o.get("rate_limit_info") or {}
            elif kind == "result":
                result = o
                break
        if result is None:
            code = self.process.poll()
            raise ProviderError(f"Claude Code exited unexpectedly (exit {code}). " + self._stderr_summary())
        return self._parse_result(result, texts, transcript, input_tokens, output_tokens, stop_reason)

    def _parse_result(self, o: dict, texts, transcript, input_tokens, output_tokens, stop_reason) -> Response:
        subtype = o.get("subtype", "")
        if subtype == "error_max_turns":
            raise MaxTurnsExceeded("Claude Code reached the turn limit (max_turns)")
        if o.get("is_error") or subtype.startswith("error"):
            message = str(o.get("result") or subtype or "unknown error")
            status = o.get("api_error_status")
            lower = message.lower()
            if status == 401 or any(s in lower for s in ("login", "not logged", "invalid api key")):
                err = ProviderError("Not signed in to Claude Code, or the session expired. "
                                    "Settings › Accounts › Claude › Connect.")
                err.status = 401
                raise err
            if self.rate_limit_info.get("status") == "rejected" or status == 429 or "limit" in lower:
                err = ProviderError("You reached your Claude subscription's usage limit: " + message[:200])
                err.status = 429
                raise err
            err = ProviderError("Claude Code error: " + message[:400])
            err.status = status
            raise err
        if o.get("structured_output") is not None:
            text = json.dumps(o["structured_output"], ensure_ascii=False)
        else:
            text = str(o.get("result") or "".join(texts))
        usage = o.get("usage") or {}
        if not input_tokens and usage:
            input_tokens = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) \
                + (usage.get("cache_read_input_tokens") or 0)
            output_tokens = usage.get("output_tokens") or 0
        return Response(text=text, raw=transcript, input_tokens=input_tokens, output_tokens=output_tokens,
                        stop_reason=o.get("stop_reason") or stop_reason, model=self.model)

    def close(self) -> None:
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        except Exception:  # noqa: BLE001
            pass
        self._stderr_reader.join(timeout=2)
        for pipe in (self.process.stdout, self.process.stderr):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except OSError:
                pass
        self._cleanup()

    def stop(self) -> None:
        try:
            self.process.kill()
        except Exception:  # noqa: BLE001
            pass

    def _cleanup(self):
        shutil.rmtree(self._temp, ignore_errors=True)
