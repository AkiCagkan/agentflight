"""Shared parts of the provider layer.

The AgentFlight runtime talks to models in a provider-neutral message format. Each provider translates
that format into its own API format.

Neutral messages:
  {"role": "user", "content": "..."}
  {"role": "assistant", "content": "...", "tool_calls": [{"id", "name", "arguments"}],
   "raw": <provider-specific content>, "provider": "gemini"}
  {"role": "tool", "results": [{"id", "name", "content", "error": bool}]}

`raw` keeps the content the model produced itself (thinking blocks, signatures) so it can be sent
back unchanged while the conversation continues with the same provider.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from ..errors import ProviderError


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Request:
    model: str
    system: str
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)  # {"name", "description", "schema"}
    schema: dict | None = None          # full JSON schema (in the prompt)
    strict_schema: dict | None = None   # simplified schema for native schema modes
    temperature: float | None = None
    seed: int | None = None
    effort: str | None = None
    max_tokens: int | None = None


@dataclass
class Response:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: object = None
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    model: str = ""


StreamCallback = Callable[[str], None]


class Provider:
    name = "base"
    key_env_var: str | None = None
    subscription = False    # requests are billed to the user's plan; cost is not counted in the AgentFlight budget
    session_based = False   # the provider runs the tool loop itself (see open_session)

    def is_ready(self) -> tuple[bool, str]:
        return True, ""

    def call(self, request: Request, on_text: StreamCallback) -> Response:  # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# HTTP helpers (for providers without an SDK)

RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


def _error_text(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return body.decode("utf-8", "replace")[:500]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            return str(error.get("message") or error)[:500]
        return str(error)[:500]
    return str(data)[:500]


def http_open(url: str, body: dict | None, headers: dict, timeout: float = 300, attempts: int = 3):
    """Opens a POST request (GET without a body); retries transient errors with exponential backoff."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last_error = None
    for i in range(attempts):
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers},
                                         method="POST" if data is not None else "GET")
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            text = _error_text(e.read())
            last_error = ProviderError(f"HTTP {e.code}: {text}")
            last_error.status = e.code
            if e.code not in RETRYABLE or i == attempts - 1:
                raise last_error from None
        except urllib.error.URLError as e:
            last_error = ProviderError(f"connection error: {e.reason}")
            last_error.status = None
            if i == attempts - 1:
                raise last_error from None
        except TimeoutError:
            last_error = ProviderError("timed out")
            last_error.status = None
            if i == attempts - 1:
                raise last_error from None
        time.sleep(min(8, 1.5 * (2 ** i)))
    raise last_error  # pragma: no cover


def sse_events(response) -> Iterator[tuple[str | None, str]]:
    """Splits a Server-Sent Events stream into (event_name, data) pairs."""
    event_name, data_lines = None, []
    for raw in response:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def ndjson_lines(response) -> Iterator[dict]:
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def tool_result_text(result: dict) -> str:
    return result.get("content", "")
