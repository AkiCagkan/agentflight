"""Agent calls at runtime: events, schemas, tools, live control, approvals, budget, recording, replay and the CLI.

Everything runs on the offline fake model; no API key is needed."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

from agentflight import pricing
from agentflight.errors import AgentError, ApprovalRejected, BudgetExceeded, SchemaError
from agentflight.flow import parallel_map, require_approval
from agentflight.prompts import INTERJECTION_PREFIX
from agentflight.recording import Recorder, ReplayStore, runs_dir
from agentflight.runner import _load_module
from agentflight.runtime import Run, set_current_run
from tests.helpers import EventCollector, cleanup, in_background, run_cli, temp_project, wait_until

PROGRAM = '''
from agentflight import Agent, Schema, field, llm, parallel, tool


class Summary(Schema):
    title: str
    bullets: list[str] = field("bullets", min=1, max=4)
    score: int = field("score", min=1, max=10)


notebook = []


@tool
def take_note(text: str) -> str:
    """Writes a note into the notebook."""
    notebook.append(text)
    return "saved"


class Summarizer(Agent):
    """You summarize texts."""

    @llm
    def summarize(self, text: str) -> Summary: ...

    @llm
    def one_sentence(self, text: str) -> str: ...


class NoteTaker(Agent):
    """You take notes."""
    tools = [take_note]

    @llm
    def process(self, text: str) -> str: ...


@tool
def sub_summary(text: str) -> str:
    """Summarizes with a sub-agent."""
    return Summarizer().one_sentence(text)


class Manager(Agent):
    """You hand work out to sub-agents."""
    tools = [sub_summary]

    @llm
    def manage(self, text: str) -> str: ...


class WithMemory(Agent):
    """You remember the conversation."""
    memory = True

    @llm
    def ask(self, question: str) -> str: ...


TEXT = ("Biogas is produced from organic waste. Its methane share is between 50 and 70 percent. "
        "The gas is cleaned before use.")


def summarize_here():
    return Summarizer().summarize(TEXT)
'''


class RunTestCase(unittest.TestCase):
    """Loads PROGRAM as a module inside an in-process Run that collects every event."""

    def setUp(self):
        os.environ["AGENTFLIGHT_FAKE_DELAY"] = "0"
        self.folder = temp_project({"prog.py": PROGRAM})
        self.run = Run(model=None, approval="auto", default_model="fake", project=self.folder)
        self.events = EventCollector()
        self.run.bus.add(self.events)
        set_current_run(self.run)
        self.module_names = [f"prog_{id(self)}"]
        self.m = _load_module(self.folder / "prog.py", self.module_names[0], only_definitions=False)

    def tearDown(self):
        set_current_run(None)
        os.environ["AGENTFLIGHT_FAKE_DELAY"] = "0"
        pricing._EXTRA.clear()
        for name in self.module_names:
            sys.modules.pop(name, None)
        cleanup(self.folder)

    def wait(self, condition, timeout=10):
        if not wait_until(condition, timeout):
            self.fail("the condition was not met in time")


class AgentCalls(RunTestCase):
    def test_typed_result_and_events(self):
        summary = self.m.summarize_here()
        self.assertIsInstance(summary, self.m.Summary)
        self.assertTrue(1 <= summary.score <= 10)
        started = self.events.of_type("call_started")[0]
        self.assertEqual((started["agent"], started["method"], started["step"]), ("Summarizer", "summarize", 1))
        self.assertTrue(started["file"].endswith("prog.py"))
        lines = (self.folder / "prog.py").read_text(encoding="utf-8").splitlines()
        expected = next(i for i, s in enumerate(lines, 1) if "return Summarizer().summarize(TEXT)" in s)
        self.assertEqual(started["line"], expected)
        self.assertTrue(self.events.of_type("token"))
        finished = self.events.of_type("call_finished")[0]
        self.assertEqual(finished["status"], "done")
        self.assertEqual(finished["result"]["score"], summary.score)

    def test_format_error_is_fixed_automatically(self):
        self.run.model_override = "fake:bad"
        summary = self.m.Summarizer().summarize(self.m.TEXT)
        self.assertIsInstance(summary.score, int)
        errors = self.events.of_type("format_error")
        self.assertEqual(len(errors), 1)
        self.assertIn("score: expected int", errors[0]["errors"][0])
        messages = self.events.of_type("call_finished")[0]["messages"]
        self.assertTrue(any(m["role"] == "user" and "[Format error]" in m["content"] for m in messages))

    def test_schema_error_when_retries_run_out(self):
        self.run.model_override = "fake:bad"
        with self.assertRaises(SchemaError) as cm:
            self.m.Summarizer(retries=0).summarize(self.m.TEXT)
        self.assertIn("correction attempts", str(cm.exception))
        self.assertEqual(self.events.of_type("call_finished")[0]["status"], "error")

    def test_input_type_is_checked(self):
        with self.assertRaises(SchemaError):
            self.m.Summarizer().summarize(["a", "list", "not", "a", "text"])

    def test_unknown_setting(self):
        with self.assertRaises(TypeError):
            self.m.Summarizer(modl="x")

    def test_tool_call(self):
        self.m.NoteTaker().process("Meeting note: boiler maintenance is on Tuesday.")
        self.assertEqual(len(self.m.notebook), 1)
        started, finished = self.events.of_type("tool_started")[0], self.events.of_type("tool_finished")[0]
        self.assertEqual(started["name"], "take_note")
        self.assertFalse(finished["error"])
        self.assertEqual(finished["result"], "saved")

    def test_tool_arguments_are_validated(self):
        with self.assertRaises(AgentError):
            self.m.take_note.run({"text": ["a list"]})
        with self.assertRaises(AgentError):
            self.m.take_note.run({})
        self.assertEqual(self.m.take_note("called directly"), "saved")

    def test_nested_agent_parent_link(self):
        self.m.Manager().manage(self.m.TEXT)
        calls = self.events.of_type("call_started")
        manager = next(e for e in calls if e["agent"] == "Manager")
        child = next(e for e in calls if e["agent"] == "Summarizer")
        self.assertEqual(child["parent"], manager["id"])

    def test_memory(self):
        a = self.m.WithMemory()
        a.ask("My name is Ada.")
        a.ask("What was my name?")
        second = self.events.of_type("call_finished")[1]["messages"]
        self.assertGreaterEqual(len(second), 4)
        self.assertIn("My name is Ada.", second[0]["content"])


class Parallel(RunTestCase):
    def test_parallel(self):
        source = PROGRAM + '''

first, second, third = parallel(
    lambda: Summarizer().one_sentence("The first text is right here."),
    lambda: Summarizer().one_sentence("The second text is right here."),
    lambda: Summarizer().one_sentence("The third text is right here."),
)
results = [first, second, third]
'''
        (self.folder / "p.py").write_text(source, encoding="utf-8")
        self.module_names.append("p_module")
        m = _load_module(self.folder / "p.py", "p_module", only_definitions=False)
        self.assertEqual(len(m.results), 3)
        self.assertIn("first", m.results[0])
        self.assertIn("third", m.results[2])
        self.assertEqual(self.events.of_type("parallel")[0]["n"], 3)

    def test_parallel_map(self):
        results = parallel_map(self.m.Summarizer().one_sentence,
                               ["Sentence number one is here.", "Sentence number two is here."])
        self.assertIn("number one", results[0])
        self.assertIn("number two", results[1])


class LiveControl(RunTestCase):
    def setUp(self):
        super().setUp()
        os.environ["AGENTFLIGHT_FAKE_DELAY"] = "0.03"

    def _first_call(self):
        self.wait(lambda: self.events.of_type("token"))
        return self.events.of_type("call_started")[0]["id"]

    def test_pause_and_resume(self):
        thread, result = in_background(lambda: self.m.Summarizer().summarize(self.m.TEXT))
        call_id = self._first_call()
        self.run.control.command({"command": "pause", "id": call_id})
        self.wait(lambda: any(e.get("state") == "paused" for e in self.events.of_type("state")))
        count = len(self.events.of_type("token"))
        time.sleep(0.4)
        self.assertEqual(len(self.events.of_type("token")), count, "no tokens may arrive while paused")
        self.run.control.command({"command": "resume", "id": call_id})
        thread.join(20)
        self.assertIsInstance(result.get("value"), self.m.Summary)

    def test_kill(self):
        thread, result = in_background(lambda: self.m.Summarizer().summarize(self.m.TEXT))
        call_id = self._first_call()
        self.run.control.command({"command": "kill", "id": call_id})
        thread.join(10)
        self.assertNotIn("error", result)
        self.assertIsNone(result["value"])
        self.assertEqual(self.events.of_type("call_finished")[0]["status"], "stopped")

    def test_interjected_message(self):
        thread, result = in_background(lambda: self.m.Summarizer().one_sentence(self.m.TEXT))
        call_id = self._first_call()
        self.run.control.command({"command": "message", "id": call_id, "text": "make it shorter"})
        thread.join(20)
        self.assertIn("make it shorter", result["value"])
        messages = self.events.of_type("call_finished")[0]["messages"]
        self.assertTrue(any(m["role"] == "user" and m["content"].startswith(INTERJECTION_PREFIX) for m in messages))
        self.assertEqual(self.events.of_type("message_added")[0]["text"], "make it shorter")


class Approval(RunTestCase):
    def _wait_for_request(self, value):
        self.run.approval_mode = "panel"
        thread, result = in_background(lambda: require_approval(value, "Continue?"))
        self.wait(lambda: self.events.of_type("approval_requested"))
        return thread, result, self.events.of_type("approval_requested")[0]["approval_id"]

    def test_auto(self):
        self.assertEqual(require_approval(5, "is this fine?"), 5)

    def test_reject(self):
        thread, result, approval_id = self._wait_for_request("text")
        self.run.control.command({"command": "approve", "approval_id": approval_id, "decision": "reject"})
        thread.join(5)
        self.assertIsInstance(result["error"], ApprovalRejected)

    def test_edit_is_validated_again(self):
        summary = self.m.Summary(title="a", bullets=["x"], score=3)
        thread, result, approval_id = self._wait_for_request(summary)
        self.run.control.command({"command": "approve", "approval_id": approval_id, "decision": "edit",
                                  "value": {"title": "new", "bullets": ["y"], "score": "9"}})
        thread.join(5)
        self.assertEqual(result["value"].title, "new")
        self.assertEqual(result["value"].score, 9)


class BudgetAndRecording(RunTestCase):
    def test_budget(self):
        pricing.add_prices({"fake": (1_000_000, 1_000_000)})
        a = self.m.Summarizer(budget=0.01)
        a.one_sentence(self.m.TEXT)
        with self.assertRaises(BudgetExceeded):
            a.one_sentence(self.m.TEXT)

    def test_recording_and_replay_from_a_step(self):
        folder = runs_dir(self.folder) / "r1"
        recorder = Recorder(folder, {"run_id": "r1"})
        self.run.recorder = recorder
        self.run.bus.add(recorder.event)
        self.m.Summarizer().one_sentence("The text of the first step is here.")
        self.m.Summarizer().one_sentence("The text of the second step is here.")
        recorder.close({"status": "done"})
        lines = (folder / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)

        replay = Run(approval="auto", default_model="fake", project=self.folder,
                     replay=ReplayStore.from_run(self.folder, "r1", step=2))
        events = EventCollector()
        replay.bus.add(events)
        set_current_run(replay)
        self.m.Summarizer().one_sentence("The text of the first step is here.")
        self.m.Summarizer().one_sentence("The text of the second step is here.")
        finished = events.of_type("call_finished")
        self.assertTrue(finished[0]["from_record"])
        self.assertFalse(finished[1]["from_record"])
        self.assertNotEqual(events.of_type("token")[0]["id"], finished[0]["id"])


class CommandLine(unittest.TestCase):
    def test_run_check_eval(self):
        folder = temp_project({
            "instructions.md": "Keep it short.",
            "prog.py": '''
                from typing import Literal

                from agentflight import Agent, Eval, Schema, llm


                class Result(Schema):
                    label: Literal["a", "b"]


                class A(Agent):
                    """instructions.md"""

                    @llm
                    def f(self, x: str) -> Result: ...


                class E1(Eval, target=A.f):
                    inputs = ["about a", "about b"]

                    @staticmethod
                    def check(input, result):
                        assert result.label in ("a", "b")


                print("RESULT:", A().f("a").label)
            ''',
            "broken.py": '''
                from agentflight import Agent, Eval, llm


                class B(Agent):
                    """missing.md"""
                    model = "unknown-model"
                    tools = [undefined_tool]

                    @llm
                    def f(self, x: str): ...

                    @llm
                    def g(self, x: Unknown) -> str: ...


                class E2(Eval, target=B.nonexistent):
                    repeat = 2


                B().f()
                B(modl="x")
            ''',
        })
        try:
            r = run_cli("run", "prog.py", cwd=folder)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("RESULT:", r.stdout)
            self.assertTrue(any((folder / ".agentflight" / "runs").iterdir()))

            r = run_cli("check", "broken.py", cwd=folder)
            self.assertEqual(r.returncode, 1)
            for expected in ("instructions file not found", "unknown model 'unknown-model'",
                             "no tool named 'undefined_tool' is defined", "needs a return type",
                             "unknown type 'Unknown'", "has no method named 'nonexistent'",
                             "define 'inputs' or 'examples'", "missing argument: x", "unknown setting 'modl'"):
                self.assertIn(expected, r.stdout)

            r = run_cli("run", "broken.py", cwd=folder)
            self.assertEqual(r.returncode, 2)

            r = run_cli("eval", "prog.py", "--json", "result.json", cwd=folder)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            report = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(report["evals"][0]["sample_count"], 2)
            self.assertNotIn("RESULT:", r.stdout, "eval mode must not run the program code")
        finally:
            cleanup(folder)

    def test_program_error_shows_the_user_line(self):
        folder = temp_project({"p.py": "x = 1\ny = x / 0\n"})
        try:
            r = run_cli("run", "p.py", cwd=folder)
            self.assertEqual(r.returncode, 1)
            self.assertIn("ZeroDivisionError (p.py:2)", r.stderr)
        finally:
            cleanup(folder)

    def test_terminal_approval_rejected(self):
        folder = temp_project({"p.py": 'from agentflight import require_approval\n\n'
                                       'require_approval("x", "continue?")\nprint("PASSED")\n'})
        try:
            r = run_cli("run", "p.py", cwd=folder, input="n\n")
            self.assertEqual(r.returncode, 1)
            self.assertNotIn("PASSED", r.stdout)
            r = run_cli("run", "p.py", "--approve", "auto", cwd=folder)
            self.assertIn("PASSED", r.stdout)
        finally:
            cleanup(folder)


if __name__ == "__main__":
    unittest.main()
