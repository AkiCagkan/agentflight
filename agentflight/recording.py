"""Run records and replay.

Every run is written under `<project>/.agentflight/runs/<run_id>/`:
  events.jsonl   - the event stream (without token events)
  calls.jsonl    - each agent call's input, the messages sent, its result and cost
  summary.json   - file, duration, status, total cost

Replay: calls with the same key (same agent, instructions, model and input) return the recorded result without
going to the model. If `step` is given, only the calls before that step come from the record; that step and the
ones after it run live.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import defaultdict
from pathlib import Path


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def runs_dir(project: Path) -> Path:
    return Path(project) / ".agentflight" / "runs"


class Recorder:
    def __init__(self, folder: Path, meta: dict):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._events = open(self.folder / "events.jsonl", "a", encoding="utf-8")
        self._calls = open(self.folder / "calls.jsonl", "a", encoding="utf-8")
        self.meta = dict(meta)
        self._write_summary({**self.meta, "status": "running"})

    def _write_summary(self, data):
        (self.folder / "summary.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def event(self, event: dict) -> None:
        if event.get("type") == "token":
            return
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            if not self._events.closed:
                self._events.write(line + "\n")
                self._events.flush()

    def call(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            if not self._calls.closed:
                self._calls.write(line + "\n")
                self._calls.flush()

    def close(self, summary: dict) -> None:
        with self._lock:
            self._events.close()
            self._calls.close()
        self._write_summary({**self.meta, **summary})


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return result


def read_run(project: Path, run_id: str) -> dict:
    folder = runs_dir(project) / run_id
    if not folder.is_dir():
        raise FileNotFoundError(f"run record not found: {run_id}")
    summary_path = folder / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    return {
        "run_id": run_id,
        "summary": summary,
        "events": _read_jsonl(folder / "events.jsonl"),
        "calls": _read_jsonl(folder / "calls.jsonl"),
    }


def list_runs(project: Path, limit: int = 50) -> list[dict]:
    folder = runs_dir(project)
    if not folder.is_dir():
        return []
    result = []
    for sub in sorted(folder.iterdir(), reverse=True):
        if not sub.is_dir():
            continue
        summary_path = sub / "summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        result.append({"run_id": sub.name, **summary})
        if len(result) >= limit:
            break
    return result


class ReplayStore:
    """Reuses recorded calls by their keys."""

    def __init__(self, calls: list[dict], step_limit: int | None = None):
        self._lock = threading.Lock()
        self._store: dict[str, list[dict]] = defaultdict(list)
        for c in sorted(calls, key=lambda c: c.get("step", 0)):
            if c.get("status") != "done" or "key" not in c:
                continue
            if step_limit is not None and c.get("step", 0) >= step_limit:
                continue
            self._store[c["key"]].append(c)

    def take(self, key: str) -> dict | None:
        with self._lock:
            entries = self._store.get(key)
            if entries:
                return entries.pop(0)
        return None

    @classmethod
    def from_run(cls, project: Path, run_id: str, step: int | None = None) -> ReplayStore:
        return cls(read_run(project, run_id)["calls"], step)
