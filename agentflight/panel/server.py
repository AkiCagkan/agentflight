"""AgentFlight panel server: local HTTP server + live event stream (SSE).

  panel page (browser or editor view)  <--HTTP/SSE-->  panel server
  panel server  <--TCP (JSON lines)-->  running program (python main.py / agentflight run / agentflight eval)

Programs run in their own processes; they send their events to the server and the server forwards
them to the page. The page's buttons (pause, message, kill, approve) travel back to the program the
same way.

Security: binds to 127.0.0.1 only; every POST needs a token embedded in the page (so other sites
cannot send requests) and the Host header is checked (against DNS rebinding).
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__, config
from ..localhttp import LocalHTTPServer

STATIC = Path(__file__).resolve().parent / "static"
HISTORY_LIMIT = 40000
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("font/woff2", ".woff2")


def _interpreter(python: str | None) -> str:
    """Python that re-runs a program: the one the program reported, else this one (python.exe, not pythonw.exe)."""
    exe = Path(python or sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        exe = exe.with_name("python.exe")
    return str(exe)


class EventBroadcast:
    """Fans events out to SSE clients; replays the history to a page that connects later."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._history: deque = deque(maxlen=HISTORY_LIMIT)

    def publish(self, event: dict, history: bool = True) -> None:
        data = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            if history:
                self._history.append(data)
            clients = list(self._clients)
        for q in clients:
            q.put(data)

    def subscribe(self) -> tuple[queue.Queue, list[str]]:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._clients.append(q)
            history = list(self._history)
        return q, history

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)


