"""Connection between a running program and the panel server (program side).

Two ways in:
  * the panel starts the program with AGENTFLIGHT_PANEL_SOCKET=host:port and AGENTFLIGHT_PANEL_TOKEN (panel-launched
    re-run or replay);
  * the program finds a running panel through ~/.agentflight/panel.json and connects by itself
    ("external" handshake: python main.py, agentflight run, an editor's run button).

Events go to the server as JSON lines; commands from the panel (pause, message, kill, approve...)
come back on the same connection.
"""

from __future__ import annotations

import io
import json
import socket
import threading


class PanelConnection:
    def __init__(self, address: str, token: str, control, run_id: str, external: dict | None = None):
        host, port = address.rsplit(":", 1)
        self.socket = socket.create_connection((host, int(port)), timeout=5)
        self.socket.settimeout(None)
        self.control = control
        self._lock = threading.Lock()
        self._writer = self.socket.makefile("wb")
        self._open = True
        hello = {"hello": True, "token": token, "run_id": run_id}
        if external is not None:
            hello["external"] = external
        self._send(hello)
        threading.Thread(target=self._read, daemon=True).start()

    @property
    def is_open(self) -> bool:
        return self._open

    def _send(self, data: dict) -> None:
        line = (json.dumps(data, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._lock:
            if not self._open:
                return
            try:
                self._writer.write(line)
                self._writer.flush()
            except OSError:
                self._open = False

    def __call__(self, event: dict) -> None:
        self._send(event)

    def _read(self) -> None:
        try:
            for line in self.socket.makefile("rb"):
                try:
                    command = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                self.control.command(command)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._open = False
            try:
                self._writer.flush()
            except OSError:
                pass
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class OutputMirror(io.TextIOBase):
    """Writes to the original stream and mirrors whole lines to the panel ("output" events)."""

    def __init__(self, original, connection: PanelConnection, stream: str):
        self.original = original
        self.connection = connection
        self.stream = stream
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        n = self.original.write(text)
        with self._lock:
            self._buffer += text
            *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            self.connection({"type": "output", "stream": self.stream, "text": line.rstrip("\r")})
        return n if n is not None else len(text)

    def flush(self) -> None:
        try:
            self.original.flush()
        except (OSError, ValueError):
            pass

    def drain(self) -> None:
        """Sends the unfinished last line, if any."""
        with self._lock:
            rest, self._buffer = self._buffer, ""
        if rest:
            self.connection({"type": "output", "stream": self.stream, "text": rest})

    @property
    def encoding(self):
        return getattr(self.original, "encoding", "utf-8")

    def isatty(self) -> bool:
        try:
            return self.original.isatty()
        except (AttributeError, ValueError):
            return False

    def fileno(self) -> int:
        return self.original.fileno()

    def writable(self) -> bool:
        return True
