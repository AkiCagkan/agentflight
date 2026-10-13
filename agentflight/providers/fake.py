"""Fake model: for offline demos and tests without an API key.

Not a real language model. It builds deterministic (same input -> same output) answers that fit
the requested schema from the sentences of the input, really calls the tools and imitates token
streaming, so live watching, interjections and record/replay in the panel can be tried without a
model.

Model names:
  fake           - normal
  fake:bad       - deliberately breaks the schema on the first try
                   (format error -> shows the automatic correction flow)

Token delay: AGENTFLIGHT_FAKE_DELAY (seconds, default 0.02)
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time

from ..prompts import FORMAT_ERROR_PREFIX, INTERJECTION_PREFIX
from .base import Provider, Request, Response, ToolCall

_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")
_HEADING = re.compile(r"^\s*#+\s*\w+\s*$")


def _sentences(source: str) -> list[str]:
    result = []
    for piece in _SENTENCE.split(source or ""):
        piece = piece.strip(" \t-*•")
        if not piece or _HEADING.match(piece) or piece.startswith(("{", "}", "[", "]", "```")):
            continue
        if len(piece) < 12:
            continue
        result.append(piece)
    return result


def _leaves(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _leaves(v)]
    if isinstance(value, list):
        return [s for v in value for s in _leaves(v)]
    return []


def _plain_text(user: str) -> str:
    """Takes the text out of the '## name' sections; flattens the string values of JSON sections."""
    pieces = re.split(r"^##\s+\w+\s*$", user, flags=re.M)
    result = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if piece[0] in "[{":
            try:
                leaves = _leaves(json.loads(piece))
            except json.JSONDecodeError:
                leaves = []
            if leaves:
                result.append(" ".join(s.strip().rstrip(".") + "." for s in leaves if s.strip()))
                continue
        result.append(piece)
    return "\n".join(result)


def _words(source: str) -> list[str]:
    return [w for w in re.findall(r"[\w'-]+", source or "") if len(w) > 2]


class FakeProvider(Provider):
    name = "fake"

    def is_ready(self):
        return True, ""

    # --- generators
    def _title(self, source, rng):
        sentences = _sentences(source)
        if sentences:
            words = sentences[0].split()
            return " ".join(words[:6]).rstrip(",.;:") + ("…" if len(words) > 6 else "")
        words = _words(source)
        return " ".join(words[:4]).capitalize() or "Title"

    def _sentence(self, source, rng, index=None):
        sentences = _sentences(source)
        if not sentences:
            words = _words(source) or ["sample", "text"]
            return " ".join(words[:10]).capitalize() + "."
        if index is not None:
            return sentences[index % len(sentences)]
        return rng.choice(sentences)

    def _text(self, source, rng, notes):
        sentences = _sentences(source)
        if sentences:
            chosen = [sentences[0]] + rng.sample(sentences[1:], min(2, max(0, len(sentences) - 1)))
            text = " ".join(chosen)
        else:
            text = self._sentence(source, rng)
        if notes:
            text += f" (Note: took “{notes[-1]}” into account.)"
        return text

    def _value(self, schema, source, rng, name="", notes=None, index=0):
        if not isinstance(schema, dict) or not schema:
            return self._sentence(source, rng)
        if "anyOf" in schema:
            options = [s for s in schema["anyOf"] if s.get("type") != "null"] or schema["anyOf"]
            return self._value(options[0], source, rng, name, notes, index)
        if "enum" in schema:
            folded = (source or "").casefold()
            for option in schema["enum"]:
                if isinstance(option, str) and option.casefold() in folded:
                    return option
            return rng.choice(schema["enum"])
        kind = schema.get("type")
        if kind == "object":
            properties = schema.get("properties")
            if not properties:
                return {}
            required = set(schema.get("required", properties))
            return {
                sub_name: self._value(sub, source, rng, sub_name, notes, i)
                for i, (sub_name, sub) in enumerate(properties.items())
                if sub_name in required or rng.random() < 0.5
            }
        if kind == "array":
            min_items = schema.get("minItems", 2)
            max_items = schema.get("maxItems", max(min_items, 4))
            n = max(min_items, min(max_items, rng.randint(2, 4)))
            items = schema.get("items", {})
            if items.get("type") == "string" and "enum" not in items:
                sentences = _sentences(source) or [self._sentence(source, rng)]
                return [sentences[i % len(sentences)] for i in range(n)]
            return [self._value(items, source, rng, name, notes, i) for i in range(n)]
        if kind == "integer":
            low, high = schema.get("minimum", 1), schema.get("maximum", 10)
            return rng.randint(int(low), int(max(low, high)))
        if kind == "number":
            low, high = schema.get("minimum", 0), schema.get("maximum", 1)
            return round(rng.uniform(low, max(low, high)), 2)
        if kind == "boolean":
            return rng.random() > 0.3
        # string
        folded_name = name.casefold()
        if any(s in folded_name for s in ("title", "subject", "topic")):
            value = self._title(source, rng)
        elif any(s in folded_name for s in ("summary", "description", "explanation", "reason", "rationale", "text",
                                            "comment", "body")):
            value = self._text(source, rng, notes)
        elif folded_name == "language":
            value = "en"
        else:
            value = self._sentence(source, rng, index)
        max_length = schema.get("maxLength")
        if max_length and len(value) > max_length:
            value = value[: max(1, max_length - 1)] + "…"
        return value

    def _tool_arguments(self, schema, source, rng):
        properties = (schema or {}).get("properties", {})
        arguments = {}
        for name, sub in properties.items():
            if sub.get("type") == "string" and "enum" not in sub:
                words = _words(source)
                arguments[name] = " ".join(words[:2]) if words else "sample"
            else:
                arguments[name] = self._value(sub, source, rng, name)
        return arguments

    @staticmethod
    def _break(data, schema):
        if isinstance(data, dict):
            for name, sub in (schema.get("properties") or {}).items():
                if sub.get("type") in ("integer", "number") and name in data:
                    data[name] = "high"
                    return data
            required = schema.get("required") or []
            if required and required[0] in data:
                data.pop(required[0])
        return data

    def _stream(self, text, on_text, delay):
        pieces = re.findall(r"\s*\S+", text) or [text]
        wait = min(delay, 3.0 / max(1, len(pieces)))
        for p in pieces:
            on_text(p)
            if wait > 0:
                time.sleep(wait)

    def call(self, request: Request, on_text) -> Response:
        delay = float(os.environ.get("AGENTFLIGHT_FAKE_DELAY", "0.02"))
        digest = json.dumps([request.system, request.messages, request.seed], ensure_ascii=False, default=str,
                            sort_keys=True)
        rng = random.Random(hashlib.sha256(digest.encode("utf-8")).hexdigest())

        user = [m["content"] for m in request.messages if m["role"] == "user"]
        first = _plain_text(user[0] if user else "")
        notes = [u.split("]", 1)[1].split("\n")[0].strip() for u in user if u.startswith(INTERJECTION_PREFIX)]
        format_feedback = [u for u in user if u.startswith(FORMAT_ERROR_PREFIX)]
        tool_results = [r["content"] for m in request.messages if m["role"] == "tool" for r in m["results"]]
        input_tokens = max(1, len(request.system + json.dumps(request.messages, ensure_ascii=False)) // 4)

        if request.tools and not tool_results:
            calls = [
                ToolCall(id=f"s{i + 1}", name=t["name"], arguments=self._tool_arguments(t["schema"], first, rng))
                for i, t in enumerate(request.tools[:2])
            ]
            names = ", ".join(c.name for c in calls)
            preamble = f"First I am using the {names} tool."
            self._stream(preamble, on_text, delay)
            return Response(text=preamble, tool_calls=calls, input_tokens=input_tokens,
                            output_tokens=len(preamble) // 4 + 10, stop_reason="tool_use", model=request.model)

        source = "\n".join([first] + tool_results)
        if request.schema is None:
            text = self._text(source, rng, notes)
        else:
            data = self._value(request.schema, source, rng, notes=notes)
            if request.model.startswith("fake:bad") and not format_feedback:
                data = self._break(data, request.schema)
            text = json.dumps(data, ensure_ascii=False, indent=2)
        self._stream(text, on_text, delay)
        return Response(text=text, input_tokens=input_tokens, output_tokens=max(1, len(text) // 4),
                        stop_reason="stop", model=request.model)
