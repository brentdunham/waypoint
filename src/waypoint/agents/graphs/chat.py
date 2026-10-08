"""The `chat` agent: tool-using assistant with cross-thread long-term memory.

Demonstrates: typed runtime context, the store as memory that outlives a
thread, node-level retries, and a tools loop.
"""

import logging
from typing import Any

from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from waypoint.agents.context import AgentContext
from waypoint.agents.models import ModelRouter
from waypoint.agents.state import ChatState
from waypoint.agents.tools import CHAT_TOOLS
from waypoint.agents.types import Graph
from waypoint.settings import Settings

log = logging.getLogger(__name__)

SYSTEM = """You are a concise, accurate assistant.
Prefer calling a tool over guessing. When the user states a durable preference,
call `remember`. Answer in {locale}.

{memories}"""


ROLES = ("assistant",)
"""Each model call site. ``MODELS__CHAT__ASSISTANT`` picks its model."""


def build(settings: Settings, **compile_kwargs: Any) -> Graph:
    assistant = ModelRouter(settings, "chat", ROLES).bind(
        "assistant", lambda m: m.bind_tools(CHAT_TOOLS)
    )

    async def recall_memories(
        state: ChatState, runtime: Runtime[AgentContext]
    ) -> dict[str, list[str]]:
        """Load what we know about this user before the model sees the turn."""
        store = runtime.store
        if store is None:
            return {"recalled": []}
        last = state.get("messages", [])
        query = str(last[-1].content) if last else ""
        try:
            items = await store.asearch(runtime.context.memory_namespace, query=query, limit=5)
        except Exception:
            log.warning("memory search failed", exc_info=True)
            return {"recalled": []}
        return {"recalled": [str(i.value.get("fact", "")) for i in items]}

    async def call_model(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        memories = state.get("recalled") or []
        block = (
            "What you already know about this user:\n" + "\n".join(f"- {m}" for m in memories)
            if memories
            else ""
        )
        system = SYSTEM.format(locale=runtime.context.locale, memories=block)
        model = assistant(runtime.context)
        reply = await model.ainvoke([SystemMessage(system), *state.get("messages", [])])
        return {"messages": [reply], "step_count": state.get("step_count", 0) + 1}

    def route(state: ChatState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder = StateGraph(ChatState, context_schema=AgentContext)
    # Deliberately uncached. A node cache key is a hash of the node's *input
    # state* only; the runtime context, and with it the memory namespace, is not
    # part of it. Two callers in different tenants presenting identical state
    # would share an entry and the second would read the first one's memories.
    # The store lookup is one indexed query, and it is also the only way a fact
    # saved with `remember` a few seconds ago shows up on the next turn.
    builder.add_node("recall_memories", recall_memories)
    builder.add_node(
        "model",
        call_model,
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2.0),
    )
    builder.add_node("tools", ToolNode(CHAT_TOOLS))

    builder.add_edge(START, "recall_memories")
    builder.add_edge("recall_memories", "model")
    builder.add_conditional_edges("model", route, {"tools": "tools", END: END})
    builder.add_edge("tools", "model")

    return builder.compile(name="chat", **compile_kwargs)
