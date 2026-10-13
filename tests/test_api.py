"""Public API: agent settings, schemas, fields, tools, parallel calls, instructions, evals and approvals; Pydantic
models; async @llm methods; the checker; the `agentflight new` template; and the watch panel picking up plain
`python main.py` runs."""

# No `from __future__ import annotations` here: the agents and schemas below are defined inside test methods, so
# their annotations must be real objects (string annotations could not be resolved from a function's locals).

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import agentflight
from agentflight import (
    Agent,
    ApprovalRejected,
    Eval,
    Schema,
    SchemaError,
    field,
    llm,
    parallel,
    parallel_map,
    require_approval,
    tool,
)
from agentflight.checker import check_source
from agentflight.evals import run_eval
from agentflight.runtime import Run, set_current_run
from agentflight.schema import output_schema, parse_output
from tests.helpers import EventCollector, cleanup, run_cli, temp_project

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = None


class RunTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = temp_project({})
        self.run = Run(model=None, approval="auto", default_model="fake", project=self.folder)
        self.events = EventCollector()
        self.run.bus.add(self.events)
        set_current_run(self.run)

    def tearDown(self):
        set_current_run(None)
        cleanup(self.folder)


class Api(RunTestCase):
    def test_agent_settings_schema_field_tool_parallel(self):
        notes = []

        @tool
        def take_note(text: str) -> str:
            """Writes a note.

            text: the note
            """
            notes.append(text)
            return "saved"

        class Summary(Schema):
            title: str
            bullets: list[str] = field("key points", min=1, max=4)
            score: int = field("quality", min=1, max=10)

        class Summarizer(Agent):
            """You summarize texts."""
            temperature = 0
            tools = [take_note]
            budget = 1.0
            max_turns = 5
            retries = 1

            @llm
            def summarize(self, text: str) -> Summary: ...

            @llm
            def one_line(self, text: str) -> str: ...

        a = Summarizer()
        self.assertEqual((a.temperature, a.budget, a.max_turns, a.retries), (0, 1.0, 5, 1))
        a.temperature = 0.5
        self.assertEqual(a.temperature, 0.5)
        self.assertIs(Summarizer(memory=True).memory, True)
        with self.assertRaises(TypeError) as cm:
            Summarizer(temprature=1)
        self.assertIn("unknown setting", str(cm.exception))

        text = ("Biogas is made from organic waste. Its methane share is between fifty and seventy percent. "
                "It is cleaned before use.")
        s = a.summarize(text)
        self.assertIsInstance(s, Summary)
        self.assertTrue(1 <= s.score <= 10)
        self.assertEqual(s.to_dict()["title"], s.title)
        self.assertTrue(notes, "the tool was called")
        system = self.events.of_type("call_started")[0]["system"]
        self.assertIn("## Task", system)
        self.assertIn("## Answer format", system)

        first, second = parallel(lambda: a.one_line(text), lambda: a.one_line(text + " More."))
        self.assertTrue(first and second)
        self.assertEqual(len(parallel_map(a.one_line, [text, text])), 2)
        self.assertEqual(self.events.of_type("parallel")[0]["n"], 2)

    def test_instructions_attribute_and_missing_file(self):
        class Greeter(Agent):
            instructions = "You greet people warmly in one sentence."

            @llm
            def greet(self, name: str) -> str: ...

        self.assertEqual(Greeter.instructions_text(), "You greet people warmly in one sentence.")
        self.assertTrue(Greeter().greet("Ada"))

        class Missing(Agent):
            """prompts/does_not_exist.md"""

            @llm
            def f(self, x: str) -> str: ...

        with self.assertRaises(agentflight.InstructionsNotFound) as cm:
            Missing().f("x")
        self.assertIn("instructions file for Missing not found", str(cm.exception))

    def test_schema_error_messages(self):
        class Point(Schema):
            x: int = field(min=0)

        with self.assertRaises(SchemaError) as cm:
            parse_output(Point, '{"x": -1}')
        self.assertIn("must be at least 0", str(cm.exception))
        with self.assertRaises(SchemaError) as cm:
            parse_output(Point, '{"y": 1}')
        self.assertIn("required field missing", str(cm.exception))
        self.assertEqual(output_schema(int)["required"], ["result"])
        self.assertEqual(parse_output(int, '{"result": 5}'), 5)

    def test_eval(self):
        class Classifier(Agent):
            """Classify."""

            @llm
            def classify(self, text: str) -> str: ...

        class Quality(Eval, target=Classifier.classify):
            inputs = ["first input text here.", "second input text here."]
            repeat = 2
            threshold = 0.5

            @staticmethod
            def check(input, result):
                assert isinstance(result, str)

        report = run_eval(Quality)
        self.assertTrue(all(s["passed"] for s in report["samples"]), report["samples"])
        self.assertEqual((report["sample_count"], report["threshold"]), (4, 0.5))
        self.assertTrue(report["passed"])

    def test_require_approval(self):
        self.run.approval_mode = "auto"
        self.assertEqual(require_approval({"a": 1}, "ok?"), {"a": 1})
        self.run.approval_mode = "panel"
        requests = []

        def answer(event):
            if event["type"] == "approval_requested":
                requests.append(event["approval_id"])
                self.run.control.command({"command": "approve", "approval_id": event["approval_id"],
                                          "decision": "reject"})

        self.run.bus.add(answer)
        with self.assertRaises(ApprovalRejected):
            require_approval("x", "Publish?")
        self.assertEqual(len(requests), 1)