class LaunchedProcess:
    """An `agentflight run` / `agentflight eval` process started by the panel (re-run or replay of a program)."""

    def __init__(self, server: PanelServer, kind: str, file: Path, extra_args: list[str], meta: dict,
                 python: str | None = None, extra_env: dict | None = None):
        self.server = server
        self.sid = secrets.token_hex(4)
        self.kind = kind
        self.file = file
        self.meta = meta
        self.connection: socket.socket | None = None
        self._write_lock = threading.Lock()
        self.done = False
        server.processes[self.sid] = self  # must be registered before the program connects
        env = {
            **os.environ,
            "AGENTFLIGHT_PANEL_SOCKET": f"127.0.0.1:{server.runner_port}",
            "AGENTFLIGHT_PANEL_TOKEN": f"{server.token}:{self.sid}",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
            **(extra_env or {}),
        }
        subcommand = "run" if kind == "program" else "eval"
        command = [_interpreter(python), "-m", "agentflight", subcommand, str(file)] + extra_args
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.popen = subprocess.Popen(
                command, cwd=str(file.parent), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
            )
        except OSError as e:
            server.processes.pop(self.sid, None)
            if getattr(e, "winerror", None) == 4551:
                raise OSError(f"Windows Smart App Control blocked the interpreter ({command[0]}). "
                              "Start the program with a signed Python interpreter (e.g. from python.org) "
                              "and run it again from the panel.") from None
            raise
        shown = ["agentflight", subcommand, file.name] + extra_args
        server.publish({"type": "process_started", "sid": self.sid, "kind": kind, "file": str(file),
                        "path": server.display_path(file), "command": " ".join(shown), **meta})
        for stream, name in ((self.popen.stdout, "stdout"), (self.popen.stderr, "stderr")):
            threading.Thread(target=self._read_stream, args=(stream, name), daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _read_stream(self, stream, name):
        for raw in stream:
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.server.publish({"type": "output", "sid": self.sid, "stream": name, "text": text})

    def _wait(self):
        code = self.popen.wait()
        time.sleep(0.2)  # let the last output lines arrive
        self.done = True
        self.server.publish({"type": "process_finished", "sid": self.sid, "exit_code": code})
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass

    def send_command(self, command: dict) -> bool:
        if self.connection is None or self.done:
            return False
        line = (json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.connection.sendall(line)
                return True
            except OSError:
                return False

    def stop(self):
        if self.popen.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.popen.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            self.popen.kill()


class ExternalProcess:
    """A program that connected by itself, not started by the panel (python main.py, agentflight run, an editor)."""

    def __init__(self, server: PanelServer, connection: socket.socket, info: dict):
        self.server = server
        self.sid = secrets.token_hex(4)
        self.connection = connection
        self.info = info
        self.kind = info.get("kind") or "program"
        self.file = Path(info.get("file") or ".")
        self.pid = int(info.get("pid") or 0)
        self.done = False
        self.exit_code = None
        self._write_lock = threading.Lock()
        project = info.get("project")
        if project:
            server.known_projects.add(str(Path(project).resolve()))
        server.processes[self.sid] = self
        server.publish({"type": "process_started", "sid": self.sid, "kind": self.kind, "file": str(self.file),
                        "path": server.display_path(self.file), "command": info.get("command") or self.file.name,
                        "external": True, "project": project, "python": info.get("python"), "cwd": info.get("cwd"),
                        "model": info.get("model"), "env_model": info.get("env_model")})

    def send_command(self, command: dict) -> bool:
        if self.done:
            return False
        line = (json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.connection.sendall(line)
                return True
            except OSError:
                return False

    def finish(self, exit_code=None) -> None:
        if self.done:
            return
        self.done = True
        self.server.publish({"type": "process_finished", "sid": self.sid,
                             "exit_code": exit_code if exit_code is not None else self.exit_code})

    def stop(self):
        if self.done or self.pid <= 0 or self.pid == os.getpid():
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.kill(self.pid, 15)
            except OSError:
                pass


class PanelServer:
    """State of the panel: event history, connected programs and the projects they belong to."""

    def __init__(self, port: int):
        self.port = port
        self.known_projects: set[str] = set()
        self.shutdown_callback = None
        self.token = secrets.token_hex(16)
        self.broadcast = EventBroadcast()
        self.processes: dict[str, LaunchedProcess | ExternalProcess] = {}
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.runner_port = self._listener.getsockname()[1]
        config.apply_to_env()
        threading.Thread(target=self._accept_runners, daemon=True).start()

    # --- helpers
    def publish(self, event: dict, history: bool = True) -> None:
        self.broadcast.publish(event, history)

    def display_path(self, path: Path) -> str:
        """Short path shown in the panel: folder/file name."""
        path = Path(path)
        return f"{path.parent.name}/{path.name}" if path.parent.name else path.name

    def project_root(self, project: str | None) -> Path:
        """Project whose runs may be read: only projects whose programs connected to the panel."""
        if not project:
            raise PermissionError("no project given")
        candidate = str(Path(project).resolve())
        if candidate not in self.known_projects:
            raise PermissionError("unknown project")
        return Path(candidate)

    # --- connections from running programs
    def _accept_runners(self):
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._read_runner, args=(connection,), daemon=True).start()

    def _read_runner(self, connection: socket.socket):
        process = None
        try:
            for raw in connection.makefile("rb"):
                try:
                    event = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if process is None:
                    token = str(event.get("token", ""))
                    main, _, sid = token.partition(":")
                    if not event.get("hello") or not secrets.compare_digest(main, self.token):
                        connection.close()
                        return
                    if not sid and isinstance(event.get("external"), dict):
                        process = ExternalProcess(self, connection, event["external"])
                        continue
                    if sid not in self.processes:
                        connection.close()
                        return
                    process = self.processes[sid]
                    process.connection = connection
                    continue
                if isinstance(process, ExternalProcess) and event.get("type") == "run_finished":
                    process.exit_code = 0 if event.get("status") == "done" else 1
                event["sid"] = process.sid
                # Token chunks are not kept in the history: call_finished carries the full result.
                self.publish(event, history=event.get("type") != "token")
        except OSError:
            pass
        finally:
            if isinstance(process, ExternalProcess):
                process.finish()

    # --- actions
    def launch(self, data: dict) -> LaunchedProcess:
        """Re-runs (or replays) a program of a project that connected to the panel."""
        if not data.get("file"):
            raise FileNotFoundError("no .py file to run was found")
        file = Path(data["file"]).resolve()
        if not any(file.is_relative_to(Path(p)) for p in self.known_projects):
            raise PermissionError("this file is not in a project that connected to the panel")
        if not file.is_file() or file.suffix != ".py":
            raise FileNotFoundError("no .py file to run was found")
        args, meta = [], {}
        if data.get("model"):
            args += ["--model", str(data["model"])]
            meta["model"] = data["model"]
        kind = data.get("kind", "program")
        if kind == "program":
            if data.get("replay"):
                args += ["--replay", str(data["replay"])]
                meta["replay"] = data["replay"]
                if data.get("step"):
                    args += ["--step", str(int(data["step"]))]
                    meta["step"] = int(data["step"])
            if data.get("budget"):
                args += ["--budget", str(float(data["budget"]))]
        elif data.get("name"):
            args += ["--name", str(data["name"])]
        extra_env = {"AGENTFLIGHT_MODEL": str(data["env_model"])} if data.get("env_model") else None
        return LaunchedProcess(self, kind, file, args, meta, python=data.get("python"), extra_env=extra_env)

    def close(self) -> None:
        for process in list(self.processes.values()):
            process.stop()


def _handler_class(server: PanelServer):
    allowed_hosts = {f"127.0.0.1:{server.port}", f"localhost:{server.port}", "127.0.0.1", "localhost"}

    class Handler(BaseHTTPRequestHandler):
        server_version = f"AgentFlightPanel/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # --- response helpers
        def _send(self, code: int, body: bytes, content_type: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code=200):
            self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, message, code=400):
            self._json({"error": str(message)}, code)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _host_valid(self) -> bool:
            return (self.headers.get("Host") or "") in allowed_hosts

        # --- GET
        def do_GET(self):
            if not self._host_valid():
                return self._error("invalid Host", 403)
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            try:
                if path in ("/", "/index.html"):
                    html = (STATIC / "index.html").read_text(encoding="utf-8")
                    html = html.replace("{{TOKEN}}", server.token).replace("{{VERSION}}", __version__)
                    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    file = (STATIC / path[len("/static/"):]).resolve()
                    if STATIC not in file.parents or not file.is_file():
                        return self._error("not found", 404)
                    content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    if content_type.startswith("text/") or content_type.endswith("javascript"):
                        content_type += "; charset=utf-8"
                    return self._send(200, file.read_bytes(), content_type, cache="/vendor/" in path)
                if path == "/api/events":
                    return self._sse()
                if path == "/api/status":
                    return self._json({
                        "version": __version__,
                        "running": [p.sid for p in server.processes.values() if not p.done],
                        "projects": sorted(server.known_projects),
                    })
                if path == "/api/runs":
                    from ..recording import list_runs
                    if query.get("project"):
                        root = server.project_root(query["project"])
                        return self._json([{**r, "project": str(root)} for r in list_runs(root)])
                    runs = [{**r, "project": project}
                            for project in sorted(server.known_projects) for r in list_runs(Path(project))]
                    return self._json(sorted(runs, key=lambda r: r.get("started_at", ""), reverse=True)[:200])
                if path == "/api/run":
                    from ..recording import read_run
                    return self._json(read_run(server.project_root(query.get("project")), query.get("run_id", "")))
                if path == "/api/models":
                    from ..providers import auto_model, available_models
                    return self._json({"auto": auto_model(), "models": available_models()})
                if path == "/api/accounts":
                    return self._json(account_status(refresh=query.get("refresh") == "1"))
                if path == "/api/settings":
                    return self._json({"keys": config.key_summary(),
                                       "settings_file": str(config.settings_dir() / "settings.json")})
                return self._error("not found", 404)
            except PermissionError as e:
                return self._error(e, 403)
            except FileNotFoundError as e:
                return self._error(e, 404)
            except Exception as e:  # noqa: BLE001
                return self._error(f"{type(e).__name__}: {e}", 500)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q, history = server.broadcast.subscribe()
            try:
                self.wfile.write(b'data: {"type":"history_start"}\n\n')
                for data in history:
                    self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.write(b'data: {"type":"history_end"}\n\n')
                self.wfile.flush()
                while True:
                    try:
                        data = q.get(timeout=15)
                        batch = [data]
                        while len(batch) < 200:
                            try:
                                batch.append(q.get_nowait())
                            except queue.Empty:
                                break
                        self.wfile.write("".join(f"data: {d}\n\n" for d in batch).encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": live\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                server.broadcast.unsubscribe(q)
                self.close_connection = True

        # --- POST
        def do_POST(self):
            try:
                data = self._body()
            except (ValueError, json.JSONDecodeError):
                return self._error("invalid JSON")
            if not self._host_valid():
                return self._error("invalid Host", 403)
            if not secrets.compare_digest(self.headers.get("X-AgentFlight-Token", ""), server.token):
                return self._error("invalid token", 403)
            path = urlparse(self.path).path
            try:
                if path == "/api/panel/stop":
                    if server.shutdown_callback is None:
                        return self._error("this server cannot be stopped", 400)
                    threading.Timer(0.2, server.shutdown_callback).start()
                    return self._json({"ok": True})
                # --- running
                if path == "/api/run":
                    process = server.launch({**data, "kind": "program"})
                    return self._json({"sid": process.sid})
                if path == "/api/eval":
                    process = server.launch({**data, "kind": "eval"})
                    return self._json({"sid": process.sid})
                if path == "/api/command":
                    process = server.processes.get(data.get("sid", ""))
                    if process is None:
                        return self._error("process not found", 404)
                    command = {k: v for k, v in data.items() if k != "sid"}
                    return self._json({"ok": process.send_command(command)})
                if path == "/api/stop":
                    process = server.processes.get(data.get("sid", ""))
                    if process is not None:
                        process.stop()
                    return self._json({"ok": True})
                # --- settings
                if path == "/api/settings":
                    if "keys" in data:
                        config.set_keys(data["keys"])
                    return self._json({"ok": True, "keys": config.key_summary()})
                # --- accounts
                if path.startswith("/api/accounts/"):
                    return self._json(account_action(server, path[len("/api/accounts/"):], data))
                return self._error("not found", 404)
            except PermissionError as e:
                return self._error(e, 403)
            except FileNotFoundError as e:
                return self._error(e, 404)
            except Exception as e:  # noqa: BLE001
                return self._error(f"{type(e).__name__}: {e}", 500)

    return Handler


def account_status(refresh: bool = False) -> dict:
    from ..accounts import chatgpt, claude_code
    priority = config.read().get("account_priority") or {}
    return {
        "chatgpt": chatgpt.status(),
        "claude": claude_code.status(refresh=refresh),
        "claude_login_command": claude_code.login_command(),
        "claude_install_command": claude_code.install_command(),
        "priority": {"anthropic": priority.get("anthropic", "account"), "openai": priority.get("openai", "account")},
        "keys": {"anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
                 "openai": bool(os.environ.get("OPENAI_API_KEY")),
                 "gemini": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))},
    }


