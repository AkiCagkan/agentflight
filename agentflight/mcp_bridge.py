"""Serves AgentFlight tools as a local MCP server (Streamable HTTP, 127.0.0.1 only).

For agents that run on Claude Code, Claude Code drives the tool loop; its tool calls come back to the AgentFlight
process through this server and run the usual way (live watching, approval, stopping). The server is protected
by a random token and only accepts its own Host header.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler

from .localhttp import LocalHTTPServer

# MCP tool names must be ASCII: transliterate the accented letters users are likely to type.
_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜâîûÂÎÛ", "cgiosuCGIOSUaiuAIU")


def mcp_name(name: str, used: set[str]) -> str:
    """Fits a tool name to the MCP/API naming rule (^[a-zA-Z0-9_-]{1,64}$)."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", name.translate(_ASCII))[:64] or "tool"
    candidate, i = clean, 2
    while candidate in used:
        candidate = f"{clean[:60]}_{i}"
        i += 1
    used.add(candidate)
    return candidate


class _QuietServer(LocalHTTPServer):

    def handle_error(self, request, client_address):
        pass  # connections dropped while the client (Claude Code) shuts down are normal


class ToolServer:
    """Serves tools through a `run_tool(name, arguments) -> (text, is_error)` callback."""

    SERVER_NAME = "agentflight"

    def __init__(self, tools: list, run_tool):
        self._run_tool = run_tool
        used: set[str] = set()
        self.name_map: dict[str, str] = {}   # MCP name -> AgentFlight tool name
        self.definitions = []
        for t in tools:
            name = mcp_name(t.name, used)
            self.name_map[name] = t.name
            self.definitions.append({"name": name, "description": t.description or t.name, "inputSchema": t.schema})
        self.token = secrets.token_urlsafe(24)
        self._server = _QuietServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def mcp_config(self) -> dict:
        return {"mcpServers": {self.SERVER_NAME: {"type": "http", "url": self.url,
                                                 "headers": {"Authorization": f"Bearer {self.token}"}}}}

    def tool_name(self, full_name: str) -> str | None:
        """AgentFlight tool name from Claude Code's `mcp__agentflight__<name>` form."""
        prefix = f"mcp__{self.SERVER_NAME}__"
        if full_name.startswith(prefix):
            return self.name_map.get(full_name[len(prefix):])
        return None

    def close(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass

    def _respond(self, body: dict) -> dict | None:
        method = body.get("method")
        request_id = body.get("id")
        if request_id is None:
            return None  # notification
        if method == "initialize":
            version = (body.get("params") or {}).get("protocolVersion") or "2025-06-18"
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": self.SERVER_NAME, "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.definitions}
        elif method == "tools/call":
            params = body.get("params") or {}
            name = self.name_map.get(params.get("name", ""))
            if name is None:
                text = f"Error: there is no tool named '{params.get('name')}'."
                result = {"content": [{"type": "text", "text": text}], "isError": True}
            else:
                text, is_error = self._run_tool(name, params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}
        else:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": f"not supported: {method}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _authorized(self) -> bool:
                return (self.headers.get("Host") == f"127.0.0.1:{bridge.port}"
                        and secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {bridge.token}"))

            def _empty(self, code: int, headers: dict | None = None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                self._empty(405 if self._authorized() else 401, {"Allow": "POST"})

            def do_DELETE(self):
                self._empty(200 if self._authorized() else 401)

            def do_POST(self):
                try:
                    # Always read the body: answering without reading it makes Windows reset the connection.
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length)
                except (ValueError, OSError):
                    self._empty(400)
                    return
                if not self._authorized():
                    self._empty(401)
                    return
                try:
                    body = json.loads(raw or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._empty(400)
                    return
                if isinstance(body, list):
                    responses = [r for r in (bridge._respond(b) for b in body) if r is not None]
                    data = responses if responses else None
                else:
                    data = bridge._respond(body)
                if data is None:
                    self._empty(202)
                    return
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler
