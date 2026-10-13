"""Live view in the terminal: writes events as readable lines."""

from __future__ import annotations

import json
import os
import threading

_COLOR = os.environ.get("NO_COLOR") is None


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def gray(m):
    return _c("90", m)


def green(m):
    return _c("32", m)


def red(m):
    return _c("31", m)


def yellow(m):
    return _c("33", m)


def blue(m):
    return _c("94", m)


def bold(m):
    return _c("1", m)


def _short(value, n=60):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _duration(ms):
    if ms is None:
        return ""
    return f"{ms / 1000:.1f} s"


_STATUSES = {"check_failed": "check failed"}


def plural(n: int, word: str) -> str:
    """`1 call`, `2 calls`."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def status_text(status) -> str:
    return _STATUSES.get(status, str(status))


class ConsoleWriter:
    def __init__(self, stream):
        self.stream = stream
        self._lock = threading.Lock()
        self._calls: dict[str, dict] = {}

    def _depth(self, call_id):
        d, seen = 0, set()
        while call_id and call_id in self._calls and call_id not in seen:
            seen.add(call_id)
            call_id = self._calls[call_id].get("parent")
            d += 1
        return d

    def _write(self, text, depth=0):
        with self._lock:
            try:
                print("  " * depth + text, file=self.stream, flush=True)
            except Exception:  # noqa: BLE001 - the live log must never break the program
                pass

    def __call__(self, e: dict):
        t = e.get("type")
        if t == "run_started":
            extra = f" · model: {e['model']}" if e.get("model") else ""
            replay = (f" · replaying: {e['replay']}"
                      + (f" (live from step {e['step']})" if e.get("step") else "")) if e.get("replay") else ""
            self._write(bold(f"▶ {e.get('file_name', '')}") + gray(extra + replay))
        elif t == "call_started":
            self._calls[e["id"]] = e
            d = self._depth(e.get("parent"))
            line = f" · line {e['line']}" if e.get("line") else ""
            self._write(blue(f"[{e['step']}] {e['agent']}.{e['method']}")
                        + gray(f"({_short(e.get('input', {}), 50)}){line} · {e.get('model')}"), d)
        elif t == "tool_started":
            d = self._depth(e["id"])
            self._write(gray(f"↳ {e['name']}({_short(e.get('arguments', {}), 50)})"), d)
        elif t == "tool_finished":
            d = self._depth(e["id"])
            mark = red("✗") if e.get("error") else green("✓")
            self._write(f"{mark} {e['name']} "
                        + gray(f"{_duration(e.get('duration_ms'))} → {_short(e.get('result', ''), 60)}"), d)
        elif t == "format_error":
            d = self._depth(e["id"])
            first = (e.get("errors") or ["?"])[0]
            self._write(yellow(f"⚠ format error (attempt {e['attempt']}/{e['max_attempts']}): {first}")
                        + gray(" → asking for a fix"), d)
        elif t == "model_switched":
            d = self._depth(e["id"])
            reason = _short(e.get("reason", ""), 70)
            self._write(yellow(f"⚠ {e['old']} unavailable ({reason}); continuing with {e['new']}"), d)
        elif t == "message_added":
            d = self._depth(e["id"])
            self._write(yellow("✉ message: " + _short(e.get("text", ""), 70)), d)
        elif t == "call_finished":
            info = self._calls.get(e["id"], {})
            d = self._depth(info.get("parent"))
            name = f"[{info.get('step', '?')}] {info.get('agent', '')}.{info.get('method', '')}"
            status = e.get("status")
            if status == "done":
                tokens = (e.get("input_tokens") or 0) + (e.get("output_tokens") or 0)
                price = "subscription" if e.get("subscription") else f"${e.get('cost', 0):.4f}"
                if e.get("from_record"):
                    extra = gray(" · from record")
                else:
                    extra = gray(f" {_duration(e.get('duration_ms'))} · {plural(tokens, 'token')} · {price}")
                self._write(green("✓ ") + name + extra + gray(f" → {_short(e.get('result'), 70)}"), d)
            elif status == "stopped":
                self._write(yellow("■ ") + name + gray(" " + status_text("stopped")), d)
            else:
                self._write(red("✗ ") + name + red(f" {e.get('error', '')}"), d)
        elif t == "parallel":
            d = self._depth(e.get("parent"))
            where = f" (line {e['line']})" if e.get("line") else ""
            self._write(gray(f"⇉ parallel: {plural(e['n'], 'task')}{where}"), d)
        elif t == "approval_answered":
            decision = {"approve": green("approved"), "reject": red("rejected"), "edit": yellow("edited and approved")}
            self._write(gray("⏸ approval: ") + decision.get(e.get("decision"), str(e.get("decision"))))
        elif t == "run_finished":
            status = e.get("status")
            color = green if status == "done" else red
            self._write(color(f"■ {status_text(status)}") + gray(
                f" · {_duration(e.get('duration_ms'))} · {plural(e.get('call_count', 0), 'agent call')} · "
                f"{plural(e.get('total_tokens', 0), 'token')} · ${e.get('total_cost', 0):.4f}"
                + (f" · record: {e['run_id']}" if e.get("recorded") else "")
            ))
