"""Provider for OpenAI and OpenAI-compatible servers (LM Studio, vLLM...) (REST, streaming).

Model names:
  gpt-5, gpt-5-mini, o4-mini ...      -> api.openai.com (OPENAI_API_KEY)
  openai/<model>                      -> same; another server via OPENAI_BASE_URL
  lmstudio/<model>                    -> http://localhost:1234/v1 (no key needed)
"""

from __future__ import annotations

import json
import os

from ..errors import ModelRefused, ProviderError
from .base import Provider, Request, Response, ToolCall, http_open, sse_events


class OpenAIProvider(Provider):
    name = "openai"
    key_env_var = "OPENAI_API_KEY"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, name: str = "openai"):
        self.name = name
        self._base_url = base_url
        self._api_key = api_key
        self._no_temperature: set[str] = set()
        self._no_response_format: set[str] = set()

    @property
    def base_url(self):
        return (self._base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    @property
    def api_key(self):
        return self._api_key or os.environ.get("OPENAI_API_KEY")

    def is_ready(self):
        if self.api_key:
            return True, ""
        return False, "OPENAI_API_KEY is not set"

    @staticmethod
    def _messages(request: Request) -> list[dict]:
        result: list[dict] = []
        if request.system:
            result.append({"role": "system", "content": request.system})
        for m in request.messages:
            if m["role"] == "user":
                result.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                entry: dict = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                        for c in m["tool_calls"]
                    ]
                result.append(entry)
            elif m["role"] == "tool":
                for r in m["results"]:
                    result.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
        return result

    def _body(self, request: Request) -> dict:
        body: dict = {
            "model": request.model,
            "messages": self._messages(request),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
                for t in request.tools
            ]
        # Compatible local servers (LM Studio etc.) do not support a schema mode and tools in the same request;
        # with tools, the schema stays in the prompt only and AgentFlight validates the answer.
        official = "api.openai.com" in self.base_url
        if (request.strict_schema is not None and request.model not in self._no_response_format
                and (official or not request.tools)):
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "output", "schema": request.strict_schema}}
        if request.temperature is not None and request.model not in self._no_temperature:
            body["temperature"] = request.temperature
        if request.seed is not None:
            body["seed"] = request.seed
        if request.max_tokens:
            body["max_completion_tokens"] = request.max_tokens
        return body

    def _open(self, request: Request):
        headers = {"Authorization": f"Bearer {self.api_key or 'none'}"}
        url = f"{self.base_url}/chat/completions"
        for _ in range(3):
            try:
                return http_open(url, self._body(request), headers)
            except ProviderError as e:
                text = str(e).lower()
                if (getattr(e, "status", None) == 400 and "temperature" in text
                        and request.model not in self._no_temperature):
                    self._no_temperature.add(request.model)
                    continue
                if (getattr(e, "status", None) == 400 and "response_format" in text
                        and request.model not in self._no_response_format):
                    self._no_response_format.add(request.model)
                    continue
                raise
        raise ProviderError("could not send the OpenAI request")

    def call(self, request: Request, on_text) -> Response:
        if not self.api_key and "api.openai.com" in self.base_url:
            raise ProviderError("OPENAI_API_KEY is not set")
        response = self._open(request)
        text: list[str] = []
        tools: dict[int, dict] = {}
        input_tokens = output_tokens = 0
        stop_reason = ""
        model_name = request.model
        with response:
            for _, data in sse_events(response):
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk:
                    raise ProviderError(str(chunk["error"]))
                model_name = chunk.get("model", model_name)
                if chunk.get("usage"):
                    input_tokens = chunk["usage"].get("prompt_tokens", input_tokens) or input_tokens
                    output_tokens = chunk["usage"].get("completion_tokens", output_tokens) or output_tokens
                for choice in chunk.get("choices", [])[:1]:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text.append(delta["content"])
                        on_text(delta["content"])
                    if delta.get("refusal"):
                        raise ModelRefused("the model declined: " + str(delta["refusal"]))
                    for tc in delta.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        t = tools.setdefault(i, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            t["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            t["name"] += fn["name"]
                        if fn.get("arguments"):
                            t["args"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        stop_reason = choice["finish_reason"]
        calls = []
        for i in sorted(tools):
            t = tools[i]
            try:
                arguments = json.loads(t["args"]) if t["args"].strip() else {}
            except json.JSONDecodeError:
                arguments = {"__invalid_json__": t["args"]}
            calls.append(ToolCall(id=t["id"] or f"c{i}", name=t["name"], arguments=arguments))
        full_text = "".join(text)
        return Response(
            text=full_text,
            tool_calls=calls,
            raw=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            model=model_name,
        )
