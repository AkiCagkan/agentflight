"""Event bus and live control.

The runtime publishes an event at every step (call started, token arrived, tool called, finished...). The panel
listens to these events live, and the same events are written to the run record.

`Control` passes the panel's commands (pause, resume, message, kill, approve) on to running agents. Agents call
`checkpoint` at safe points: it waits while paused and raises AgentStopped once killed.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from .errors import AgentStopped


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._listeners = []
        self._t0 = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def add(self, listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove(self, listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def publish(self, event_type: str, /, **fields) -> dict:
        event = {"type": event_type, "elapsed_ms": self.elapsed_ms, **fields}
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a failing listener must not stop the run
                pass
        return event


class Control:
    """Passes panel commands on to the agents."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._cond = threading.Condition()
        self.paused_all = False
        self.paused: set[str] = set()
        self.killed: set[str] = set()
        self.parents: dict[str, str | None] = {}
        self.active: set[str] = set()
        self._messages: dict[str, list[str]] = defaultdict(list)
        self._approvals: dict[str, dict] = {}
        self._pending_approvals: set[str] = set()
        self.panel_connected = False

    # --- call lifecycle
    def call_started(self, call_id: str, parent: str | None) -> None:
        with self._cond:
            self.parents[call_id] = parent
            self.active.add(call_id)

    def call_finished(self, call_id: str) -> None:
        with self._cond:
            self.active.discard(call_id)
            self.paused.discard(call_id)
            self._messages.pop(call_id, None)

    def _ancestors(self, call_id):
        seen = set()
        while call_id and call_id not in seen:
            seen.add(call_id)
            yield call_id
            call_id = self.parents.get(call_id)

    def _is_killed(self, call_id) -> bool:
        return any(a in self.killed for a in self._ancestors(call_id))

    def _is_paused(self, call_id) -> bool:
        return self.paused_all or any(a in self.paused for a in self._ancestors(call_id))

    def checkpoint(self, call_id: str | None) -> None:
        """Safe point: waits while paused, raises AgentStopped once killed."""
        if call_id is None:
            with self._cond:
                while self.paused_all:
                    self._cond.wait(0.5)
            return
        announced = False
        with self._cond:
            while True:
                if self._is_killed(call_id):
                    raise AgentStopped(call_id)
                if not self._is_paused(call_id):
                    break
                if not announced:
                    announced = True
                    self.bus.publish("state", id=call_id, state="paused")
                self._cond.wait(0.5)
        if announced:
            self.bus.publish("state", id=call_id, state="running")

    def take_messages(self, call_id: str) -> list[str]:
        with self._cond:
            return self._messages.pop(call_id, [])

    # --- approval
    def wait_for_approval(self, approval_id: str) -> dict:
        with self._cond:
            self._pending_approvals.add(approval_id)
            while approval_id not in self._approvals:
                self._cond.wait(0.5)
            self._pending_approvals.discard(approval_id)
            return self._approvals.pop(approval_id)

    # --- commands
    def command(self, cmd: dict) -> None:
        kind = cmd.get("command")
        target = cmd.get("id")
        with self._cond:
            if kind == "pause":
                if target:
                    self.paused.add(target)
                else:
                    self.paused_all = True
            elif kind == "resume":
                if target:
                    self.paused.discard(target)
                else:
                    self.paused_all = False
                    self.paused.clear()
            elif kind == "kill" and target:
                self.killed.add(target)
            elif kind == "message" and target and cmd.get("text"):
                self._messages[target].append(str(cmd["text"]))
            elif kind == "approve" and cmd.get("approval_id"):
                self._approvals[cmd["approval_id"]] = {"decision": cmd.get("decision", "approve"),
                                                       "value": cmd.get("value")}
            self._cond.notify_all()
        if kind == "pause":
            self.bus.publish("command_received", command="pause", id=target)
        elif kind == "resume":
            self.bus.publish("command_received", command="resume", id=target)
        elif kind == "kill" and target:
            self.bus.publish("command_received", command="kill", id=target)
        elif kind == "message" and target and cmd.get("text"):
            self.bus.publish("message_added", id=target, text=str(cmd["text"]))
