"""Eval: tests that measure the quality of agents, written next to the code.

    class SummaryRules(Eval, target=Summarizer.summarize):
        inputs = "data/*.txt"          # or a list of inputs
        repeat = 3                     # how many times each input is tried
        threshold = 0.8                # share of passing samples needed

        @staticmethod
        def check(input, result):      # optional; assert or return False to fail
            assert len(result.bullets) <= 5

    class Accuracy(Eval, target=Classifier.classify):
        examples = [("Great product", "positive"), ("Terrible", "negative")]   # compared with the expected value

    class Faithfulness(Eval, target=Summarizer.summarize):
        inputs = [...]
        judge = "Does the summary contain a claim that is not in the text? Pass if it does not."   # model judge

A sample passes when the call returns without an error, `check` (if any) does not return False or fail an
assert, the expected value from `examples` matches and the `judge` (if any) gives a positive verdict.
"""

from __future__ import annotations

import contextvars
import glob
import inspect
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agents import Agent, llm
from .runtime import current_run
from .schema import Schema, field, to_jsonable

_REGISTERED: list[type] = []


class Eval:
    """Base class of evals."""

    __agentflight_eval__ = True
    inputs = None
    examples = None
    repeat: int = 1
    threshold: float = 0.8
    judge: str | None = None
    judge_model: str | None = None
    concurrency: int = 4

    def __init_subclass__(cls, target=None, **kw):
        super().__init_subclass__(**kw)
        cls._target = target
        _REGISTERED.append(cls)


def registered_evals(module_name: str | None = None) -> list[type]:
    return [e for e in _REGISTERED if module_name is None or e.__module__ == module_name]


def _resolve_target(eval_cls):
    """Turns the eval's target into a callable: (callable, name, parameter names)."""
    target = eval_cls._target
    if target is None:
        raise ValueError(f"{eval_cls.__name__}: no target, e.g. class {eval_cls.__name__}(Eval, target=Agent.method)")
    try:
        params = [p for p in inspect.signature(target).parameters if p != "self"]
    except (TypeError, ValueError):
        params = []
    if inspect.ismethod(target):
        return target, target.__qualname__, params
    qualname = getattr(target, "__qualname__", "")
    owner = getattr(target, "__agentflight_owner__", None)
    if isinstance(owner, type):
        name = f"{owner.__name__}.{target.__name__}"
        return (lambda *a, **k: getattr(owner(), target.__name__)(*a, **k)), name, params
    if "." in qualname and "<" not in qualname:
        class_name, method = qualname.rsplit(".", 1)
        for module_name in (getattr(target, "__module__", None), eval_cls.__module__):
            module = sys.modules.get(module_name) if module_name else None
            klass = getattr(module, class_name, None) if module else None
            if isinstance(klass, type):
                return (lambda *a, _klass=klass, **k: getattr(_klass(), method)(*a, **k)), qualname, params
    return target, qualname or repr(target), params


def _resolve_samples(eval_cls) -> list[tuple[object, object, bool]]:
    """(input, expected, has_expected) triples."""
    result = []
    if eval_cls.examples is not None:
        for ex in eval_cls.examples:
            if isinstance(ex, dict) and "input" in ex:
                result.append((ex["input"], ex.get("expected"), "expected" in ex))
            elif isinstance(ex, (tuple, list)) and len(ex) == 2:
                result.append((ex[0], ex[1], True))
            else:
                raise ValueError(f"{eval_cls.__name__}.examples: each example must be an (input, expected) pair")
    inputs = eval_cls.inputs
    if isinstance(inputs, str):
        module = sys.modules.get(eval_cls.__module__)
        base = Path(getattr(module, "__file__", ".") or ".").parent
        files = sorted(glob.glob(str(base / inputs)))
        if not files:
            raise ValueError(f"{eval_cls.__name__}.inputs: no file matches '{inputs}'")
        inputs = [Path(f).read_text(encoding="utf-8") for f in files]
    for value in inputs or []:
        result.append((value, None, False))
    if not result:
        raise ValueError(f"{eval_cls.__name__}: define 'inputs' or 'examples'")
    return result


def _call_target(target, params, value):
    """Dict inputs whose keys match the parameter names are passed as keyword arguments."""
    if isinstance(value, dict) and value and set(value) <= set(params):
        return target(**value)
    if isinstance(value, tuple) and len(params) > 1:
        return target(*value)
    return target(value)


def _equal(result, expected) -> bool:
    if isinstance(result, Schema) or hasattr(result, "model_dump"):
        result = to_jsonable(result)
    if isinstance(expected, Schema) or hasattr(expected, "model_dump"):
        expected = to_jsonable(expected)
    if isinstance(result, str) and isinstance(expected, str):
        return result.strip().casefold() == expected.strip().casefold()
    if isinstance(result, dict) and isinstance(expected, dict):
        return all(_equal(result.get(k), v) for k, v in expected.items())
    return result == expected


def _check_function(eval_cls):
    return eval_cls.__dict__.get("check")


