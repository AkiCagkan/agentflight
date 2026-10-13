"""Model accounts: connecting with the user's own subscription.

  chatgpt      ChatGPT plan ("Sign in with ChatGPT"; OpenAI's open-source / local app flow).
               GPT model usage is billed to the user's ChatGPT Plus/Pro plan.
  claude_code  Claude Code on the user's computer. Sign-in uses Claude Code's own flow; AgentFlight calls Claude
               models through Claude Code (Claude subscription or a key configured in Claude Code).
               AgentFlight never reads or stores any Claude credential.

API keys (config.py) are a separate route; the settings decide which route is tried first.
"""

from __future__ import annotations


def summary() -> dict:
    """Status of all accounts for the panel and the CLI."""
    from . import chatgpt, claude_code
    return {"chatgpt": chatgpt.status(), "claude": claude_code.status()}
