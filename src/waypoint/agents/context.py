"""Typed runtime context.

LangGraph 1.x separates three things that used to be one dict:

* **state** -- mutated by nodes, checkpointed, part of the conversation;
* **context** -- immutable per-run facts (who is calling, which tenant);
* **config** -- plumbing (thread id, recursion limit, callbacks).

Putting the caller's identity in *context* rather than state means it never
gets checkpointed into a thread and can't be replayed with stale credentials.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AgentContext:
    tenant_id: str
    user_id: str
    request_id: str
    run_id: str
    agent: str
    locale: str = "en-US"
    models: tuple[tuple[str, str], ...] = ()
    """Role -> model for this run, resolved once when the run starts
    (:func:`waypoint.agents.models.resolve_models`). A tuple of pairs, not a dict, so
    the context stays hashable. Empty -> each node resolves from settings."""
    tags: tuple[str, ...] = field(default_factory=tuple)
    scopes: tuple[str, ...] = ()
    """The scopes of the key that started this run. Workspace offers the tools
    of the MCP servers they allow (``tools:<server>``; docs/design/tools.md, T8).
    Each run, a resume included, carries the scopes of the key that sent it."""
    thread_id: str = ""

    def model_for(self, role: str) -> str | None:
        return dict(self.models).get(role)

    @property
    def memory_namespace(self) -> tuple[str, ...]:
        """Where this caller's long-term memories live in the store."""
        return memory_namespace(self.tenant_id, self.user_id)


def memory_namespace(tenant_id: str, user_id: str) -> tuple[str, ...]:
    """The one definition of a memory namespace.

    The graphs reach it through ``AgentContext.memory_namespace`` and the
    ``/memories`` API through the request principal. Both must land on the same
    tuple, or the API shows a user memories the agent cannot see.
    """
    return ("memories", tenant_id, user_id)