def _run_check(eval_cls, value, result, expected):
    """Returns (passed, score, note)."""
    fn = _check_function(eval_cls)
    if fn is None:
        return True, 1.0, ""
    if isinstance(fn, (staticmethod, classmethod)):
        fn = fn.__func__
    params = [p for p in inspect.signature(fn).parameters if p not in ("self", "cls")]
    arguments = {"input": value, "result": result, "output": result, "expected": expected}
    try:
        if all(p in arguments for p in params):
            outcome = fn(**{p: arguments[p] for p in params})
        else:
            outcome = fn(*[value, result, expected][: len(params)])
    except AssertionError as e:
        return False, 0.0, str(e) or "assertion failed"
    if outcome is None or outcome is True:
        return True, 1.0, ""
    if outcome is False:
        return False, 0.0, "check returned False"
    if isinstance(outcome, (int, float)):
        score = float(outcome)
        return score >= 0.5, score, f"score {score:.2f}"
    return bool(outcome), 1.0 if outcome else 0.0, ""


class JudgeVerdict(Schema):
    passed: bool = field("true if the criterion is met, otherwise false")
    reason: str = field("A short reason for the verdict")


class Judge(Agent):
    """You are an impartial judge that evaluates an AI output against the given criterion.
    Look only at the criterion; give a short, clear verdict."""

    @llm
    def evaluate(self, criterion: str, input: str, output: str) -> JudgeVerdict: ...  # noqa: A002


def _ask_judge(eval_cls, value, result):
    settings = {"model": eval_cls.judge_model} if eval_cls.judge_model else {}
    verdict = Judge(**settings).evaluate(eval_cls.judge, str(to_jsonable(value)), str(to_jsonable(result)))
    if verdict is None:
        return False, "judge stopped"
    return verdict.passed, verdict.reason


def run_eval(eval_cls) -> dict:
    run = current_run()
    try:
        target, target_name, params = _resolve_target(eval_cls)
        samples = _resolve_samples(eval_cls)
    except ValueError as e:
        # A broken eval (no target, no inputs, a glob that matches nothing) fails on its own; the other evals of
        # the file still run.
        summary = {"name": eval_cls.__name__, "target": "?", "sample_count": 0, "passed_count": 0, "rate": 0.0,
                   "threshold": eval_cls.threshold, "passed": False, "cost": 0.0, "duration_ms": 0, "samples": [],
                   "error": str(e)}
        run.bus.publish("eval_started", name=eval_cls.__name__, target="?", n=0, threshold=eval_cls.threshold,
                        repeat=eval_cls.repeat)
        run.bus.publish("eval_finished", **{k: v for k, v in summary.items() if k != "samples"})
        return summary
    jobs = [(i, v, e, has_e) for i, (v, e, has_e) in enumerate(samples) for _ in range(max(1, int(eval_cls.repeat)))]
    run.bus.publish("eval_started", name=eval_cls.__name__, target=target_name, n=len(jobs),
                    threshold=eval_cls.threshold, repeat=eval_cls.repeat)
    t0 = time.monotonic()
    cost0 = run.total_cost

    def one(job):
        index, value, expected, has_expected = job
        start = time.monotonic()
        sample = {"index": index, "input": to_jsonable(value, truncate=300)}
        if has_expected:
            sample["expected"] = to_jsonable(expected)
        try:
            result = _call_target(target, params, value)
            sample["result"] = to_jsonable(result, truncate=600)
            if result is None:
                passed, score, note = False, 0.0, "call stopped"
            else:
                passed, score, note = _run_check(eval_cls, value, result, expected)
                if passed and has_expected and _check_function(eval_cls) is None and not _equal(result, expected):
                    passed, score, note = False, 0.0, "did not match the expected value"
                if passed and eval_cls.judge:
                    passed, reason = _ask_judge(eval_cls, value, result)
                    score = 1.0 if passed else 0.0
                    note = f"judge: {reason}"
        except Exception as e:  # noqa: BLE001 - counts as a failed sample
            passed, score, note = False, 0.0, f"{type(e).__name__}: {e}"
        sample.update(passed=passed, score=score, note=note, duration_ms=int((time.monotonic() - start) * 1000))
        run.bus.publish("eval_sample", name=eval_cls.__name__, **sample)
        return sample

    with ThreadPoolExecutor(max_workers=max(1, min(eval_cls.concurrency, len(jobs)))) as pool:
        results = list(pool.map(lambda j: contextvars.copy_context().run(one, j), jobs))

    passed_count = sum(1 for s in results if s["passed"])
    rate = passed_count / len(results) if results else 0.0
    summary = {
        "name": eval_cls.__name__, "target": target_name, "sample_count": len(results), "passed_count": passed_count,
        "rate": round(rate, 4), "threshold": eval_cls.threshold, "passed": rate >= eval_cls.threshold,
        "cost": round(run.total_cost - cost0, 6), "duration_ms": int((time.monotonic() - t0) * 1000),
        "samples": results,
    }
    run.bus.publish("eval_finished", **{k: v for k, v in summary.items() if k != "samples"})
    return summary
