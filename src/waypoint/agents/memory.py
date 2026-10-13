"""Long-term memory for the agents that talk with a person (chat, workspace).

Memories live in the LangGraph store under ``AgentContext.memory_namespace``,
one namespace per tenant and user with no agent in it, so a preference told to
one agent holds in the other and the ``/memories`` API stays one list. The
``remember`` and ``recall`` tools in ``agents/tools.py`` write and search the
same namespace.

Shared so the two graphs cannot drift (workspace design, open decision 7).
Moving this out of ``graphs/chat.py`` changed no prompt text and no behaviour.
"""

import logging
from typing import Any

from langgraph.runtime import Runtime

from waypoint.agents.context import AgentContext

log = logging.getLogger(__name__)

RECALL_LIMIT = 5


async def recall_memories(state: Any, runtime: Runtime[AgentContext]) -> dict[str, list[str]]:
    """A graph node: load what we know about this user before the model sees the turn.

    The query is the latest message. A failed search is logged and treated as
    no memories: a turn must not fail because recall did.

    Deliberately uncached by the graphs that use it. A node cache key is a hash
    of the node's input state only; the runtime context, and with it the
    memory namespace, is not part of it. Two callers in different tenants
    presenting identical state would share an entry and the second would read
    the first one's memories. The store lookup is one indexed query, and it is
    also the only way a fact saved with ``remember`` a few seconds ago shows up
    on the next turn.
    """
    store = runtime.store
    if store is None:
        return {"recalled": []}
    last = state.get("messages", [])
    query = str(last[-1].content) if last else ""
    try:
        items = await store.asearch(
            runtime.context.memory_namespace, query=query, limit=RECALL_LIMIT
        )
    except Exception:
        log.warning("memory search failed", exc_info=True)
        return {"recalled": []}
    return {"recalled": [str(i.value.get("fact", "")) for i in items]}


def memory_block(memories: list[str]) -> str:
    """The recalled memories as the system prompt shows them; empty when none."""
    if not memories:
        return ""
    return "What you already know about this user:\n" + "\n".join(f"- {m}" for m in memories)
