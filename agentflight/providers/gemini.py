"""Google Gemini provider (REST, streaming)."""

from __future__ import annotations

import json
import os

from ..errors import ModelRefused, ProviderError
from .base import Provider, Request, Response, ToolCall, http_open, sse_events

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}


def _api_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def _openapi_schema(schema):
    """Reduces a JSON schema to the OpenAPI subset for the legacy `parameters` field."""
    if isinstance(schema, list):
        return [_openapi_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for k, v in schema.items():
        if k in ("additionalProperties", "$schema", "title"):
            continue
        if k == "anyOf":
            options = [s for s in v if s.get("type") != "null"]
            if len(options) == 1:
                result.update(_openapi_schema(options[0]))
                result["nullable"] = True
                continue
        result[k] = _openapi_schema(v)
    return result


class GeminiProvider(Provider):
    name = "gemini"
    key_env_var = "GEMINI_API_KEY"

    def __init__(self):
        self._legacy_parameters = False

    def is_ready(self):
        if _api_key():
            return True, ""
        return False, "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set"

    def _contents(self, request: Request) -> list[dict]:
        contents: list[dict] = []
        for m in request.messages:
            if m["role"] == "user":
                part = {"text": m["content"]}
                if contents and contents[-1]["role"] == "user":
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
            elif m["role"] == "assistant":
                if m.get("provider") == self.name and m.get("raw"):
                    parts = m["raw"]
                else:
                    parts = []
                    if m.get("content"):
                        parts.append({"text": m["content"]})
                    for c in m.get("tool_calls", []):
                        parts.append({"functionCall": {"name": c["name"], "args": c["arguments"]}})
                    if not parts:
                        parts = [{"text": "…"}]
                contents.append({"role": "model", "parts": parts})
            elif m["role"] == "tool":
                parts = [
                    {"functionResponse": {"name": r["name"],
                                          "response": {"error" if r.get("error") else "result": r["content"]}}}
                    for r in m["results"]
                ]
                contents.append({"role": "user", "parts": parts})
        return contents

    def _body(self, request: Request) -> dict:
        body: dict = {"contents": self._contents(request)}
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        if request.tools:
            declarations = []
            for t in request.tools:
                d = {"name": t["name"], "description": t["description"]}
                if self._legacy_parameters:
                    d["parameters"] = _openapi_schema(t["schema"])
                else:
                    d["parametersJsonSchema"] = t["schema"]
                declarations.append(d)
            body["tools"] = [{"functionDeclarations": declarations}]
        config: dict = {}
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.seed is not None:
            config["seed"] = request.seed
        if request.max_tokens:
            config["maxOutputTokens"] = request.max_tokens
        if request.schema is not None and not request.tools:
            config["responseMimeType"] = "application/json"
        if config:
            body["generationConfig"] = config
        return body

    def call(self, request: Request, on_text) -> Response:
        api_key = _api_key()
        if not api_key:
            raise ProviderError("GEMINI_API_KEY is not set")
        url = f"{BASE_URL}/models/{request.model}:streamGenerateContent?alt=sse"
        try:
            response = http_open(url, self._body(request), {"x-goog-api-key": api_key})
        except ProviderError as e:
            if (getattr(e, "status", None) == 400 and request.tools and "parametersJsonSchema" in str(e)
                    and not self._legacy_parameters):
                self._legacy_parameters = True
                response = http_open(url, self._body(request), {"x-goog-api-key": api_key})
            else:
                raise

        parts: list[dict] = []
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        input_tokens = output_tokens = 0
        stop_reason = ""
        model_version = request.model
        with response:
            for _, data in sse_events(response):
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk:
                    raise ProviderError(str(chunk["error"].get("message", chunk["error"])))
                block_reason = chunk.get("promptFeedback", {}).get("blockReason")
                if block_reason:
                    raise ModelRefused(f"Gemini blocked the request: {block_reason}")
                model_version = chunk.get("modelVersion", model_version)
                usage = chunk.get("usageMetadata")
                if usage:
                    input_tokens = usage.get("promptTokenCount", input_tokens)
                    output_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
                for candidate in chunk.get("candidates", [])[:1]:
                    stop_reason = candidate.get("finishReason", stop_reason) or stop_reason
                    for p in candidate.get("content", {}).get("parts", []):
                        if "text" in p and not p.get("thought"):
                            text_parts.append(p["text"])
                            on_text(p["text"])
                        if "functionCall" in p:
                            fc = p["functionCall"]
                            calls.append(ToolCall(
                                id=fc.get("id") or f"g{len(calls) + 1}",
                                name=fc.get("name", ""),
                                arguments=fc.get("args") or {},
                            ))
                        # raw content: merge consecutive plain text parts
                        if parts and set(p) == {"text"} and set(parts[-1]) == {"text"}:
                            parts[-1] = {"text": parts[-1]["text"] + p["text"]}
                        else:
                            parts.append(dict(p))
        if stop_reason in _BLOCKED:
            raise ModelRefused(f"Gemini blocked the response: {stop_reason}")
        return Response(
            text="".join(text_parts),
            tool_calls=calls,
            raw=parts or [{"text": ""}],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            model=model_version,
        )
