"""Plain Python programs: running them, the call-site locations of the live view, evals, the static checker and
the error report."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest

from agentflight.checker import check_source
from agentflight.runner import _load_module
from agentflight.runtime import Run, set_current_run
from tests.helpers import EventCollector, cleanup, run_cli, temp_project

PROGRAM = '''
from typing import Literal

from agentflight import Agent, Eval, Schema, field, llm, parallel, tool


class Summary(Schema):
    title: str
    bullets: list[str] = field("bullets", min=1, max=4)
    sentiment: Literal["positive", "negative", "neutral"]


notes = []


@tool
def take_note(text: str) -> str:
    """Writes a note into the notebook."""
    notes.append(text)
    return "saved"


class Summarizer(Agent):
    """You summarize texts."""

    @llm
    def summarize(self, text: str) -> Summary: ...

    @llm
    def one_sentence(self, text: str) -> str: ...

    def helper(self) -> int:
        return 42


class NoteTaker(Agent):
    """You take notes."""
    tools = [take_note]

    @llm
    def process(self, text: str) -> str: ...


class SummaryRule(Eval, target=Summarizer.summarize):
    inputs = ["Biogas is made from organic waste. It contains methane.",
              "A heat pump moves heat around. Its efficiency is high."]
    threshold = 0.5

    @staticmethod
    def check(input, result):
        assert 1 <= len(result.bullets) <= 4


TEXT = "Biogas is produced from organic waste. Its methane share is between 50 and 70 percent."

if __name__ == "__main__":
    summary = Summarizer().summarize(TEXT)
    a, b = parallel(lambda: Summarizer().one_sentence(TEXT), lambda: NoteTaker().process(TEXT))
    print("TITLE:", summary.title)
    print("HELPER:", Summarizer().helper())
    print("PROGRAM_DONE")
'''


class PythonProgram(unittest.TestCase):
    def setUp(self):
        self.folder = temp_project({"main.py": PROGRAM})

    def tearDown(self):
        set_current_run(None)
        cleanup(self.folder)

    def test_cli_run_eval_check(self):
        r = run_cli("run", "main.py", cwd=self.folder)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TITLE:", r.stdout)
        self.assertIn("HELPER: 42", r.stdout)
        self.assertIn("PROGRAM_DONE", r.stdout)

        r = run_cli("eval", "main.py", "--json", "result.json", cwd=self.folder)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        report = json.loads((self.folder / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(report["evals"][0]["name"], "SummaryRule")
        self.assertEqual(report["evals"][0]["sample_count"], 2)
        self.assertNotIn("PROGRAM_DONE", r.stdout)  # an eval does not run the program's main block

        r = run_cli("check", "main.py", cwd=self.folder)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_live_view_location_and_parallel(self):
        run = Run(model=None, approval="auto", default_model="fake", project=self.folder)
        events = EventCollector()
        run.bus.add(events)
        set_current_run(run)
        old_cwd, old_main = os.getcwd(), sys.modules.get("__main__")
        os.chdir(self.folder)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                _load_module(self.folder / "main.py", "__main__", only_definitions=False)
        finally:
            os.chdir(old_cwd)
            if old_main is not None:
                sys.modules["__main__"] = old_main
        self.assertIn("PROGRAM_DONE", output.getvalue())
        started = events.of_type("call_started")
        self.assertEqual(len(started), 3)
        lines = PROGRAM.lstrip("\n").splitlines()
        expected = next(i for i, s in enumerate(lines, 1) if "summary = Summarizer().summarize(TEXT)" in s)
        self.assertTrue(started[0]["file"].endswith("main.py"))
        self.assertEqual(started[0]["line"], expected)
        parallel = events.of_type("parallel")[0]
        self.assertEqual(parallel["n"], 2)
        self.assertEqual(parallel["line"], expected + 1)
        self.assertTrue(events.of_type("tool_finished"))

    def test_error_report_shows_the_user_line(self):
        (self.folder / "p.py").write_text("x = 1\ny = x / 0\n", encoding="utf-8")
        r = run_cli("run", "p.py", cwd=self.folder)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ZeroDivisionError (p.py:2)", r.stderr)


class PythonChecker(unittest.TestCase):
    def diagnostics(self, source):
        return [(d["severity"], d["message"]) for d in check_source(source, "x.py")]

    def test_warns_when_llm_decorator_is_missing(self):
        d = self.diagnostics('from agentflight import Agent\n\nclass A(Agent):\n    """t"""\n'
                             '    def f(self, x: str) -> str: ...\n')
        self.assertTrue(any(s == "warning" and "@llm" in m for s, m in d), d)

    def test_types_and_eval_target_are_checked(self):
        source = ('from agentflight import Agent, Eval, llm\n\nclass A(Agent):\n    """t"""\n    @llm\n'
                  '    def f(self, x: str) -> Smry: ...\n\n'
                  'class E(Eval, target=A.missing):\n    inputs = ["a"]\n')
        d = self.diagnostics(source)
        self.assertTrue(any("unknown type 'Smry'" in m for _, m in d), d)
        self.assertTrue(any("has no method named 'missing'" in m for _, m in d), d)


if __name__ == "__main__":
    unittest.main()
