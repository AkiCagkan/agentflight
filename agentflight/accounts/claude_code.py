"""Connection to Claude Code on the user's computer.

Anthropic does not allow third-party apps to offer claude.ai sign-in or to collect Claude credentials, but it
does allow users to sign in to the unmodified Claude Code with their own Claude subscription through Claude
Code's own sign-in flow (code.claude.com/docs/en/legal-and-compliance). This module therefore only:
  - finds the `claude` executable and reads the sign-in state with `claude auth status`,
  - starts sign-in with Claude Code's own command (`claude auth login`),
  - prepares a clean environment for running Claude Code from inside AgentFlight.
AgentFlight never reads, stores or forwards any Claude credential; it only runs the unmodified `claude` binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

INSTALL_URL = "https://code.claude.com/docs/en/setup"
USAGE_URL = "https://claude.ai/settings/usage"
# Removed so that Claude Code started from AgentFlight does not think it is running inside another Claude Code session.
_SESSION_VARS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT")
_SESSION_PREFIX = "CLAUDE_CODE_"
_PRESERVED = {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
              "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_OAUTH_TOKEN"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_CACHE: dict = {}
_LOCK = threading.Lock()


def executable() -> str | None:
    """Full path of the `claude` executable (can be set by hand in the settings)."""
    from .. import config

    manual = (config.read().get("claude_path") or os.environ.get("AGENTFLIGHT_CLAUDE") or "").strip()
    if manual:
        return manual if Path(manual).is_file() else None
    found = shutil.which("claude")
    if found:
        return found
    if os.name == "nt":
        candidate = Path.home() / ".local" / "bin" / "claude.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def env(subscription: bool = True) -> dict:
    """Environment for a Claude Code subprocess. With `subscription` the API key is removed from the environment
    so that requests go through the user's Claude Code sign-in (their subscription)."""
    e = {k: v for k, v in os.environ.items()
         if k not in _SESSION_VARS and not (k.startswith(_SESSION_PREFIX) and k not in _PRESERVED)}
    if subscription:
        e.pop("ANTHROPIC_API_KEY", None)
        e.pop("ANTHROPIC_AUTH_TOKEN", None)
    return e


def command(exe: str, *args: str) -> list[str]:
    lower = exe.lower()
    if lower.endswith(".py"):  # the fake claude used by the tests
        import sys
        return [sys.executable, exe, *args]
    if lower.endswith((".cmd", ".bat")):
        # npm install: run the node script directly so JSON arguments do not pass through cmd.exe.
        script = Path(exe).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        node = shutil.which("node")
        if script.is_file() and node:
            return [node, str(script), *args]
        return ["cmd", "/d", "/s", "/c", exe, *args]
    return [exe, *args]


def status(refresh: bool = False) -> dict:
    """{"installed", "signed_in", "email", "subscription", "auth_method", "version", "path", "install_url",
    "usage_url"}"""
    with _LOCK:
        if not refresh and _CACHE.get("time", 0) > time.time() - 30:
            return dict(_CACHE["status"])
    exe = executable()
    result = {"installed": bool(exe), "signed_in": False, "email": "", "subscription": "", "auth_method": "",
              "version": "", "path": exe or "", "install_url": INSTALL_URL, "usage_url": USAGE_URL}
    if exe:
        try:
            out = subprocess.run(command(exe, "auth", "status"), capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=20, env=env(), creationflags=NO_WINDOW,
                                 stdin=subprocess.DEVNULL)
            data = json.loads(out.stdout[out.stdout.find("{"):]) if "{" in out.stdout else {}
            result.update(signed_in=bool(data.get("loggedIn")), email=data.get("email") or "",
                          subscription=data.get("subscriptionType") or "", auth_method=data.get("authMethod") or "")
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        try:
            v = subprocess.run(command(exe, "--version"), capture_output=True, text=True, timeout=20, env=env(),
                               creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
            result["version"] = v.stdout.strip().split(" ")[0]
        except (OSError, subprocess.SubprocessError):
            pass
    with _LOCK:
        _CACHE.update(time=time.time(), status=dict(result))
    return result


def is_connected() -> bool:
    s = status()
    return s["installed"] and s["signed_in"]


def login_command() -> str:
    """Sign-in command the user runs in a terminal (Claude Code's own flow)."""
    exe = executable()
    if not exe:
        return ""
    return (f'& "{exe}" auth login' if os.name == "nt" else f'"{exe}" auth login')


def install_command() -> str:
    """Anthropic's official install command (the user runs it in a terminal)."""
    if os.name == "nt":
        return "irm https://claude.ai/install.ps1 | iex"
    return "curl -fsSL https://claude.ai/install.sh | bash"


def logout() -> bool:
    exe = executable()
    if not exe:
        return False
    try:
        subprocess.run(command(exe, "auth", "logout"), capture_output=True, timeout=30, env=env(),
                       creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    status(refresh=True)
    return True
