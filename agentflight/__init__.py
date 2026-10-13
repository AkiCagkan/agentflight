"""AgentFlight: write LLM agents in plain Python, watch them run, step in, replay and test them.

    from agentflight import Agent, llm
    from pydantic import BaseModel

    class Summary(BaseModel):
        title: str
        bullets: list[str]

    class Summarizer(Agent):
        \"\"\"prompts/summary.md\"\"\"
        model = "claude-opus-5-5"

        @llm
        def summarize(self, text: str) -> Summary: ...

Run with `python main.py` (live log in the terminal) or `agentflight run main.py`; open the panel with
`agentflight watch` to see every agent call live, pause it, message it, approve steps and replay a run
from any step. See README.md.
"""

__version__ = "0.4.0"

from .agents import Agent, llm  # noqa: F401
from .errors import (  # noqa: F401
    AgentError,
    AgentStopped,
    ApprovalRejected,
    BudgetExceeded,
    InstructionsNotFound,
    MaxTurnsExceeded,
    ModelRefused,
    ProviderError,
    SchemaError,
    UnknownModel,
)
from .evals import Eval  # noqa: F401
from .flow import parallel, parallel_for_each, parallel_map, require_approval  # noqa: F401
from .schema import Schema, field  # noqa: F401
from .tools import mcp_server, tool  # noqa: F401

__all__ = [
    "Agent", "llm", "Schema", "field", "Eval", "tool", "mcp_server", "require_approval",
    "parallel", "parallel_map", "parallel_for_each",
    "AgentError", "AgentStopped", "ApprovalRejected", "BudgetExceeded", "InstructionsNotFound",
    "MaxTurnsExceeded", "ModelRefused", "ProviderError", "SchemaError", "UnknownModel",
]
