"""Schemas: JSON schema generation, validation and parsing model output."""

from __future__ import annotations

import unittest
from typing import Literal

from agentflight.errors import SchemaError
from agentflight.schema import Schema, extract_json, field, json_schema, output_schema, parse_output
from tests.helpers import ROOT  # noqa: F401  (isolates the test environment)


class Item(Schema):
    text: str
    importance: Literal["low", "high"] = "low"


class Report(Schema):
    title: str = field("Title", max=40)
    score: int = field("Score", min=1, max=10)
    items: list[Item] = field("Items", min=1)
    note: str | None = None
    tags: list[str] = []


class JsonSchema(unittest.TestCase):
    def test_object_schema(self):
        s = json_schema(Report)
        self.assertEqual(s["type"], "object")
        self.assertEqual(set(s["required"]), {"title", "score", "items"})
        self.assertFalse(s["additionalProperties"])
        self.assertEqual(s["properties"]["score"]["minimum"], 1)
        self.assertEqual(s["properties"]["score"]["maximum"], 10)
        self.assertEqual(s["properties"]["title"]["maxLength"], 40)
        self.assertEqual(s["properties"]["items"]["items"]["properties"]["importance"]["enum"], ["low", "high"])
        self.assertEqual(s["properties"]["note"]["anyOf"], [{"type": "string"}, {"type": "null"}])

    def test_strict_schema_moves_limits_into_description(self):
        s = json_schema(Report, strict=True)
        score = s["properties"]["score"]
        self.assertNotIn("minimum", score)
        self.assertIn("at least 1", score["description"])

    def test_non_object_return_type_is_wrapped(self):
        s = output_schema(list[int])
        self.assertEqual(s["properties"]["result"], {"type": "array", "items": {"type": "integer"}})


class Validation(unittest.TestCase):
    def test_valid(self):
        r = Report.validate({"title": "B", "score": "7", "items": [{"text": "m", "importance": "HIGH"}]})
        self.assertEqual(r.score, 7)
        self.assertEqual(r.items[0].importance, "high")
        self.assertIsNone(r.note)
        self.assertEqual(r.tags, [])
        self.assertEqual(r.to_dict()["items"][0]["text"], "m")

    def test_errors_are_reported_with_their_path(self):
        with self.assertRaises(SchemaError) as cm:
            Report.validate({"title": "B", "score": "high", "items": [{"importance": "medium"}], "extra": 1})
        errors = cm.exception.errors
        self.assertTrue(any(e.startswith("score: expected int") for e in errors), errors)
        self.assertTrue(any(e.startswith("items[0].text: required field missing") for e in errors), errors)
        self.assertTrue(any("items[0].importance" in e for e in errors), errors)
        self.assertTrue(any("unknown field" in e for e in errors), errors)

    def test_limits(self):
        with self.assertRaises(SchemaError) as cm:
            Report.validate({"title": "B", "score": 11, "items": []})
        text = str(cm.exception)
        self.assertIn("must be at most 10", text)
        self.assertIn("must be at least 1 items", text)

    def test_defaults_are_copied(self):
        a = Report(title="a", score=1, items=[{"text": "x"}])
        b = Report(title="b", score=1, items=[{"text": "y"}])
        a.tags.append("z")
        self.assertEqual(b.tags, [])

    def test_constructor_and_equality(self):
        a = Item(text="x")
        self.assertEqual(a, Item.validate('{"text": "x"}'))
        self.assertEqual(repr(a), "Item(text='x', importance='low')")


class OutputParsing(unittest.TestCase):
    def test_code_fence_and_surrounding_text(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure, here it is: {"a": [1, 2]} hope this helps'), {"a": [1, 2]})

    def test_wrapped_type(self):
        self.assertEqual(parse_output(list[int], '{"result": [1, 2]}'), [1, 2])
        self.assertEqual(parse_output(list[int], "[3]"), [3])
        self.assertEqual(parse_output(str, "  hello \n"), "hello")

    def test_invalid_json(self):
        with self.assertRaises(SchemaError):
            parse_output(Item, "no json here")


if __name__ == "__main__":
    unittest.main()
