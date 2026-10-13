"""Registry that finds the provider for a model name.

In agent code the model name is written independently of the provider (`claude-opus-5-5`,
`gpt-5.5`...). Connected accounts and API keys decide which route a request takes:

  claude-*          -> the user's Claude subscription if Claude Code is connected (claude_code),
                       otherwise the Anthropic API with ANTHROPIC_API_KEY
  gpt-*, o1/o3/o4-* -> the user's ChatGPT plan if a ChatGPT account is connected (chatgpt),
                       otherwise the OpenAI API with OPENAI_API_KEY
  gemini-*, gemma-* -> Google Gemini with GEMINI_API_KEY
  If both exist, the priority in Settings › Accounts applies (default: account first).

Prefixes that force a route:
  claude-code/<model>   Claude Code (subscription)  anthropic/<model>   Anthropic API key
  chatgpt/<model>       ChatGPT plan                openai/<model>      OpenAI API or OPENAI_BASE_URL
  lmstudio/<model>      LM Studio (local)           ollama/<model>      Ollama (local)
  fake, fake:bad        offline fake model
  auto                  the first connected/configured route; else a local model; else the fake model
"""

from __future__ import annotations

import os
import threading

from ..errors import UnknownModel
from .base import Provider

_LOCK = threading.Lock()
_INSTANCES: dict[str, Provider] = {}

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "gemini": "gemini-flash-latest",
    "openai": "gpt-5",
}
CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")
_GPT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-", "codex-")


def _instance(key: str, factory) -> Provider:
    with _LOCK:
        if key not in _INSTANCES:
            _INSTANCES[key] = factory()
        return _INSTANCES[key]


def account_priority(provider: str) -> str:
    """"account" (subscription first) or "key" (API key first)."""
    from .. import config
    value = (config.read().get("account_priority") or {}).get(provider, "account")
    return value if value in ("account", "key") else "account"


def _claude_code_connected() -> bool:
    from ..accounts import claude_code
    try:
        return claude_code.is_connected()
    except Exception:  # noqa: BLE001
        return False


def _chatgpt_connected() -> bool:
    from ..accounts import chatgpt
    try:
        return chatgpt.is_connected()
    except Exception:  # noqa: BLE001
        return False


def _has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _claude_code():
    from .claude_code import ClaudeCodeProvider
    return _instance("claude_code", ClaudeCodeProvider)


def _anthropic():
    from .anthropic_api import AnthropicProvider
    return _instance("anthropic", AnthropicProvider)


def _chatgpt():
    from .chatgpt import ChatGPTProvider
    return _instance("chatgpt", ChatGPTProvider)


def _openai():
    from .openai_api import OpenAIProvider
    return _instance("openai", OpenAIProvider)


def find_provider(model: str) -> tuple[Provider, str]:
    """Returns (provider, model name to send to the provider)."""
    model = (model or "").strip()
    if model == "auto" or not model:
        model = auto_model()
    if model == "fake" or model.startswith("fake:"):
        from .fake import FakeProvider
        return _instance("fake", FakeProvider), model
    if model.startswith("claude-code/"):
        return _claude_code(), model.split("/", 1)[1]
    if model.startswith("anthropic/"):
        return _anthropic(), model.split("/", 1)[1]
    if model.startswith("claude"):
        has_key = _has_anthropic_key() and "anthropic" not in _UNAVAILABLE
        if "claude_code" not in _UNAVAILABLE and (not has_key or account_priority("anthropic") == "account") \
                and _claude_code_connected():
            return _claude_code(), model
        return _anthropic(), model
    if model.startswith(("gemini", "gemma")):
        from .gemini import GeminiProvider
        return _instance("gemini", GeminiProvider), model
    if model.startswith("chatgpt/"):
        return _chatgpt(), model.split("/", 1)[1]
    if model.startswith(_GPT_PREFIXES):
        has_key = bool(os.environ.get("OPENAI_API_KEY")) and "openai" not in _UNAVAILABLE
        if "chatgpt" not in _UNAVAILABLE and (not has_key or account_priority("openai") == "account") \
                and _chatgpt_connected():
            return _chatgpt(), model
        return _openai(), model
    if model.startswith("openai/"):
        return _openai(), model.split("/", 1)[1]
    if model.startswith("lmstudio/"):
        from .openai_api import OpenAIProvider
        base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
        return (_instance("lmstudio", lambda: OpenAIProvider(base_url=base_url, api_key="lm-studio", name="lmstudio")),
                model.split("/", 1)[1])
    if model.startswith("ollama/"):
        from .ollama import OllamaProvider
        return _instance("ollama", OllamaProvider), model.split("/", 1)[1]
    raise UnknownModel(
        f"unknown model '{model}'. Examples: "
        + "claude-opus-5-5, gpt-5, gemini-flash-latest, ollama/llama3.2, lmstudio/<model>, fake"
    )


def lmstudio_models(timeout: float = 0.4) -> list[tuple[str, bool]]:
    """Language models in LM Studio: (name, is_loaded). Empty list if the server is not running."""
    import json
    import urllib.request

    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").rstrip("/")
    root = base_url[:-3] if base_url.endswith("/v1") else base_url
    try:
        with urllib.request.urlopen(f"{root}/api/v0/models", timeout=timeout) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001 - LM Studio not running or not answering
        return []
    return [(m["id"], m.get("state") == "loaded") for m in data.get("data", []) if m.get("type") in ("llm", "vlm")]


