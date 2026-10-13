"""Run context: the shared state of one execution of a program.

The event bus, live control, recording, replay, budget and counters live here. The CLI and the panel set up a
`Run`; when agent modules are imported straight from Python, `current_run()` creates one on first use.
"""

from __future__ import annotations

import contextvars
import itertools
import os
import sys
import threading
from pathlib import Path

from .errors import BudgetExceeded
from .events import Control, EventBus
from .pricing import cost as compute_cost
from .recording import Recorder, ReplayStore

current_call: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_call", default=None)


class Run:
    def __init__(self, *, run_id: str = "", project: Path | None = None, model: str | None = None,
                 approval: str = "ask", total_budget: float | None = None, default_model: str | None = None,
                 recorder: Recorder | None = None, replay: ReplayStore | None = None):
        self.run_id = run_id
        self.project = Path(project) if project else Path.cwd()
        self.model_override = model          # the model chosen in the CLI/panel applies to every agent
        self.default_model = default_model   # the default from agentflight.toml
        self.approval_mode = approval        # ask | auto | panel
        self.total_budget = total_budget
        self.recorder = recorder
        self.replay = replay
        self.bus = EventBus()
        self.control = Control(self.bus)
        self._lock = threading.Lock()
        self._steps = itertools.count(1)
        self._ids = itertools.count(1)
        self.spent_by_agent: dict[str, float] = {}
        self.total_cost = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0
        if recorder is not None:
            self.bus.add(recorder.event)

    def new_id(self, prefix: str = "c") -> str:
        return f"{prefix}{next(self._ids)}"

    def next_step(self) -> int:
        with self._lock:
            self.call_count += 1
            return next(self._steps)

    def choose_model(self, agent_model: str | None) -> str:
        return (self.model_override or agent_model or self.default_model or "").strip() or "auto"

    def spend(self, agent: str, model: str, input_tokens: int, output_tokens: int, subscription: bool = False) -> float:
        # Requests on a subscription route (ChatGPT plan, Claude Code) are taken from the user's plan;
        # no API fee is charged, so they are not counted against the budget, only their tokens are.
        amount = 0.0 if subscription else compute_cost(model, input_tokens, output_tokens)
        with self._lock:
            self.spent_by_agent[agent] = self.spent_by_agent.get(agent, 0.0) + amount
            self.total_cost += amount
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
        return amount

    def check_budget(self, agent: str, agent_budget: float | None) -> None:
        with self._lock:
            spent = self.spent_by_agent.get(agent, 0.0)
            total = self.total_cost
        if agent_budget is not None and spent >= agent_budget:
            raise BudgetExceeded(f"{agent} budget exceeded: ${spent:.4f} / ${agent_budget:.2f}")
        if self.total_budget is not None and total >= self.total_budget:
            raise BudgetExceeded(f"run budget exceeded: ${total:.4f} / ${self.total_budget:.2f}")


_CURRENT: Run | None = None
_CURRENT_LOCK = threading.RLock()  # the auto session calls set_current_run while this lock is held


def current_run() -> Run:
    global _CURRENT
    with _CURRENT_LOCK:
        if _CURRENT is None:
            if not os.environ.get("AGENTFLIGHT_QUIET"):
                try:
                    from .runner import auto_session
                    session = auto_session()  # python main.py: project settings, panel, record
                except Exception as e:  # noqa: BLE001 - fall back to a bare run
                    print(f"agentflight: {e} (running without the project settings)", file=sys.stderr)
                    session = None
                if session is not None:
                    _CURRENT = session
                    return _CURRENT
            _CURRENT = Run()
            if not os.environ.get("AGENTFLIGHT_QUIET"):
                from .console import ConsoleWriter
                _CURRENT.bus.add(ConsoleWriter(sys.stderr))
        return _CURRENT


def set_current_run(run: Run | None) -> None:
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = run


_PACKAGE = str(Path(__file__).resolve().parent).lower()
_SYSTEM = tuple({str(Path(p).resolve()).lower() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)})


def is_user_file(path: str | None) -> bool:
    """Is this a user's program file (a `.py` outside the AgentFlight package and the Python installation)?"""
    if not path or path.startswith("<"):
        return False
    if not path.endswith(".py"):
        return False
    try:
        resolved = str(Path(path).resolve()).lower()
    except OSError:
        return False
    return not resolved.startswith(_PACKAGE) and not resolved.startswith(_SYSTEM) and "site-packages" not in resolved


def caller_location() -> tuple[str | None, int | None]:
    """Finds the file and line of the first user (`.py`) frame on the call stack."""
    frame = sys._getframe(1)
    while frame is not None:
        file = frame.f_code.co_filename
        if is_user_file(file):
            return file, frame.f_lineno
        frame = frame.f_back
    return None, None
