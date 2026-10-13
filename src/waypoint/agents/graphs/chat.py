"""The `chat` agent: tool-using assistant with cross-thread long-term memory.

Demonstrates: typed runtime context, the store as memory that outlives a
thread, node-level retries, and a tools loop.
"""

from typing import Any

from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from waypoint.agents.context import AgentContext
from waypoint.agents.memory import memory_block, recall_memories
from waypoint.agents.models import ModelRouter, stamp_model
from waypoint.agents.state import ChatState
from waypoint.agents.tools import CHAT_TOOLS
from waypoint.agents.types import Graph
from waypoint.settings import Settings

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

    async def call_model(state: ChatState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        block = memory_block(state.get("recalled") or [])
        system = SYSTEM.format(locale=runtime.context.locale, memories=block)
        model = assistant(runtime.context)
        reply = await model.ainvoke([SystemMessage(system), *state.get("messages", [])])
        stamp_model(reply, assistant.name(runtime.context))
        return {"messages": [reply], "step_count": state.get("step_count", 0) + 1}

    def route(state: ChatState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder = StateGraph(ChatState, context_schema=AgentContext)
    # Deliberately uncached: see agents/memory.py.
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
