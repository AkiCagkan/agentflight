"""Control flow helpers: running agent calls in parallel and asking a human for approval."""

from __future__ import annotations

import contextvars
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from .errors import ApprovalRejected
from .runtime import caller_location, current_call, current_run
from .schema import Schema, to_jsonable

MAX_CONCURRENCY = 8

__all__ = ["parallel", "parallel_map", "parallel_for_each", "require_approval"]


# ---------------------------------------------------------------------------
# parallel

def _run_tasks(tasks, line=None):
    if not tasks:
        return []
    run = current_run()
    run.bus.publish("parallel", n=len(tasks), line=line, parent=current_call.get())
    contexts = [contextvars.copy_context() for _ in tasks]
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENCY, len(tasks))) as pool:
        futures = [pool.submit(ctx.run, task) for ctx, task in zip(contexts, tasks, strict=True)]
        results, first_error = [], None
        for f in futures:
            try:
                results.append(f.result())
            except BaseException as e:  # noqa: BLE001 - re-raised once the other tasks have finished
                results.append(None)
                first_error = first_error or e
    if first_error is not None:
        raise first_error
    return results


def parallel(*tasks) -> tuple:
    """Runs zero-argument functions at the same time and returns their results in order.

        summary, translation = parallel(lambda: Summarizer().summarize(text),
                                        lambda: Translator().translate(text))
    """
    _, line = caller_location()
    return tuple(_run_tasks(list(tasks), line))


def parallel_map(fn, items) -> list:
    """Calls `fn` for every item at the same time and returns the results in order."""
    items = list(items)
    _, line = caller_location()
    return _run_tasks([(lambda o=o: fn(o)) for o in items], line)


def parallel_for_each(fn, items) -> None:
    """Calls `fn` for every item at the same time (results are discarded)."""
    items = list(items)
    _, line = caller_location()
    _run_tasks([(lambda o=o: fn(o)) for o in items], line)


# ---------------------------------------------------------------------------
# approval

def _ask_in_terminal(value, message) -> dict:
    stream = sys.stderr
    print("\n" + "─" * 60, file=stream)
    print(f"⏸  APPROVAL NEEDED: {message or 'continue?'}", file=stream)
    shown = json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)
    if len(shown) > 3000:
        shown = shown[:3000] + "\n…"
    print(shown, file=stream)
    if not sys.stdin or not sys.stdin.isatty():
        print("Non-interactive session: approval could not be asked (use --approve auto to approve all).",
              file=stream)
        return {"decision": "reject"}
    try:
        while True:
            answer = input("Approve? [Y]es / [n]o / [e]dit: ").strip().lower()
            if answer in ("", "y", "yes"):
                return {"decision": "approve"}
            if answer in ("n", "no"):
                return {"decision": "reject"}
            if answer in ("e", "edit"):
                new_value = input("New value (JSON or plain text): ").strip()
                return {"decision": "edit", "value": new_value}
    except EOFError:  # stdin closed (piped input ran out, detached terminal)
        print("\nNo input: the approval is rejected (use --approve auto to approve all).", file=stream)
        return {"decision": "reject"}


def _edited(value, new_value):
    if isinstance(new_value, str) and not isinstance(value, str):
        try:
            new_value = json.loads(new_value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, Schema):
        return type(value).validate(new_value)
    if hasattr(value, "model_validate"):
        return type(value).model_validate(new_value)
    if isinstance(value, str):
        return new_value if isinstance(new_value, str) else json.dumps(new_value, ensure_ascii=False)
    return new_value


def require_approval(value=None, message: str | None = None):
    """Pauses the program until a human approves.

    Returns the value (or the edited value); raises ApprovalRejected if rejected. In the panel an
    Approve / Edit / Reject dialog opens; in a terminal the question is asked on stdin.
    """
    run = current_run()
    approval_id = run.new_id("o")
    file, line = caller_location()
    parent = current_call.get()
    run.bus.publish("approval_requested", approval_id=approval_id, message=message or "Continue?",
                    value=to_jsonable(value, truncate=6000), file=file, line=line, parent=parent,
                    editable=value is not None)
    if run.approval_mode == "auto":
        decision = {"decision": "approve"}
    elif run.approval_mode == "panel":
        decision = run.control.wait_for_approval(approval_id)
    else:
        decision = _ask_in_terminal(value, message)
    run.bus.publish("approval_answered", approval_id=approval_id, decision=decision.get("decision"),
                    value=to_jsonable(decision.get("value"), truncate=2000)
                    if decision.get("value") is not None else None)
    if decision.get("decision") == "reject":
        raise ApprovalRejected(message or "approval rejected")
    if decision.get("decision") == "edit" and decision.get("value") is not None:
        return _edited(value, decision["value"])
    return value
