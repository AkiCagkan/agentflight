"""Edge cases: malformed model answers, misbehaving tools and providers, odd inputs, broken files on disk.

Everything runs offline: a scripted provider stands in for the model where a test needs exact answers.
"""

import asyncio
import io
import json
import socket
import sys
import threading
import unittest
from typing import Literal, Union
from unittest import mock

import agentflight.agents as agents_module
from agentflight import Agent, Schema, field, llm, parallel, parallel_map, require_approval, tool
from agentflight.errors import (
    ApprovalRejected,
    BudgetExceeded,
    InstructionsNotFound,
    MaxTurnsExceeded,
    ProviderError,
    SchemaError,
    UnknownModel,
)
from agentflight.panel.client import OutputMirror
from agentflight.providers import fake as fake_provider_module
from agentflight.providers.base import Provider, Response, ToolCall
from agentflight.recording import Recorder, read_run
from agentflight.runtime import Run, set_current_run
from agentflight.schema import convert, extract_json, parse_output, to_jsonable
from tests.helpers import EventCollector, cleanup, run_cli, temp_project


class Item(Schema):
    name: str
    qty: int = field("quantity", min=0)


class Box(Schema):
    items: list[Item]
    note: str | None = None
    kind: Literal["small", "large"] = "small"
    tags: list[str] = []


class SchemaEdges(unittest.TestCase):
    def test_nested_error_paths(self):
        with self.assertRaises(SchemaError) as ctx:
            Box.validate({"items": [{"name": "a", "qty": 1}, {"name": "b", "qty": "many"}, {"qty": 2}]})
        errors = ctx.exception.errors
        self.assertTrue(any(e.startswith("items[1].qty: expected int") for e in errors), errors)
        self.assertTrue(any(e.startswith("items[2].name: required field missing") for e in errors), errors)

    def test_optional_default_literal_and_unknown_fields(self):
        box = Box.validate({"items": [], "kind": " LARGE "})
        self.assertIsNone(box.note)
        self.assertEqual(box.kind, "large")
        with self.assertRaises(SchemaError) as ctx:
            Box.validate({"items": [], "colour": "red"})
        self.assertIn("unknown field(s): colour", str(ctx.exception))

    def test_mutable_defaults_are_not_shared(self):
        first = Box.validate({"items": []})
        first.tags.append("x")
        self.assertEqual(Box.validate({"items": []}).tags, [])

    def test_number_coercion_rules(self):
        self.assertEqual(Item.validate({"name": "a", "qty": " 7 "}).qty, 7)
        self.assertEqual(Item.validate({"name": "a", "qty": 7.0}).qty, 7)
        for bad in (True, 7.5, "seven"):
            with self.assertRaises(SchemaError):
                Item.validate({"name": "a", "qty": bad})
        with self.assertRaises(SchemaError) as ctx:
            Item.validate({"name": "a", "qty": -1})
        self.assertIn("must be at least 0", str(ctx.exception))

    def test_union_tries_members_in_order(self):
        errors: list[str] = []
        self.assertEqual([convert(Union[int, str], v, "v", errors) for v in (1, "x", "3")], [1, "x", 3])
        self.assertEqual(errors, [])

    def test_extract_json_from_chatty_answers(self):
        self.assertEqual(extract_json('Sure!\n```json\n{"a": "}"}\n```\nThanks'), {"a": "}"})
        self.assertEqual(extract_json('the list: [1, 2, {"b": [3]}] done'), [1, 2, {"b": [3]}])
        with self.assertRaises(ValueError):
            extract_json("no json here {oops")

    def test_parse_output_for_plain_types(self):
        self.assertEqual(parse_output(str, "  hello  "), "hello")
        self.assertEqual(parse_output(list[int], '{"result": [1, "2"]}'), [1, 2])
        self.assertIs(parse_output(bool, '{"result": "yes"}'), True)
        self.assertEqual(parse_output(int, "42"), 42)

    def test_unicode_survives_json(self):
        item = Item.validate({"name": "Çağkan 🚀 ğüşıöç", "qty": 1})
        self.assertEqual(json.loads(item.to_json())["name"], "Çağkan 🚀 ğüşıöç")
        self.assertIn("🚀", item.to_json())

    def test_to_jsonable_truncates_and_handles_odd_values(self):
        self.assertTrue(to_jsonable("x" * 50, truncate=10).startswith("x" * 10 + "…"))
        self.assertEqual(to_jsonable({1: {2}}), {"1": [2]})
        self.assertIsInstance(to_jsonable(object()), str)


