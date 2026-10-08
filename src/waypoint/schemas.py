"""Wire models. Pydantic is the boundary; nothing untyped gets past it."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Message(Strict):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=100_000)


class CreateThread(Strict):
    agent: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=200)


class StartRun(Strict):
    """Either start fresh (``input``) or continue a paused graph (``resume``)."""

    input: dict[str, Any] | None = None
    message: str | None = Field(default=None, max_length=100_000)
    """Convenience: wrapped into the agent's declared input key."""
    resume: dict[str, Any] | str | None = None
    checkpoint_id: str | None = Field(default=None, max_length=200)
    """Fork from this checkpoint instead of the thread head (time travel)."""
    durability: Literal["sync", "async", "exit"] = "async"
    models: dict[str, str] | None = Field(default=None, max_length=16)
    """Role -> model for this run, e.g. ``{"critic": "claude-opus-..."}``;
    ``"default"`` covers every role not named. Models must be in MODELS_ALLOWED."""


class UpdateState(Strict):
    """Rewrite a checkpoint's values -- the write half of time travel."""

    values: dict[str, Any]
    as_node: str | None = Field(default=None, max_length=128)
    checkpoint_id: str | None = Field(default=None, max_length=200)


class CreateKey(Strict):
    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["agents:run", "threads:write"])
    label: str | None = Field(default=None, max_length=120)


class RememberFact(Strict):
    fact: str = Field(min_length=1, max_length=4_000)


SCOPE_RUN = "agents:run"
SCOPE_READ = "threads:read"
SCOPE_WRITE = "threads:write"
SCOPE_ADMIN = "keys:admin"