@unittest.skipIf(BaseModel is None, "pydantic is not installed")
class PydanticSupport(RunTestCase):
    def test_pydantic_return_type_tool_parameter_and_validation(self):
        class Address(BaseModel):
            city: str
            zip_code: str = Field(description="postal code")

        class Person(BaseModel):
            """A person found in the text."""
            name: str = Field(description="full name")
            age: int = Field(ge=0, le=150)
            addresses: list[Address] = Field(default_factory=list, max_length=3)

        schema = output_schema(Person)
        self.assertNotIn("$defs", json.dumps(schema))  # references inlined for providers
        self.assertEqual(schema["properties"]["addresses"]["items"]["properties"]["city"]["type"], "string")
        strict = output_schema(Person, strict=True)
        self.assertNotIn("maximum", json.dumps(strict))
        self.assertIn("at most 150", strict["properties"]["age"]["description"])

        p = parse_output(Person, '{"name": "Ada", "age": 36, "addresses": [{"city": "London", "zip_code": "N1"}]}')
        self.assertIsInstance(p, Person)
        self.assertEqual(p.addresses[0].city, "London")
        with self.assertRaises(SchemaError) as cm:
            parse_output(Person, '{"name": "Ada", "age": 200}')
        self.assertIn("age", str(cm.exception))

        seen = []

        @tool
        def save_person(person: Person) -> str:
            """Saves a person."""
            seen.append(person)
            return "ok"

        self.assertEqual(save_person.schema["properties"]["person"]["properties"]["age"]["type"], "integer")
        save_person.run({"person": {"name": "Bob", "age": 3}})
        self.assertIsInstance(seen[0], Person)

        class Extractor(Agent):
            """Extract the person."""

            @llm
            def extract(self, text: str) -> Person: ...

        result = Extractor().extract("Ada Lovelace was thirty six years old and lived in London for many years.")
        self.assertIsInstance(result, Person)
        finished = self.events.of_type("call_finished")[-1]
        self.assertEqual(set(finished["result"]), {"name", "age", "addresses"})  # recorded as JSON


class AsyncSupport(RunTestCase):
    def test_async_llm_method(self):
        class Writer(Agent):
            """You write one sentence."""

            @llm
            async def write(self, topic: str) -> str: ...

        async def both():
            return await asyncio.gather(Writer().write("bees and honey production"),
                                        Writer().write("heat pumps for homes"))

        results = asyncio.run(both())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(r, str) and r for r in results))
        self.assertEqual(len(self.events.of_type("call_finished")), 2)


