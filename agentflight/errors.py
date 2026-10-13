"""AgentFlight's exception classes."""

from __future__ import annotations


class AgentError(Exception):
    """Base class of all AgentFlight errors."""


class SchemaError(AgentError):
    """A value did not match the expected type / schema."""

    def __init__(self, errors, message=None):
        if isinstance(errors, str):
            errors = [errors]
        self.errors: list[str] = list(errors)
        super().__init__(message or "; ".join(self.errors))


class InstructionsNotFound(AgentError):
    """The agent's instructions file was not found."""


class ProviderError(AgentError):
    """The model provider returned an error."""


class UnknownModel(AgentError):
    """The model name matched no provider."""


class BudgetExceeded(AgentError):
    """The budget of an agent or of the run was exceeded."""


class MaxTurnsExceeded(AgentError):
    """The agent did not reach a result within the allowed number of turns."""


class ApprovalRejected(AgentError):
    """A human rejected an approval request."""


class ModelRefused(AgentError):
    """The model declined to answer."""


class SetupError(AgentError):
    """A run could not be set up (unreadable agentflight.toml, unknown or unreadable run record...)."""


class AgentStopped(Exception):
    """The agent was stopped by the user (internal)."""
