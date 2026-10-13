"""Shared helpers for the test suite.

Importing this module isolates the tests from the machine: settings go to a temporary AGENTFLIGHT_HOME, the fake model
answers without delay, the real Claude Code is never used and no console log is printed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

os.environ.setdefault("AGENTFLIGHT_FAKE_DELAY", "0")
os.environ["NO_COLOR"] = "1"
os.environ["AGENTFLIGHT_QUIET"] = "1"
os.environ.setdefault("AGENTFLIGHT_HOME", tempfile.mkdtemp(prefix="agentflight-test-home-"))
os.environ["AGENTFLIGHT_CLAUDE"] = str(Path(tempfile.gettempdir()) / "agentflight-test-no-claude")
os.environ.pop("AGENTFLIGHT_MODEL", None)
for _key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(_key, None)


def temp_project(files: dict[str, str], model: str = "fake") -> Path:
    """A project folder with an agentflight.toml (default model `model`) and the given files (dedented)."""
    folder = Path(tempfile.mkdtemp(prefix="agentflight-test-"))
    (folder / "agentflight.toml").write_text(f'[project]\nname = "test"\nmodel = "{model}"\n', encoding="utf-8")
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return folder


def cleanup(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def run_cli(*args: str, cwd: Path | None = None, input: str | None = None, timeout: int = 120,
            env: dict | None = None) -> subprocess.CompletedProcess:
    """Runs `python -m agentflight ARGS` in a subprocess (with the console log enabled)."""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "AGENTFLIGHT_FAKE_DELAY": "0", "NO_COLOR": "1",
                   **(env or {})}
    environment.pop("AGENTFLIGHT_QUIET", None)
    return subprocess.run([sys.executable, "-m", "agentflight", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", input=input, timeout=timeout, env=environment)


class EventCollector:
    """An event-bus listener that keeps every event."""

    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)

    def of_type(self, event_type: str) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e["type"] == event_type]


def wait_until(condition, timeout: float = 10.0, interval: float = 0.01) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(interval)
    return False


def in_background(fn):
    """Runs fn on a thread; returns (thread, result dict with "value" or "error")."""
    result: dict = {}

    def target():
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - the test inspects it
            result["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result
