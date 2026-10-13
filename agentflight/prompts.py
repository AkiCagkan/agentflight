"""Builds the prompts of agent calls (system prompt, user message, correction requests)."""

from __future__ import annotations

import json

from .schema import to_jsonable

INTERJECTION_PREFIX = "[Message from the user]"
FORMAT_ERROR_PREFIX = "[Format error]"

_FORMAT_RULE = (
    "Give your final answer ONLY as a single JSON object that matches the JSON schema below. "
    "Do not add explanations, preamble or a code block."
)
_TOOLS_RULE = "Use the tools you are given when needed; give your final answer after you have seen the tool results."
_NO_INPUT = "Carry out the task."
_MISMATCH = "Your last answer did not match the expected schema:"
_FIX = "Fix your answer and send again only the JSON that matches the schema."
_INTERJECTION_HINT = "Take this message into account and continue; update your final answer if needed."


def system_prompt(instructions: str, signature: str, task: str | None, schema: dict | None, has_tools: bool) -> str:
    parts = []
    if instructions.strip():
        parts.append(instructions.strip())
    task_text = f"This call: `{signature}`"
    if task:
        task_text += "\n" + task.strip()
    parts.append("## Task\n" + task_text)
    if schema is not None:
        parts.append(
            "## Answer format\n" + _FORMAT_RULE + "\n```json\n" + json.dumps(schema, ensure_ascii=False, indent=2)
            + "\n```"
        )
    if has_tools:
        parts.append(_TOOLS_RULE)
    return "\n\n".join(parts)


def user_prompt(inputs: dict) -> str:
    if not inputs:
        return _NO_INPUT
    parts = []
    for name, value in inputs.items():
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(to_jsonable(value), ensure_ascii=False, indent=2)
        parts.append(f"## {name}\n{text}")
    return "\n\n".join(parts)


def format_error_message(errors: list[str]) -> str:
    return (
        f"{FORMAT_ERROR_PREFIX} {_MISMATCH}\n"
        + "\n".join(f"- {e}" for e in errors)
        + "\n" + _FIX
    )


def interjection(text: str) -> str:
    return f"{INTERJECTION_PREFIX} {text}\n{_INTERJECTION_HINT}"
