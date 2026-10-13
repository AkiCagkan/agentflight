"""User settings: model API keys and account preferences.

File: ~/.agentflight/settings.json (the folder can be changed with AGENTFLIGHT_HOME). On Windows the API keys are
encrypted with DPAPI, bound to the user account (another user or computer cannot decrypt them). Keys are passed
to program processes as environment variables; a key saved in the settings overrides the one in the environment.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path

KEY_NAMES = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OLLAMA_HOST")
SECRET_KEYS = {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"}

_LOCK = threading.Lock()
_INITIAL_ENV = {name: os.environ.get(name) for name in KEY_NAMES}  # what the environment had at startup


def settings_dir() -> Path:
    folder = Path(os.environ.get("AGENTFLIGHT_HOME") or (Path.home() / ".agentflight"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _file() -> Path:
    return settings_dir() / "settings.json"


# ---------------------------------------------------------------------------
# Windows DPAPI

def _dpapi(data: bytes, decrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    crypt32 = ctypes.windll.crypt32
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), ctypes.c_wchar_p("agentflight"), None, None, None, 0,
                                      ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI operation failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _encrypt(text: str) -> str:
    if sys.platform == "win32":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), decrypt=False)).decode("ascii")
        except OSError:
            pass
    return "plain:" + text


def _decrypt(stored: str) -> str:
    if stored.startswith("dpapi:"):
        try:
            return _dpapi(base64.b64decode(stored[6:]), decrypt=True).decode("utf-8")
        except (OSError, ValueError):
            return ""
    if stored.startswith("plain:"):
        return stored[6:]
    return stored


# ---------------------------------------------------------------------------

def read() -> dict:
    with _LOCK:
        try:
            data = json.loads(_file().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    data.setdefault("keys", {})
    return data


def write(data: dict) -> None:
    with _LOCK:
        file = _file()
        temp = file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, file)


def key(name: str) -> str:
    return _decrypt(read()["keys"].get(name, ""))


def set_keys(values: dict) -> None:
    """{name: value}; an empty or None value deletes the key."""
    data = read()
    for name, value in values.items():
        if name not in KEY_NAMES:
            continue
        if value:
            data["keys"][name] = _encrypt(str(value).strip())
        else:
            data["keys"].pop(name, None)
    write(data)
    apply_to_env(force=True)


def apply_to_env(force: bool = False) -> None:
    """Writes the saved keys into this process's environment. A saved key overrides the environment; when a key
    is deleted from the settings, the value the environment had at startup comes back."""
    data = read()
    for name in KEY_NAMES:
        value = _decrypt(data["keys"].get(name, ""))
        if value:
            os.environ[name] = value
        elif force:
            if _INITIAL_ENV.get(name):
                os.environ[name] = _INITIAL_ENV[name]
            else:
                os.environ.pop(name, None)


def key_summary() -> dict:
    data = read()
    summary = {}
    for name in KEY_NAMES:
        saved = _decrypt(data["keys"].get(name, ""))
        if saved:
            value, source = saved, "settings"
        elif _INITIAL_ENV.get(name):
            value, source = _INITIAL_ENV[name], "env"
        else:
            value, source = "", ""
        if name in SECRET_KEYS and value:
            display = value[:4] + "…" + value[-4:] if len(value) > 12 else "••••"
        else:
            display = value
        summary[name] = {"set": bool(value), "source": source, "display": display, "secret": name in SECRET_KEYS}
    return summary


def set_preference(section: str, values: dict) -> None:
    """Sections: `account_priority` ({provider: "account" | "key"}) and `claude_path` ({"path": ...})."""
    data = read()
    if section == "claude_path":
        data[section] = str(values.get("path", "")).strip()
    else:
        data.setdefault(section, {}).update(values)
    write(data)
