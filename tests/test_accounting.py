"""Token and cost accounting: the numbers must add up at every level (turn, call, record, run summary, replay)."""

import json
import unittest
from unittest import mock

import agentflight.agents as agents_module
from agentflight import Agent, llm
from agentflight.errors import SchemaError
from agentflight.providers.base import Provider, Response
from agentflight.runtime import Run, set_current_run
from tests.helpers import EventCollector, cleanup, run_cli, temp_project

PROGRAM = '''
from agentflight import Agent, Schema, field, llm, parallel, tool


class Item(Schema):
    name: str
    qty: int = field("quantity", min=0)


@tool
def lookup(topic: str) -> str:
    """Looks a topic up."""
    return "Biogas is made from organic waste. It is mostly methane."


class Researcher(Agent):
    """You research."""
    tools = [lookup]

    @llm
    def research(self, topic: str) -> str: ...


class Extractor(Agent):
    """You extract."""

    @llm
    def extract(self, text: str) -> Item: ...


a, b = parallel(lambda: Researcher().research("biogas"), lambda: Researcher().research("wind"))
print(Extractor(model="fake:bad").extract("Ten boxes of screws. They are small."))
'''

PRICES = '\n[prices]\n"fake" = [3.0, 15.0]\n"fake:bad" = [3.0, 15.0]\n'


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class RecordedTotals(unittest.TestCase):
    def setUp(self):
        self.folder = temp_project({"main.py": PROGRAM})
        with open(self.folder / "agentflight.toml", "a", encoding="utf-8") as f:
            f.write(PRICES)

    def tearDown(self):
        cleanup(self.folder)

    def test_turns_calls_record_and_summary_agree(self):
        r = run_cli("run", str(self.folder / "main.py"))
        self.assertEqual(r.returncode, 0, r.stderr)
        run_dir = next((self.folder / ".agentflight" / "runs").iterdir())
        events = read_jsonl(run_dir / "events.jsonl")
        calls = {c["id"]: c for c in read_jsonl(run_dir / "calls.jsonl")}
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

        per_call: dict[str, list] = {}
        for e in events:
            if e["type"] == "turn_finished":
                totals = per_call.setdefault(e["id"], [0, 0, 0.0, 0])
                totals[0] += e["input_tokens"]
                totals[1] += e["output_tokens"]
                totals[2] += e["cost"]
                totals[3] += 1
        finished = {e["id"]: e for e in events if e["type"] == "call_finished"}
        self.assertEqual(len(finished), 3)
        self.assertGreater(sum(t[3] for t in per_call.values()), 3, "tool turns and a format fix happened")
        for call_id, (inp, out, cost, turns) in per_call.items():
            event, record = finished[call_id], calls[call_id]
            self.assertEqual((event["input_tokens"], event["output_tokens"], event["turns"]), (inp, out, turns))
            self.assertAlmostEqual(event["cost"], cost, places=9)
            self.assertEqual((record["input_tokens"], record["output_tokens"]), (inp, out))
            self.assertAlmostEqual(cost, (inp * 3.0 + out * 15.0) / 1_000_000, places=9)

        total_in = sum(t[0] for t in per_call.values())
        total_out = sum(t[1] for t in per_call.values())
        total_cost = sum(t[2] for t in per_call.values())
        self.assertEqual((summary["input_tokens"], summary["output_tokens"], summary["total_tokens"]),
                         (total_in, total_out, total_in + total_out))
        self.assertAlmostEqual(summary["total_cost"], total_cost, places=6)
        self.assertEqual(summary["call_count"], 3)

        replay = run_cli("run", str(self.folder / "main.py"), "--replay", run_dir.name)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_dir = next(d for d in (self.folder / ".agentflight" / "runs").iterdir() if d != run_dir)
        replayed = json.loads((replay_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((replayed["call_count"], replayed["total_tokens"], replayed["total_cost"]), (3, 0, 0.0))


class Metered(Provider):
    """Answers with fixed token counts."""

    name = "metered"

    def __init__(self, answers, subscription=False):
        self.answers = list(answers)
        self.subscription = subscription

    def call(self, request, on_text):
        return self.answers.pop(0)


class Counter(Agent):
    """You count."""
    retries = 1

    @llm
    def count(self) -> int: ...


class CallLevelTotals(unittest.TestCase):
    def setUp(self):
        self.events = EventCollector()
        self.run = Run(approval="auto", default_model="metered")
        self.run.bus.add(self.events)
        set_current_run(self.run)

    def tearDown(self):
        set_current_run(None)

    def use(self, provider):
        patcher = mock.patch.object(agents_module, "find_provider", lambda model: (provider, "claude-opus-5-5"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_call_still_reports_what_it_spent(self):
        self.use(Metered([Response(text="no", input_tokens=100, output_tokens=10),
                          Response(text="still no", input_tokens=120, output_tokens=12)]))
        with self.assertRaises(SchemaError):
            Counter().count()
        finished = self.events.of_type("call_finished")[0]
        self.assertEqual(finished["status"], "error")
        self.assertEqual((finished["input_tokens"], finished["output_tokens"], finished["turns"]), (220, 22, 2))
        self.assertGreater(finished["cost"], 0)
        self.assertAlmostEqual(finished["cost"], self.run.total_cost, places=12)
        self.assertEqual((self.run.total_input_tokens, self.run.total_output_tokens), (220, 22))

    def test_subscription_calls_count_tokens_but_cost_nothing(self):
        self.use(Metered([Response(text="42", input_tokens=500, output_tokens=5)], subscription=True))
        self.assertEqual(Counter().count(), 42)
        finished = self.events.of_type("call_finished")[0]
        self.assertEqual((finished["input_tokens"], finished["output_tokens"], finished["cost"]), (500, 5, 0.0))
        self.assertTrue(finished["subscription"])
        self.assertEqual((self.run.total_input_tokens, self.run.total_cost), (500, 0.0))

    def test_published_price_is_applied(self):
        self.use(Metered([Response(text="42", input_tokens=1_000_000, output_tokens=1_000_000)]))
        Counter().count()
        from agentflight.pricing import find_price
        price_in, price_out = find_price("claude-opus-5-5")
        self.assertAlmostEqual(self.events.of_type("call_finished")[0]["cost"], price_in + price_out, places=9)


class PriceTable(unittest.TestCase):
    def test_long_prompts_use_the_higher_price(self):
        from agentflight.pricing import cost
        self.assertAlmostEqual(cost("claude-haiku-5-5", 100_000, 1_000), 0.01 + 0.0005, places=9)
        self.assertAlmostEqual(cost("claude-haiku-5-5", 100_001, 1_000), 100_001 * 0.5e-6 + 0.0025, places=9)
        self.assertAlmostEqual(cost("claude-opus-5-5", 1_000_000, 1_000_000), 24.0, places=9)
        self.assertEqual(cost("unknown-model", 1_000, 1_000), 0.0)


if __name__ == "__main__":
    unittest.main()