def account_action(server: PanelServer, action: str, data: dict) -> dict:
    from ..accounts import chatgpt, claude_code

    def on_done(login):
        server.publish({"type": "account_changed", "account": "chatgpt", "status": login.status,
                        "message": login.message, "info": login.account}, history=False)

    if action == "chatgpt/login":
        if data.get("new"):
            return chatgpt.add_account(browser=False, on_done=on_done)
        return chatgpt.start_login(data.get("key"), browser=False, on_done=on_done)
    if action == "chatgpt/logout":
        result = chatgpt.logout(data.get("key"))
        server.publish({"type": "account_changed", "account": "chatgpt", "status": "signed_out"}, history=False)
        return result
    if action == "chatgpt/select":
        chatgpt.select_account(str(data.get("key", "")))
        return {"ok": True}
    if action == "chatgpt/welcomed":
        chatgpt.mark_welcomed(data.get("key"))
        return {"ok": True}
    if action == "chatgpt/models":
        return {"models": chatgpt.refresh_models(data.get("key"))}
    if action == "claude/logout":
        return {"ok": claude_code.logout()}
    if action == "priority":
        values = {k: v for k, v in data.items() if k in ("anthropic", "openai") and v in ("account", "key")}
        config.set_preference("account_priority", values)
        return {"ok": True}
    if action == "claude/path":
        config.set_preference("claude_path", {"path": str(data.get("path", "")).strip()})
        return account_status(refresh=True)
    raise FileNotFoundError(f"unknown account action: {action}")


class _HTTPServer(LocalHTTPServer):
    allow_reuse_address = False


def create_server(port: int = 7790) -> tuple[_HTTPServer, PanelServer]:
    """Panel state + HTTP server on the first free port from `port` up (20 tries)."""
    last_error = None
    for attempt in range(20):
        try:
            panel = PanelServer(port + attempt)
            http = _HTTPServer(("127.0.0.1", port + attempt), _handler_class(panel))
            return http, panel
        except OSError as e:
            last_error = e
    raise OSError(f"no free port found ({port}-{port + 19}): {last_error}")
