"""Nalar Agent: a local-first coding agent with BYOK inference."""

__version__ = "0.7.281"


def __getattr__(name):
    if name == "Agent":
        from .sdk import Agent

        return Agent
    if name == "WorkspaceAgent":
        from .sdk import WorkspaceAgent

        return WorkspaceAgent
    raise AttributeError(name)