_UNAVAILABLE: set[str] = set()  # providers that returned an auth/access error in this process


def mark_unavailable(provider_name: str) -> None:
    with _LOCK:
        _UNAVAILABLE.add(provider_name)


def _chatgpt_default() -> str | None:
    from ..accounts import chatgpt
    try:
        models = chatgpt.active_models()
    except Exception:  # noqa: BLE001
        models = []
    return models[0]["slug"] if models else None


def auto_candidates() -> list[str]:
    """Candidate models for `auto` in priority order (the last one is always the fake model)."""
    candidates: list[tuple[str, str]] = []
    anthropic_key = False
    if _has_anthropic_key():
        try:
            import anthropic  # noqa: F401
            anthropic_key = True
        except ImportError:
            pass
    claude_account = _claude_code_connected()
    if claude_account and (not anthropic_key or account_priority("anthropic") == "account"):
        candidates.append(("claude_code", "claude-code/" + DEFAULT_MODELS["anthropic"]))
    if anthropic_key:
        candidates.append(("anthropic", "anthropic/" + DEFAULT_MODELS["anthropic"]))
    if claude_account and anthropic_key and account_priority("anthropic") == "key":
        candidates.append(("claude_code", "claude-code/" + DEFAULT_MODELS["anthropic"]))
    gpt_account = _chatgpt_connected() and _chatgpt_default()
    if gpt_account and (not os.environ.get("OPENAI_API_KEY") or account_priority("openai") == "account"):
        candidates.append(("chatgpt", "chatgpt/" + gpt_account))
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        candidates.append(("gemini", DEFAULT_MODELS["gemini"]))
    if os.environ.get("OPENAI_API_KEY"):
        candidates.append(("openai", "openai/" + DEFAULT_MODELS["openai"]))
    if gpt_account and os.environ.get("OPENAI_API_KEY") and account_priority("openai") == "key":
        candidates.append(("chatgpt", "chatgpt/" + gpt_account))
    from .ollama import ollama_models
    candidates += [("ollama", "ollama/" + m) for m in ollama_models()[:1]]
    candidates += [("lmstudio", "lmstudio/" + name) for name, loaded in lmstudio_models() if loaded][:1]
    return [model for name, model in candidates if name not in _UNAVAILABLE] + ["fake"]


def auto_model() -> str:
    """Picks the default model from the connected accounts, keys and local servers."""
    choice = (os.environ.get("AGENTFLIGHT_MODEL") or "").strip()
    if choice and choice != "auto":
        return choice
    return auto_candidates()[0]


def available_models() -> list[dict]:
    """Models for the model picker: {name, label, group, group_id, route, route_id, ready, reason}.

    `name` is the name written in agent code; `route` / `route_id` is the account or key the request goes
    through (route_id: claude_code, anthropic, chatgpt, openai, gemini, ollama, lmstudio, fake)."""
    from ..accounts import chatgpt, claude_code
    from .gemini import GeminiProvider
    from .ollama import ollama_models

    models: list[dict] = []

    def add(name, label, group, route, ready, reason="", route_id="", group_id=""):
        models.append({"name": name, "label": label, "group": group, "group_id": group_id or group.lower(),
                       "route": route, "route_id": route_id if ready else "", "ready": ready, "reason": reason})

    # Claude
    cc = claude_code.status()
    has_key = _has_anthropic_key()
    account_first = account_priority("anthropic") == "account"
    if cc["signed_in"] and (not has_key or account_first):
        route, ready, reason = "Claude subscription (Claude Code)", True, ""
        route_id = "claude_code"
    elif has_key:
        route, ready, reason = "Anthropic API key", True, ""
        route_id = "anthropic"
    else:
        route, ready, route_id = "", False, ""
        reason = ("Claude not connected: Settings › Accounts › Claude"
                  if cc["installed"] else "Claude Code is not installed")
    for m in CLAUDE_MODELS:
        add(m, m, "Claude", route, ready, reason, route_id)

    # GPT
    gpt_models = chatgpt.active_models() if chatgpt.is_connected() else []
    openai_key = bool(os.environ.get("OPENAI_API_KEY"))
    if gpt_models and (not openai_key or account_priority("openai") == "account"):
        for m in gpt_models:
            add(m["slug"], m["name"], "GPT", "ChatGPT plan", True, route_id="chatgpt")
    elif openai_key:
        for m in ("gpt-5", "gpt-5-mini"):
            add(m, m, "GPT", "OpenAI API key", True, route_id="openai")
    else:
        for m in ("gpt-5", "gpt-5-mini"):
            add(m, m, "GPT", "", False, "ChatGPT not connected: Settings › Accounts › Continue with ChatGPT")

    # Gemini
    ready, reason = GeminiProvider().is_ready()
    for m in ("gemini-flash-latest", "gemini-pro-latest", "gemini-2.5-flash"):
        add(m, m, "Gemini", "Gemini API key" if ready else "", ready, reason, "gemini")

    # local
    for m in ollama_models():
        add("ollama/" + m, m, "Local", "Ollama", True, route_id="ollama", group_id="local")
    for name, loaded in lmstudio_models():
        add("lmstudio/" + name, name, "Local", "LM Studio", loaded,
            "" if loaded else "not loaded in LM Studio", "lmstudio", "local")
    add("fake", "fake (offline demo model)", "Local", "offline", True, route_id="fake", group_id="local")
    add("fake:bad", "fake:bad (format error on the first try)", "Local", "offline", True, route_id="fake",
        group_id="local")
    return models
