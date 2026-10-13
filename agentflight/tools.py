"""Tools: functions and MCP servers that agents can call.

    @tool
    def weather(city: str) -> str:
        \"\"\"Returns the weather for a city.\"\"\"
        ...

    class Assistant(Agent):
        tools = [weather, *mcp_server("npx -y @modelcontextprotocol/server-filesystem .")]

The tool schema is built from the function signature; when the model calls a tool, the arguments are
validated against this schema.
"""

from __future__ import annotations

import atexit
import inspect
import itertools
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import typing

from .errors import AgentError
from .schema import Schema, convert, json_schema, to_jsonable

RESULT_LIMIT = 20000


def _param_descriptions(doc: str) -> dict[str, str]:
    descriptions = {}
    for line in doc.splitlines():
        m = re.match(r"^\s*[-*]?\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)$", line)
        if m:
            descriptions[m.group(1)] = m.group(2).strip()
    return descriptions


def result_text(value) -> str:
    if value is None:
        return "ok"
    if isinstance(value, str):
        text = value
    elif isinstance(value, Schema):
        text = value.to_json()
    else:
        try:
            text = json.dumps(to_jsonable(value), ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > RESULT_LIMIT:
        text = text[:RESULT_LIMIT] + f"\n… (result truncated by {len(text) - RESULT_LIMIT} characters)"
    return text


class Tool:
    """A tool the model can call."""

    def __init__(self, name: str, description: str, schema: dict, runner, fn=None, source: str = "python"):
        self.name = name
        self.description = description
        self.schema = schema
        self._runner = runner
        self.fn = fn
        self.source = source
        if fn is not None:
            self.__wrapped__ = fn
            self.__name__ = getattr(fn, "__name__", name)
            self.__doc__ = getattr(fn, "__doc__", None)

    def __call__(self, *args, **kwargs):
        if self.fn is None:
            if args:
                raise TypeError(f"{self.name} takes keyword arguments only")
            return self._runner(kwargs)
        return self.fn(*args, **kwargs)

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "schema": self.schema}

    def run(self, arguments: dict) -> str:
        return result_text(self._runner(arguments))

    def __repr__(self):
        return f"<tool {self.name}>"


def _from_function(fn, name=None, description=None) -> Tool:
    signature = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - unresolvable annotations: fall back to the raw ones
        hints = getattr(fn, "__annotations__", {})
    doc = inspect.getdoc(fn) or ""
    first_paragraph = doc.split("\n\n")[0].strip()
    param_descriptions = _param_descriptions(doc)
    properties, required, types = {}, [], {}
    for p in signature.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        tp = hints.get(p.name, str)
        types[p.name] = tp
        s = dict(json_schema(tp, strict=True))
        if p.name in param_descriptions:
            s["description"] = param_descriptions[p.name]
        properties[p.name] = s
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
    schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def runner(arguments: dict):
        if "__invalid_json__" in arguments:
            raise AgentError("tool arguments are not valid JSON")
        errors: list[str] = []
        converted = {}
        for key, value in arguments.items():
            if key not in types:
                errors.append(f"unknown argument: {key}")
                continue
            converted[key] = convert(types[key], value, key, errors)
        for needed in required:
            if needed not in arguments:
                errors.append(f"{needed}: required argument missing")
        if errors:
            raise AgentError("invalid arguments: " + "; ".join(errors))
        return fn(**converted)

    return Tool(name or fn.__name__, description or first_paragraph or fn.__name__, schema, runner, fn=fn)


def tool(fn=None, *, name: str | None = None, description: str | None = None):
    """Turns a function into a tool the model can call. The schema comes from the signature and the
    description from the docstring (parameter lines like `city: the city name` describe arguments).

        @tool
        def weather(city: str) -> str:
            \"\"\"Returns the weather for a city.\"\"\"
    """
    if fn is None:
        return lambda f: _from_function(f, name, description)
    if isinstance(fn, Tool):
        return fn
    return _from_function(fn, name, description)


def to_tools(obj) -> list[Tool]:
    if isinstance(obj, Tool):
        return [obj]
    if isinstance(obj, (list, tuple)):
        result = []
        for o in obj:
            result.extend(to_tools(o))
        return result
    if callable(obj):
        return [_from_function(obj)]
    raise AgentError(f"cannot be used as a tool: {obj!r}")


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) stdio client

class _McpClient:
    def __init__(self, command, env=None, cwd=None):
        if isinstance(command, str):
            command = shlex.split(command, posix=os.name != "nt")
        self.command = command
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **(env or {})},
            cwd=cwd, creationflags=flags,
            shell=os.name == "nt" and not command[0].lower().endswith((".exe", "python")),
        )
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        atexit.register(self.close)
        self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "agentflight", "version": "0.1"},
        })
        self.notify("notifications/initialized")

    def _read(self):
        for raw in self.process.stdout:
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            msg_id = message.get("id")
            if msg_id is not None and msg_id in self._pending:
                self._pending[msg_id].put(message)
        for k in list(self._pending):
            self._pending[k].put({"error": {"message": "the MCP server exited"}})

    def _send(self, data):
        line = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self.process.stdin.write(line)
            self.process.stdin.flush()

    def notify(self, method, params=None):
        data = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            data["params"] = params
        self._send(data)

    def request(self, method, params=None, timeout=60):
        msg_id = next(self._ids)
        replies: queue.Queue = queue.Queue()
        self._pending[msg_id] = replies
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        try:
            message = replies.get(timeout=timeout)
        except queue.Empty:
            raise AgentError(f"the MCP server did not respond: {method}") from None
        finally:
            self._pending.pop(msg_id, None)
        if "error" in message:
            raise AgentError(f"MCP error ({method}): {message['error'].get('message', message['error'])}")
        return message.get("result", {})

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.terminate()
        except Exception:  # noqa: BLE001 - best effort at exit
            pass


def mcp_server(command, *, env: dict | None = None, cwd: str | None = None, prefix: str = "") -> list[Tool]:
    """Starts an MCP server (stdio) and returns its tools as agent tools.

        tools = mcp_server("npx -y @modelcontextprotocol/server-filesystem .")
    """
    client = _McpClient(command, env, cwd)
    tools = []
    cursor = None
    while True:
        result = client.request("tools/list", {"cursor": cursor} if cursor else {})
        for t in result.get("tools", []):
            name = t["name"]

            def runner(arguments, _name=name):
                reply = client.request("tools/call", {"name": _name, "arguments": arguments}, timeout=300)
                parts = []
                for p in reply.get("content", []):
                    if p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    else:
                        parts.append(json.dumps(p, ensure_ascii=False))
                text = "\n".join(parts)
                if reply.get("isError"):
                    raise AgentError(text or "the MCP tool returned an error")
                return text

            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            tools.append(Tool(prefix + name, t.get("description") or name, schema, runner, source="mcp"))
        cursor = result.get("nextCursor")
        if not cursor:
            break
    return tools


__all__ = ["Tool", "tool", "to_tools", "mcp_server", "result_text"]