class Scripted(Provider):
    """A provider that answers from a list (an exception in the list is raised)."""

    name = "scripted"

    def __init__(self, answers, default=None):
        self.answers = list(answers)
        self.default = default
        self.requests = []

    def call(self, request, on_text):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else self.default
        if answer is None:
            raise AssertionError("the scripted provider ran out of answers")
        if isinstance(answer, Exception):
            raise answer
        on_text(answer.text)
        return answer


@tool
def echo(text: str) -> str:
    """Returns the text."""
    return text


@tool
def explode(text: str) -> str:
    """Always fails."""
    raise RuntimeError("tool blew up")


class Worker(Agent):
    """You work."""
    tools = [echo, explode]
    max_turns = 3

    @llm
    def work(self, text: str) -> str: ...


class Counter(Agent):
    """You count."""

    @llm
    def count(self) -> int: ...


class ProviderAndToolEdges(unittest.TestCase):
    def setUp(self):
        self.events = EventCollector()
        self.run = Run(approval="auto", default_model="fake")
        self.run.bus.add(self.events)
        set_current_run(self.run)

    def tearDown(self):
        set_current_run(None)

    def script(self, answers, default=None):
        provider = Scripted(answers, default)
        patcher = mock.patch.object(agents_module, "find_provider", lambda model: (provider, "scripted-model"))
        patcher.start()
        self.addCleanup(patcher.stop)
        return provider

    def tool_result(self):
        return self.events.of_type("tool_finished")[0]

    def test_tool_exception_is_reported_to_the_model(self):
        provider = self.script([Response(tool_calls=[ToolCall("1", "explode", {"text": "a"})]), Response(text="ok")])
        self.assertEqual(Worker().work("x"), "ok")
        self.assertEqual(self.tool_result()["result"], "Error: tool blew up")
        self.assertTrue(self.tool_result()["error"])
        tool_message = next(m for m in provider.requests[1].messages if m["role"] == "tool")
        self.assertEqual(tool_message["results"][0]["content"], "Error: tool blew up")

    def test_unknown_tool_invalid_json_and_bad_arguments(self):
        cases = [
            ({"name": "nope", "args": {}}, "Error: there is no tool named 'nope'."),
            ({"name": "echo", "args": {"__invalid_json__": "{bad"}}, "Error: tool arguments are not valid JSON"),
            ({"name": "echo", "args": {"text": "a", "extra": 1}}, "Error: invalid arguments: unknown argument: extra"),
            ({"name": "echo", "args": {}}, "Error: invalid arguments: text: required argument missing"),
        ]
        for call, expected in cases:
            with self.subTest(call=call):
                self.events.events.clear()
                self.script([Response(tool_calls=[ToolCall("1", call["name"], call["args"])]), Response(text="ok")])
                self.assertEqual(Worker().work("x"), "ok")
                self.assertEqual(self.tool_result()["result"], expected)

    def test_endless_tool_loop_hits_max_turns(self):
        self.script([], default=Response(tool_calls=[ToolCall("1", "echo", {"text": "again"})]))
        with self.assertRaises(MaxTurnsExceeded):
            Worker().work("x")
        self.assertEqual(len(self.events.of_type("turn_started")), 3)
        self.assertEqual(self.events.of_type("call_finished")[0]["status"], "error")

    def test_provider_error_propagates_with_its_status(self):
        error = ProviderError("HTTP 500: boom")
        error.status = 500
        self.script([error])
        with self.assertRaises(ProviderError):
            Worker().work("x")

    def test_auto_model_skips_a_provider_that_denies_access(self):
        error = ProviderError("HTTP 403: denied")
        error.status = 403
        denied = Scripted([error])
        fake = fake_provider_module.FakeProvider()
        calls = []

        def find(model):
            calls.append(model)
            return (denied, "denied-model") if len(calls) == 1 else (fake, "fake")

        with mock.patch.object(agents_module, "find_provider", find), \
                mock.patch.object(agents_module, "mark_unavailable") as mark:
            self.run.default_model = "auto"
            self.assertIsInstance(Worker(tools=[]).work("hello there. second sentence."), str)
        mark.assert_called_once_with("scripted")
        switched = self.events.of_type("model_switched")[0]
        self.assertEqual((switched["old"], switched["new"]), ("denied-model", "fake"))

    def test_fixed_model_does_not_fall_back(self):
        error = ProviderError("HTTP 403: denied")
        error.status = 403
        self.script([error])
        with self.assertRaises(ProviderError):
            Worker(model="scripted-model").work("x")

    def test_format_fixes_until_valid(self):
        provider = self.script([Response(text="not json"), Response(text="still not"), Response(text='{"result": 4}')])
        self.assertEqual(Counter().count(), 4)
        self.assertEqual(len(self.events.of_type("format_error")), 2)
        fixes = [m["content"] for m in provider.requests[-1].messages if m["role"] == "user"][1:]
        self.assertEqual(len(fixes), 2)
        self.assertTrue(all(f.startswith("[Format error]") for f in fixes), fixes)

    def test_zero_retries_fail_on_the_first_bad_answer(self):
        self.script([Response(text="nope")])
        with self.assertRaises(SchemaError) as ctx:
            Counter(retries=0).count()
        self.assertIn("after 0 correction attempts", str(ctx.exception))

    def test_empty_answer_for_a_string_method(self):
        self.script([Response(text="")])
        self.assertEqual(Worker().work("x"), "")


