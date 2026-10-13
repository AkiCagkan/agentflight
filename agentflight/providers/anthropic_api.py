"""Anthropic Claude provider (official `anthropic` Python SDK, streaming).

Install: pip install anthropic   (or: pip install -e ".[claude]")

Notes:
- Current Claude models (Opus 5.5, Sonnet 5.5, Fable...) do not accept a temperature setting; it
  is only sent to older models that support it. For these models, use record/replay for
  reproducibility.
- Server-side fallback models are enabled for Opus 5.5 / Opus 5 / Sonnet 5.5 / Fable 5.1: if the
  safety classifier declines a request, it is routed to a suitable model. Set
  AGENTFLIGHT_CLAUDE_FALLBACK=0 to turn this off.
- Tool inputs stream with `eager_input_streaming`; AgentFlight validates them against the schema before
  running the tool.
"""

from __future__ import annotations

import os

from ..errors import ModelRefused, ProviderError
from .base import Provider, Request, Response, ToolCall

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _supports_temperature(model: str) -> bool:
    return model.startswith(("claude-haiku-4", "claude-sonnet-4", "claude-opus-4-6", "claude-opus-4-5", "claude-3"))


class AnthropicProvider(Provider):
    name = "anthropic"
    key_env_var = "ANTHROPIC_API_KEY"

    def __init__(self):
        self._client = None
        self._fallback_disabled = os.environ.get("AGENTFLIGHT_CLAUDE_FALLBACK", "1") == "0"

    def is_ready(self):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "the anthropic package is not installed (pip install anthropic)"
        if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")):
            return True, ""
        return False, "ANTHROPIC_API_KEY is not set"

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise ProviderError("Claude needs the 'anthropic' package: pip install anthropic") from e
            self._client = anthropic.Anthropic(max_retries=3)
        return self._client

    @staticmethod
    def _messages(request: Request) -> list[dict]:
        result: list[dict] = []

        def add_to_user(blocks):
            if result and result[-1]["role"] == "user":
                previous = result[-1]["content"]
                if isinstance(previous, str):
                    previous = [{"type": "text", "text": previous}]
                result[-1]["content"] = previous + blocks
            else:
                result.append({"role": "user", "content": blocks})

        for m in request.messages:
            if m["role"] == "user":
                add_to_user([{"type": "text", "text": m["content"]}])
            elif m["role"] == "assistant":
                if m.get("provider") == "anthropic" and m.get("raw"):
                    content = m["raw"]
                else:
                    content = []
                    if m.get("content"):
                        content.append({"type": "text", "text": m["content"]})
                    for c in m.get("tool_calls", []):
                        content.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
                result.append({"role": "assistant", "content": content})
            elif m["role"] == "tool":
                add_to_user([
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     "is_error": bool(r.get("error"))}
                    for r in m["results"]
                ])
        return result

    def params(self, request: Request) -> dict:
        p: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens or 64000,
            "messages": self._messages(request),
        }
        if request.system:
            p["system"] = request.system
        if request.tools:
            p["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["schema"],
                 "eager_input_streaming": True}
                for t in request.tools
            ]
        output: dict = {}
        if request.strict_schema is not None:
            output["format"] = {"type": "json_schema", "schema": request.strict_schema}
        if request.effort:
            output["effort"] = request.effort
        if output:
            p["output_config"] = output
        if request.temperature is not None and _supports_temperature(request.model):
            p["temperature"] = request.temperature
        return p

    def _open_stream(self, client, p: dict, with_fallback: bool):
        if with_fallback:
            return client.beta.messages.stream(**p, betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"})
        return client.messages.stream(**p)

    def call(self, request: Request, on_text) -> Response:
        import anthropic

        client = self.client()
        p = self.params(request)
        with_fallback = request.model in FALLBACK_MODELS and not self._fallback_disabled
        json_attempts = 0
        while True:
            try:
                with self._open_stream(client, p, with_fallback) as stream:
                    for event in stream:
                        if event.type == "text":
                            on_text(event.text)
                    final = stream.get_final_message()
                break
            except ValueError:
                # Tool input JSON the SDK could not parse at all: ask for the turn again (limited).
                json_attempts += 1
                if json_attempts > 2:
                    raise ProviderError("Claude produced invalid tool input (3 attempts)") from None
            except anthropic.BadRequestError as e:
                if with_fallback and "fallback" in str(e).lower():
                    with_fallback = False
                    self._fallback_disabled = True
                    continue
                raise ProviderError(f"Claude rejected the request (400): {e}") from None
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                error = ProviderError(f"Claude access error: {e}")
                error.status = e.status_code
                raise error from None
            except anthropic.RateLimitError as e:
                raise ProviderError(f"Claude rate limit: {e}") from None
            except anthropic.APIStatusError as e:
                error = ProviderError(f"Claude API error ({e.status_code}): {e}")
                error.status = e.status_code
                raise error from None
            except anthropic.APIConnectionError as e:
                raise ProviderError(f"Claude connection error: {e}") from None

        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ModelRefused("Claude declined the request" + f" (refusal{', ' + category if category else ''})")
        text = "".join(b.text for b in final.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=b.input if isinstance(b.input, dict) else {})
            for b in final.content if b.type == "tool_use"
        ]
        if final.stop_reason == "max_tokens" and calls:
            raise ProviderError("Claude's tool input was cut off at max_tokens")
        usage = final.usage
        input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        return Response(
            text=text,
            tool_calls=calls,
            raw=[b.model_dump(mode="json", exclude_none=True) for b in final.content],
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens or 0,
            stop_reason=final.stop_reason or "",
            model=getattr(final, "model", request.model),
        )