class Checker(unittest.TestCase):
    def test_checker_knows_the_api_names(self):
        source = '''from agentflight import Agent, Eval, Schema, llm


class Out(Schema):
    text: str


class A(Agent):
    """t"""
    tools = [missing_tool]
    temprature = 0

    @llm
    def f(self, x: str) -> Out: ...


class E(Eval, target=A.nope):
    threshold = 2
'''
        messages = [d["message"] for d in check_source(source, "x.py")]
        self.assertTrue(any("no tool named 'missing_tool'" in m for m in messages), messages)
        self.assertTrue(any("did you mean 'temperature'" in m for m in messages), messages)
        self.assertTrue(any("has no method named 'nope'" in m for m in messages), messages)
        self.assertTrue(any("define 'inputs' or 'examples'" in m for m in messages), messages)
        self.assertTrue(any("threshold must be between 0 and 1" in m for m in messages), messages)


class NewProject(unittest.TestCase):
    def test_new_project_template_runs(self):
        folder = Path(tempfile.mkdtemp(prefix="agentflight-new-")) / "demo"
        try:
            r = run_cli("new", str(folder))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((folder / "prompts" / "assistant.md").is_file())
            self.assertIn("[project]", (folder / "agentflight.toml").read_text(encoding="utf-8"))
            r = run_cli("check", "main.py", cwd=folder)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("no problems", r.stdout)
            r = run_cli("run", "main.py", "--model", "fake", cwd=folder)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("confidence", r.stdout)
            self.assertIn("1 agent call ", r.stderr)
            r = run_cli("eval", "main.py", "--model", "fake", cwd=folder)
            self.assertIn("EVAL RESULTS", r.stdout)
            self.assertIn("ShortAnswers", r.stdout)
        finally:
            cleanup(folder.parent)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PanelPicksUpPlainPython(unittest.TestCase):
    def test_python_main_connects_records_and_mirrors_output(self):
        home = tempfile.mkdtemp(prefix="agentflight-panel-")
        env = {**os.environ, "AGENTFLIGHT_HOME": home, "PYTHONIOENCODING": "utf-8"}
        env.pop("AGENTFLIGHT_QUIET", None)
        folder = temp_project({"main.py": '''
            from agentflight import Agent, llm

            class Greeter(Agent):
                """Greet in one sentence."""

                @llm
                def greet(self, name: str) -> str: ...

            print("OUT:", Greeter().greet("a curious engineer who builds boilers"))
        '''})
        command = [sys.executable, "-m", "agentflight", "watch", "--no-browser", "--port", str(_free_port())]
        log = Path(home) / "panel.log"
        with open(log, "w", encoding="utf-8") as log_file:
            panel = subprocess.Popen(command, env=env, stdout=log_file, stderr=subprocess.STDOUT)
        try:
            discovery = Path(home) / "panel.json"
            end = time.time() + 30
            while not discovery.exists() and time.time() < end and panel.poll() is None:
                time.sleep(0.1)
            self.assertTrue(discovery.exists(), "the panel did not start:\n" + log.read_text(encoding="utf-8"))
            info = json.loads(discovery.read_text(encoding="utf-8"))
            r = subprocess.run([sys.executable, "main.py"], cwd=folder, env={**env, "AGENTFLIGHT_MODEL": "fake"},
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("connected to the AgentFlight panel", r.stderr)
            self.assertIn("OUT:", r.stdout)  # the terminal output is unchanged
            runs = list((folder / ".agentflight" / "runs").iterdir())
            self.assertEqual(len(runs), 1, "the run was recorded")
            summary = json.loads((runs[0] / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual((summary["status"], summary["call_count"], summary["env_model"]), ("done", 1, "fake"))
            with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/api/runs") as response:
                listed = json.load(response)
            self.assertTrue(any(item["run_id"] == runs[0].name for item in listed))
            # with AGENTFLIGHT_PANEL=0 nothing connects and nothing is recorded
            r = subprocess.run([sys.executable, "main.py"], cwd=folder,
                               env={**env, "AGENTFLIGHT_MODEL": "fake", "AGENTFLIGHT_PANEL": "0"},
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("connected to the AgentFlight panel", r.stderr)
            self.assertEqual(len(list((folder / ".agentflight" / "runs").iterdir())), 1)
        finally:
            subprocess.run([sys.executable, "-m", "agentflight", "watch", "--stop"], env=env, capture_output=True)
            try:
                panel.wait(10)
            except subprocess.TimeoutExpired:
                panel.kill()
            cleanup(folder)
            cleanup(Path(home))


if __name__ == "__main__":
    unittest.main()
