"""Tools available to the agents.

The docstring *is* the prompt the model reads, so it is written for the model.
Tools that need the long-term store reach it through ``get_store()`` rather
than a captured reference, so the same tool object works across graphs.
"""

import datetime as dt
import zoneinfo

from langchain_core.tools import tool
from langgraph.config import get_store
from langgraph.runtime import get_runtime

from waypoint.agents.context import AgentContext


@tool
def now(timezone: str = "UTC") -> str:
    """Return the current time as an ISO-8601 string.

    Args:
        timezone: IANA timezone name, e.g. "America/Chicago".
    """
    zone: dt.tzinfo
    try:
        zone = zoneinfo.ZoneInfo(timezone)
    except Exception:
        zone = dt.UTC
    return dt.datetime.now(zone).isoformat()


@tool
def calculate(expression: str) -> str:
    """Evaluate a arithmetic expression such as "1240 * 0.87 + 12".

    Only numbers and the operators + - * / ( ) % ** are permitted.
    """
    import ast

    allowed = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub,
        ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.FloorDiv,
    )  # fmt: skip
    try:
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                return f"Refused: {type(node).__name__} is not allowed in an expression."
        return str(eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {}))
    except Exception as exc:
        return f"Could not evaluate: {exc}"


@tool
async def remember(fact: str) -> str:
    """Store a durable fact about the user, e.g. "prefers metric units".

    Use this only for stable preferences and facts, never for the content of
    the current question.
    """
    store = get_store()
    namespace = _namespace()
    key = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
    await store.aput(namespace, key, {"fact": fact, "created_at": key})
    return f"Remembered: {fact}"


@tool
async def recall(query: str, limit: int = 5) -> str:
    """Search previously remembered facts about the user."""
    store = get_store()
    items = await store.asearch(_namespace(), query=query, limit=limit)
    if not items:
        return "No memories matched."
    return "\n".join(f"- {item.value.get('fact', '')}" for item in items)


def _namespace() -> tuple[str, ...]:
    """Namespace memories per tenant and user so they can never leak sideways.

    Identity comes from the run's context, the same source the graph nodes use.
    There is deliberately no fallback: a tool run without a context is a bug,
    and silently filing memories under a shared default would hide it.
    """
    return get_runtime(AgentContext).context.memory_namespace


CHAT_TOOLS = [now, calculate, remember, recall]
RESEARCH_TOOLS = [now, calculate]
