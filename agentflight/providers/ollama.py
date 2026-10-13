"""Ollama (local models) provider. Model name: ollama/<model>, e.g. ollama/llama3.2"""

from __future__ import annotations

import json
import os
import urllib.request

from ..errors import ProviderError
from .base import Provider, Request, Response, ToolCall, http_open, ndjson_lines


def ollama_url() -> str:
    url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not url.startswith("http"):
        url = "http://" + url
    return url.rstrip("/")


def ollama_models(timeout: float = 0.4) -> list[str]:
    try:
        with urllib.request.urlopen(f"{ollama_url()}/api/tags", timeout=timeout) as r:
            data = json.load(r)
        return [m["name"] for m in data.get("models", [])]
    except Exception:  # noqa: BLE001 - Ollama not running or not answering
        return []


class OllamaProvider(Provider):
    name = "ollama"

    def is_ready(self):
        if ollama_models():
            return True, ""
        return False, f"Ollama is not running ({ollama_url()})"

    @staticmethod
    def _messages(request: Request) -> list[dict]:
        result: list[dict] = []
        if request.system:
            result.append({"role": "system", "content": request.system})
        for m in request.messages:
            if m["role"] == "user":
                result.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                entry: dict = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]
                    ]
                result.append(entry)
            elif m["role"] == "tool":
                for r in m["results"]:
                    result.append({"role": "tool", "content": r["content"], "tool_name": r["name"]})
        return result

    def call(self, request: Request, on_text) -> Response:
        body: dict = {"model": request.model, "messages": self._messages(request), "stream": True}
        if request.tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]}}
                for t in request.tools
            ]
        if request.strict_schema is not None:
            body["format"] = request.strict_schema
        options = {}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.seed is not None:
            options["seed"] = request.seed
        if request.max_tokens:
            options["num_predict"] = request.max_tokens
        if options:
            body["options"] = options
        response = http_open(f"{ollama_url()}/api/chat", body, {}, timeout=600)
        text: list[str] = []
        calls: list[ToolCall] = []
        input_tokens = output_tokens = 0
        stop_reason = ""
        with response:
            for chunk in ndjson_lines(response):
                if chunk.get("error"):
                    raise ProviderError(f"Ollama: {chunk['error']}")
                message = chunk.get("message") or {}
                if message.get("content"):
                    text.append(message["content"])
                    on_text(message["content"])
                for tc in message.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    arguments = fn.get("arguments") or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    calls.append(ToolCall(id=f"o{len(calls) + 1}", name=fn.get("name", ""), arguments=arguments))
                if chunk.get("done"):
                    input_tokens = chunk.get("prompt_eval_count", 0)
                    output_tokens = chunk.get("eval_count", 0)
                    stop_reason = chunk.get("done_reason", "stop")
        return Response(text="".join(text), tool_calls=calls, raw=None, input_tokens=input_tokens,
                        output_tokens=output_tokens, stop_reason=stop_reason, model=request.model)
