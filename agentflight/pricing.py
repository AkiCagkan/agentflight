"""Model prices (USD per 1 million tokens): (input, output).

Claude prices are Anthropic's published first-party API prices (checked on 13 October 2026 against
https://platform.claude.com/docs/en/about-claude/pricing), including the higher price of long prompts where a
model has one. Prices of other providers may change; those values are estimates. Project-specific prices can
override them in the [prices] table of `agentflight.toml`:

    [prices]
    "gemini-flash-latest" = [0.30, 2.50]
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    # Google (estimated)
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-flash-latest": (0.30, 2.50),
    "gemini-flash-lite-latest": (0.10, 0.40),
    "gemini-pro-latest": (1.25, 10.00),
    # OpenAI (estimated)
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
}

EXACT_PROVIDERS = ("claude-",)

# Models whose price goes up when the prompt is longer than a threshold: model -> (input tokens, price).
LONG_PROMPT_PRICES: dict[str, tuple[int, tuple[float, float]]] = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50)),
}

_EXTRA: dict[str, tuple[float, float]] = {}


def add_prices(table: dict) -> None:
    for model, value in (table or {}).items():
        try:
            input_price, output_price = value
            _EXTRA[str(model)] = (float(input_price), float(output_price))
        except (TypeError, ValueError):
            continue


def find_price(model: str) -> tuple[float, float] | None:
    if model in _EXTRA:
        return _EXTRA[model]
    if model in PRICES:
        return PRICES[model]
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name):
            return PRICES[name]
    return None


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price = find_price(model)
    if price is None:
        return 0.0
    if model not in _EXTRA:
        for name, (threshold, long_price) in LONG_PROMPT_PRICES.items():
            if model.startswith(name) and input_tokens > threshold:
                price = long_price
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def is_estimate(model: str) -> bool:
    return model not in _EXTRA and not model.startswith(EXACT_PROVIDERS)
