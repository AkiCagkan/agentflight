"""The watch panel (`agentflight watch`): live view of every AgentFlight program on this computer.

The panel writes its address to ~/.agentflight/panel.json; programs started anywhere (`python main.py`,
`agentflight run`, an editor's run button) find that file, connect, stream their events and accept commands
(pause, message, kill, approve). Runs connected to the panel are recorded so they can be replayed
from any step.

Editor extensions (VS Code / Cursor, JetBrains) start the panel with `agentflight watch --no-browser` and
show it inside the editor.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from .. import config

DISCOVERY_FILE = "panel.json"


def discovery_path():
    return config.settings_dir() / DISCOVERY_FILE


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def panel_info() -> dict | None:
    """Running panel: {"port", "runner_port", "token", "pid", "version", "started_at", "url"} or None."""
    try:
        info = json.loads(discovery_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _process_alive(int(info.get("pid", 0))):
        return None
    try:
        socket.create_connection(("127.0.0.1", int(info["runner_port"])), timeout=0.3).close()
    except (OSError, KeyError, ValueError):
        return None
    info["url"] = f"http://127.0.0.1:{info['port']}/"
    return info


def write_discovery(server) -> None:
    data = {"port": server.port, "runner_port": server.runner_port, "token": server.token, "pid": os.getpid(),
            "version": _version(), "started_at": time.time()}
    path = discovery_path()
    tmp = path.with_name(f"{DISCOVERY_FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def remove_discovery() -> None:
    """Removes the discovery file if it belongs to this process."""
    try:
        info = json.loads(discovery_path().read_text(encoding="utf-8"))
        if int(info.get("pid", 0)) == os.getpid():
            discovery_path().unlink()
    except (OSError, ValueError):
        pass


def _version() -> str:
    from .. import __version__
    return __version__


def stop_panel() -> bool:
    info = panel_info()
    if info is None:
        print("No panel is running.", file=sys.stderr)
        return False
    request = urllib.request.Request(f"{info['url']}api/panel/stop", data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "X-AgentFlight-Token": info["token"], "Host": f"127.0.0.1:{info['port']}"})
    try:
        urllib.request.urlopen(request, timeout=5).read()
    except OSError:
        pass
    print("Panel stopped.")
    return True


def start_panel(port: int = 7790, browser: bool = True) -> int:
    from ..console import bold, gray
    from .server import create_server

    running = panel_info()
    if running is not None:
        print(f"The panel is already running: {running['url']}")
        if browser:
            webbrowser.open(running["url"])
        return 0
    http, panel = create_server(port=port)
    url = f"http://127.0.0.1:{panel.port}/"
    write_discovery(panel)
    print(bold("AgentFlight panel") + f"  {url}")
    print(gray("AgentFlight programs running on this computer show up here "
               "(python main.py, agentflight run ...). Ctrl+C to stop."))
    if browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    panel.shutdown_callback = http.shutdown
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        remove_discovery()
        panel.close()
        http.server_close()
    return 0