class AgentEdges(unittest.TestCase):
    def setUp(self):
        self.events = EventCollector()
        self.run = Run(approval="auto", default_model="fake")
        self.run.bus.add(self.events)
        set_current_run(self.run)

    def tearDown(self):
        set_current_run(None)

    def test_agent_without_instructions_still_runs(self):
        class Bare(Agent):
            @llm
            def say(self, text: str) -> str: ...

        self.assertIsInstance(Bare().say("one sentence. another one."), str)

    def test_missing_instructions_file(self):
        class Missing(Agent):
            """prompts/does-not-exist.md"""

            @llm
            def say(self, text: str) -> str: ...

        with self.assertRaises(InstructionsNotFound) as ctx:
            Missing().say("x")
        self.assertIn("does-not-exist.md", str(ctx.exception))

    def test_unknown_model_and_settings(self):
        class Bare(Agent):
            @llm
            def say(self, text: str) -> str: ...

        with self.assertRaises(UnknownModel):
            Bare(model="no-such-model-9").say("x")
        with self.assertRaises(TypeError) as ctx:
            Bare(temprature=1)
        self.assertIn("unknown setting 'temprature'", str(ctx.exception))

    def test_run_budget_of_zero_blocks_the_first_call(self):
        self.run.total_budget = 0.0

        class Paid(Agent):
            """x"""

            @llm
            def go(self, text: str) -> str: ...

        with self.assertRaises(BudgetExceeded):
            Paid().go("hi")

    def test_memory_and_clear_memory(self):
        class Chat(Agent):
            """x"""
            memory = True

            @llm
            def chat(self, text: str) -> str: ...

        bot = Chat()
        bot.chat("first message. it has words.")
        bot.chat("second message. more words.")
        self.assertEqual([m["role"] for m in bot._history], ["user", "assistant", "user", "assistant"])
        bot.clear_memory()
        self.assertEqual(bot._history, [])

    def test_async_calls_run_concurrently_and_raise(self):
        class Async(Agent):
            """x"""

            @llm
            async def go(self, text: str) -> str: ...

            @llm
            async def number(self, text: str) -> int: ...

        async def main():
            return await asyncio.gather(*(Async().go(f"text number {i}.") for i in range(5)))

        self.assertEqual(len(asyncio.run(main())), 5)
        self.assertEqual(len(self.events.of_type("call_finished")), 5)

        async def bad():
            return await Async(model="fake:bad", retries=0).number("x")

        with self.assertRaises(SchemaError):
            asyncio.run(bad())


