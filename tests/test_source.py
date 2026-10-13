"""AST helpers: `definitions_only` keeps the definitions of a program and drops its program code (used by evals)."""

from __future__ import annotations

import ast
import unittest

from agentflight.source import definitions_only
from tests.helpers import ROOT  # noqa: F401  (isolates the test environment)

SOURCE = '''
import agentflight
from agentflight import Agent, Eval, Schema, field, llm, parallel, parallel_for_each, parallel_map, tool


class Summary(Schema):
    title: str
    score: int = field("score", min=1, max=10)


class Summarizer(Agent):
    """prompts/summary.md"""
    model = "fake"

    @llm
    def summarize(self, text: str) -> Summary: ...

    def twice(self, text: str) -> list:
        return [self.summarize(text), self.summarize(text)]


class Senior(Summarizer):
    @llm
    def comment(self, x: str) -> str:
        """Write a short comment."""
        ...


@tool
def add(a: int, b: int) -> int:
    """Adds two numbers."""
    return a + b


class SummaryTest(Eval, target=Summarizer.summarize):
    inputs = ["a"]


label = "class X(Agent): just a string"
first, second = parallel(lambda: Summarizer().summarize("x"), lambda: Summarizer().summarize("y"))
parallel_for_each(print, ["p", "q"])
'''


def _strip(source: str) -> str:
    return ast.unparse(definitions_only(ast.parse(source)))


class DefinitionsOnly(unittest.TestCase):
    def test_definitions_only(self):
        text = _strip(SOURCE + "\nanswer = Summarizer().summarize('z')\nconstant = [1, 2]\n"
                               "approved = agentflight.require_approval(constant, 'ok?')\n")
        self.assertNotIn("parallel(lambda", text)
        self.assertNotIn("parallel_for_each(print", text)
        self.assertNotIn("answer =", text)
        self.assertNotIn("approved =", text)
        self.assertIn("constant = [1, 2]", text)
        self.assertIn("label = 'class X(Agent): just a string'", text)
        self.assertIn("class SummaryTest", text)
        self.assertIn("class Senior", text)
        self.assertIn("def add(a: int, b: int) -> int:", text)
        self.assertIn("from agentflight import Agent", text)

    def test_definitions_only_drops_dependent_assignments(self):
        text = _strip(SOURCE + "\nsummaries = parallel_map(Summarizer().summarize, ['a'])\n"
                               "count = len(summaries)\nconstant = 3\n")
        self.assertNotIn("summaries", text)
        self.assertNotIn("count =", text)
        self.assertIn("constant = 3", text)


if __name__ == "__main__":
    unittest.main()
