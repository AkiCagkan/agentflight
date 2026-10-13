"""Provider that runs GPT models on the user's ChatGPT plan (Responses API).

Sign-in: accounts/chatgpt.py ("Sign in with ChatGPT"). The rules of this flow:
  - POST https://api.openai.com/v1/responses, `store: false`, `stream: true`,
  - the system text goes in `instructions` (the system role is not accepted),
  - temperature, max_output_tokens, top_p ... are not sent,
  - function tools are given inside a namespace,
  - the history is sent in `input` with every request (no previous_response_id); reasoning items
    are carried with `reasoning.encrypted_content`,
  - only `response.completed` counts as success; plan usage limit errors are not retried.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..errors import ModelRefused, ProviderError
from .base import Provider, Request, Response, ToolCall, sse_events

PREFIX = "chatgpt/"
NAMESPACE = "agentflight"
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

_ERROR_EXPLANATIONS = {
    "subscription_sharing_usage_limit_exceeded":
        "You reached the usage limit of your ChatGPT plan (or of this app). "
        "Manage usage: https://chatgpt.com/settings/usage",
    "subscription_sharing_user_not_eligible": "This ChatGPT account or workspace is not eligible for plan usage.",
    "subscription_sharing_usage_unavailable": "ChatGPT plan usage cannot be checked right now; try again shortly.",
    "subscription_sharing_user_unavailable":
        "ChatGPT account information is temporarily unavailable; try again shortly.",
    "subscription_sharing_invalid_user":
        "The ChatGPT session could not be verified; sign in again in Settings › Accounts.",
    "subscription_sharing_route_not_supported": "This request is not supported through the ChatGPT plan.",
    "chatpass_v2_scope_not_authorized": "The ChatGPT plan permission does not cover this operation.",
    "chatpass_v2_invalid_authorization_context": "The ChatGPT plan permission does not cover this operation.",
}
_RETRY = {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"}


def _error(code: str | None, message: str, status: int | None, param: str | None = None) -> ProviderError:
    explanation = _ERROR_EXPLANATIONS.get(code or "") or None
    if code == "subscription_sharing_unsupported_capability":
        explanation = f"The ChatGPT plan does not support this feature ({param or message})."
    err = ProviderError(explanation or f"ChatGPT: {message}" + (f" [{code}]" if code else ""))
    err.status = status
    err.code = code
    return err


class ChatGPTProvider(Provider):
    name = "chatgpt"
    subscription = True

    def is_ready(self):
        from ..accounts import chatgpt
        if chatgpt.is_connected():
            return True, ""
        return False, "ChatGPT account not connected (Settings › Accounts › Continue with ChatGPT)"

    @staticmethod
    def _input(request: Request) -> list[dict]:
        items: list[dict] = []
        for m in request.messages:
            if m["role"] == "user":
                items.append({"type": "message", "role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if m.get("provider") == "chatgpt" and isinstance(m.get("raw"), list):
                    items.extend(m["raw"])
                    continue
                if m.get("content"):
                    items.append({"type": "message", "role": "assistant", "content": m["content"]})
                for c in m.get("tool_calls") or []:
                    items.append({"type": "function_call", "call_id": c["id"], "name": c["name"],
                                  "arguments": json.dumps(c["arguments"], ensure_ascii=False)})
            elif m["role"] == "tool":
                for r in m["results"]:
                    items.append({"type": "function_call_output", "call_id": r["id"], "output": r["content"]})
        return items

    def _body(self, request: Request, model: str) -> dict:
        body: dict = {
            "model": model, "input": self._input(request), "store": False, "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if request.system:
            body["instructions"] = request.system
        if request.tools:
            body["tools"] = [{
                "type": "namespace", "name": NAMESPACE, "description": "Tools this agent can use.",
                "tools": [{"type": "function", "name": t["name"], "description": t["description"] or t["name"],
                           "parameters": t["schema"], "strict": False} for t in request.tools],
            }]
        if request.strict_schema is not None:
            body["text"] = {"format": {"type": "json_schema", "name": "output", "schema": request.strict_schema,
                                       "strict": False}}
        if request.effort in _EFFORT:
            body["reasoning"] = {"effort": _EFFORT[request.effort]}
        return body

    def _open(self, request: Request, model: str):
        from ..accounts import chatgpt

        body = json.dumps(self._body(request, model), ensure_ascii=False).encode("utf-8")
        force = False
        for attempt in range(4):
            try:
                token, _ = chatgpt.access_token(force_refresh=force)
            except chatgpt.ChatGPTError as e:
                err = ProviderError(str(e))
                err.status = 401
                raise err from None
            r = urllib.request.Request(f"{chatgpt.api_root()}/responses", data=body, method="POST", headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream",
            })
            try:
                return urllib.request.urlopen(r, timeout=600)
            except urllib.error.HTTPError as e:
                raw = e.read()
                request_id = e.headers.get("x-request-id", "")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = {"detail": raw.decode("utf-8", "replace")[:300]}
                error = data.get("error") if isinstance(data.get("error"), dict) else {}
                code = error.get("code")
                message = error.get("message") or data.get("detail") or f"HTTP {e.code}"
                if request_id:
                    message += f" (request {request_id})"
                if e.code == 401 and not force and code != "subscription_sharing_invalid_user":
                    force = True  # the token may have been invalidated: refresh once and retry
                    continue
                if (code in _RETRY or (e.code == 503 and not code)) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise _error(code, message, e.code, error.get("param")) from None
            except urllib.error.URLError as e:
                if attempt < 1:
                    time.sleep(1.5)
                    continue
                err = ProviderError(f"ChatGPT connection error: {e.reason}")
                err.status = None
                raise err from None
        raise ProviderError("could not send the ChatGPT request")

    def call(self, request: Request, on_text) -> Response:
        model = request.model[len(PREFIX):] if request.model.startswith(PREFIX) else request.model
        response = self._open(request, model)
        text: list[str] = []
        items: list[dict] = []
        completed = False
        input_tokens = output_tokens = 0
        stop_reason = ""
        with response:
            for _, data in sse_events(response):
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                t = event.get("type", "")
                if t == "response.output_text.delta":
                    text.append(event.get("delta", ""))
                    on_text(event.get("delta", ""))
                elif t == "response.refusal.delta":
                    continue
                elif t == "response.output_item.done":
                    items.append(event.get("item") or {})
                elif t == "response.completed":
                    completed = True
                    r = event.get("response") or {}
                    usage = r.get("usage") or {}
                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    if r.get("output"):
                        items = r["output"]
                    stop_reason = "completed"
                    break
                elif t == "response.incomplete":
                    reason = ((event.get("response") or {}).get("incomplete_details") or {}).get("reason", "")
                    raise _error(None, f"response incomplete ({reason})", None)
                elif t in ("response.failed", "error"):
                    error = ((event.get("response") or {}).get("error") if t == "response.failed" else event) or {}
                    if isinstance(error.get("error"), dict):
                        error = error["error"]
                    raise _error(error.get("code"), error.get("message") or "response failed", None, error.get("param"))
        if not completed:
            raise ProviderError("the ChatGPT stream ended before completion")
        calls = []
        output_text = []
        for item in items:
            kind = item.get("type")
            if kind == "function_call":
                name = item.get("name", "")
                if item.get("namespace") == NAMESPACE or name.startswith(NAMESPACE + "."):
                    name = name.split(".", 1)[1] if name.startswith(NAMESPACE + ".") else name
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {"__invalid_json__": item.get("arguments")}
                calls.append(ToolCall(id=item.get("call_id") or item.get("id", ""), name=name, arguments=arguments))
            elif kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        output_text.append(part.get("text", ""))
                    elif part.get("type") == "refusal":
                        raise ModelRefused("the model declined: " + str(part.get("refusal", "")))
        full = "".join(output_text) if output_text else "".join(text)
        # Items sent back in the history (with store: false, item ids stored on the server are not
        # referenced; reasoning items are carried with their encrypted content).
        raw = [{k: v for k, v in o.items() if k != "status" and (k != "id" or o.get("type") == "reasoning")}
               for o in items if o.get("type") in ("message", "function_call", "reasoning")]
        return Response(text=full, tool_calls=calls, raw=raw, input_tokens=input_tokens, output_tokens=output_tokens,
                        stop_reason=stop_reason, model=model)