class FlowEdges(unittest.TestCase):
    def setUp(self):
        self.run = Run(approval="auto", default_model="fake")
        set_current_run(self.run)

    def tearDown(self):
        set_current_run(None)

    def test_parallel_edge_cases(self):
        self.assertEqual(parallel(), ())
        self.assertEqual(parallel_map(lambda x: x, []), [])
        self.assertEqual(parallel(lambda: parallel(lambda: 1, lambda: 2), lambda: 3), ((1, 2), 3))
        self.assertEqual(parallel_map(lambda x: x * 2, (i for i in range(20))), [i * 2 for i in range(20)])

    def test_parallel_waits_for_every_task_and_raises_the_first_error(self):
        finished = threading.Event()

        def slow():
            finished.wait(0.2)
            finished.set()
            return "slow"

        def boom():
            raise ValueError("first")

        with self.assertRaises(ValueError):
            parallel(slow, boom)
        self.assertTrue(finished.is_set())

    def test_approval_without_a_value(self):
        self.assertIsNone(require_approval())

    def test_terminal_approval_with_closed_input_is_rejected(self):
        class ClosedTerminal(io.StringIO):
            def isatty(self):
                return True

        self.run.approval_mode = "ask"
        with mock.patch.object(sys, "stdin", ClosedTerminal("")), mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            with self.assertRaises(ApprovalRejected):
                require_approval("x", "Publish?")
            self.assertIn("No input", sys.stderr.getvalue())

    def test_terminal_approval_edit_revalidates(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        self.run.approval_mode = "ask"
        item = Item(name="a", qty=1)
        with mock.patch.object(sys, "stdin", Terminal('e\n{"name": "b", "qty": 2}\n')), \
                mock.patch.object(sys, "stderr", io.StringIO()), mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(require_approval(item), Item(name="b", qty=2))
        with mock.patch.object(sys, "stdin", Terminal('e\n{"name": "b", "qty": -5}\n')), \
                mock.patch.object(sys, "stderr", io.StringIO()), mock.patch.object(sys, "stdout", io.StringIO()):
            with self.assertRaises(SchemaError):
                require_approval(item)


HELLO = '''
import sys

from agentflight import Agent, llm


class Greeter(Agent):
    """You greet people."""

    @llm
    def greet(self, who: str) -> str: ...


print(Greeter().greet("world. nice day."), sys.argv[1:])
'''


class CommandLineEdges(unittest.TestCase):
    def setUp(self):
        self.folder = temp_project({"main.py": HELLO})

    def tearDown(self):
        cleanup(self.folder)

    def test_bad_arguments_fail_cleanly(self):
        cases = [
            (["run", "missing.py"], "file not found"),
            (["run", str(self.folder / "agentflight.toml")], "not a Python file"),
            (["eval", str(self.folder / "agentflight.toml")], "not a Python file"),
            (["run", str(self.folder / "main.py"), "--step", "2"], "--step needs --replay"),
            (["run", str(self.folder / "main.py"), "--replay", "20990101-000000-dead"], "run record not found"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                r = run_cli(*args)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn(message, r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_broken_agentflight_toml(self):
        (self.folder / "agentflight.toml").write_text("[project\nname = broken", encoding="utf-8")
        r = run_cli("run", str(self.folder / "main.py"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("could not read", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.folder / ".agentflight" / "runs").exists())

    def test_path_with_spaces_and_non_ascii_and_program_arguments(self):
        folder = self.folder / "Çağ proje with spaces"
        folder.mkdir()
        (folder / "main.py").write_text(HELLO, encoding="utf-8")
        r = run_cli("run", str(folder / "main.py"), "--", "a b", "c")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("['a b', 'c']", r.stdout)
        self.assertIn("1 agent call ", r.stderr)

    def test_exit_codes(self):
        (self.folder / "exit3.py").write_text("import sys\nprint('bye')\nsys.exit(3)\n", encoding="utf-8")
        (self.folder / "ctrl_c.py").write_text("raise KeyboardInterrupt\n", encoding="utf-8")
        r = run_cli("run", str(self.folder / "exit3.py"))
        self.assertEqual((r.returncode, "bye" in r.stdout), (3, True))
        self.assertIn("■ error", r.stderr)
        r = run_cli("run", str(self.folder / "ctrl_c.py"))
        self.assertEqual(r.returncode, 130)
        self.assertIn("■ cancelled", r.stderr)

    def test_a_broken_eval_does_not_hide_the_others(self):
        (self.folder / "evals.py").write_text('''
from agentflight import Agent, Eval, llm


class Echo(Agent):
    """Repeat the text."""

    @llm
    def repeat(self, text: str) -> str: ...


class Works(Eval, target=Echo.repeat):
    examples = [("Hello there.", "Hello there.")]


class NoInputs(Eval, target=Echo.repeat):
    inputs = []


class NoFiles(Eval, target=Echo.repeat):
    inputs = "data/*.nothing"
''', encoding="utf-8")
        r = run_cli("eval", str(self.folder / "evals.py"), "--json", str(self.folder / "report.json"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("PASS", r.stdout)
        self.assertIn("ERROR  NoInputs", r.stdout)
        self.assertIn("no file matches 'data/*.nothing'", r.stdout)
        report = json.loads((self.folder / "report.json").read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in report["evals"]], ["Works", "NoInputs", "NoFiles"])
        self.assertTrue(report["evals"][0]["passed"])

    def test_corrupt_record_files_are_tolerated(self):
        self.assertEqual(run_cli("run", str(self.folder / "main.py")).returncode, 0)
        record = next((self.folder / ".agentflight" / "runs").iterdir())
        with open(record / "calls.jsonl", "a", encoding="utf-8") as f:
            f.write("{not json\n")
        (record / "summary.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(read_run(self.folder, record.name)["summary"], {})
        r = run_cli("run", str(self.folder / "main.py"), "--replay", record.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from record", r.stderr)
        self.assertEqual(run_cli("runs", "--project", str(self.folder)).returncode, 0)

    def test_stale_or_garbage_panel_discovery_file_is_ignored(self):
        home = self.folder / "home"
        home.mkdir()
        for content in (json.dumps({"port": 1, "runner_port": 2, "token": "x", "pid": 999999}), "garbage"):
            (home / "panel.json").write_text(content, encoding="utf-8")
            r = run_cli("run", str(self.folder / "main.py"), env={"AGENTFLIGHT_HOME": str(home)})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("connected to the AgentFlight panel", r.stderr)

    def test_new_on_an_existing_project_changes_nothing(self):
        before = (self.folder / "main.py").read_text(encoding="utf-8")
        r = run_cli("new", str(self.folder))
        self.assertEqual(r.returncode, 0)
        self.assertIn("main.py", r.stdout)  # only the files that were missing are listed
        self.assertEqual((self.folder / "main.py").read_text(encoding="utf-8"), before)
        r = run_cli("new", str(self.folder))
        self.assertIn("nothing to do", r.stdout)


class PanelConnectionEdges(unittest.TestCase):
    def test_program_survives_a_panel_that_goes_away(self):
        """The panel accepts the handshake and then disappears; the program must still finish normally."""
        folder = temp_project({"main.py": HELLO})
        self.addCleanup(cleanup, folder)
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def panel():
            conn, _ = listener.accept()
            conn.recv(4096)
            conn.close()
            listener.close()

        threading.Thread(target=panel, daemon=True).start()
        r = run_cli("run", str(folder / "main.py"),
                    env={"AGENTFLIGHT_PANEL_SOCKET": f"127.0.0.1:{port}", "AGENTFLIGHT_PANEL_TOKEN": "token:sid"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[]", r.stdout)

    def test_output_mirror_splits_lines(self):
        sent = []
        original = io.StringIO()
        mirror = OutputMirror(original, sent.append, "stdout")
        mirror.write("first\r\nsec")
        mirror.write("ond\nthird")
        self.assertEqual([e["text"] for e in sent], ["first", "second"])
        mirror.drain()
        self.assertEqual([e["text"] for e in sent], ["first", "second", "third"])
        self.assertEqual(original.getvalue(), "first\r\nsecond\nthird")
        self.assertEqual({e["type"] for e in sent}, {"output"})


class RecordingEdges(unittest.TestCase):
    def test_token_events_are_not_recorded(self):
        folder = temp_project({})
        self.addCleanup(cleanup, folder)
        recorder = Recorder(folder / ".agentflight" / "runs" / "r1", {"run_id": "r1"})
        recorder.event({"type": "token", "text": "x"})
        recorder.event({"type": "call_started", "id": "c1"})
        recorder.close({"status": "done"})
        record = read_run(folder, "r1")
        self.assertEqual([e["type"] for e in record["events"]], ["call_started"])
        self.assertEqual(record["summary"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
