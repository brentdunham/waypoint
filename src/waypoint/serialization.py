"""Turning LangGraph state into JSON a browser can read.

Messages carry provider-specific objects and content blocks; the frontend wants
a stable, small shape. Everything crossing the HTTP boundary goes through here
so that shape is defined exactly once.
"""

from typing import Any

from langchain_core.messages import BaseMessage

MAX_DEPTH = 8


def message_to_dict(message: BaseMessage) -> dict[str, Any]:
    raw = getattr(message, "text", None)
    text = raw if isinstance(raw, str) else (raw() if callable(raw) else str(message.content))
    if not text and isinstance(message.content, list):
        text = _passages(message.content)
    out: dict[str, Any] = {
        "id": message.id,
        "type": message.type,
        "content": text,
    }
    if tool_calls := getattr(message, "tool_calls", None):
        out["tool_calls"] = [
            {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")}
            for tc in tool_calls
        ]
    if name := getattr(message, "name", None):
        out["name"] = name
    if usage := (message.response_metadata or {}).get("usage"):
        out["usage"] = usage
    if model := (message.response_metadata or {}).get("model_name"):
        out["model"] = model
    return out


PASSAGE_CHARS = 300


def _passages(content: list[Any]) -> str:
    """A ``search_documents`` result (``search_result`` blocks) as readable text:
    each passage's heading and the start of its text. The full blocks stay in
    the checkpoint; this is what the transcript and the eval judge show."""
    lines = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "search_result":
            body = " ".join(str(c.get("text", "")) for c in block.get("content") or [])
            if len(body) > PASSAGE_CHARS:
                body = body[: PASSAGE_CHARS - 1].rstrip() + "\u2026"
            lines.append(f"{block.get('title', '')}: {body}")
    return "\n".join(lines)


def jsonable(value: Any, *, depth: int = 0) -> Any:  # noqa: PLR0911 - a type switch
    if depth > MAX_DEPTH:
        return str(value)
    if isinstance(value, BaseMessage):
        return message_to_dict(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [jsonable(v, depth=depth + 1) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(), depth=depth + 1)
    if hasattr(value, "_asdict"):
        return jsonable(value._asdict(), depth=depth + 1)
    return str(value)


def snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    """A ``StateSnapshot`` flattened for the wire."""
    config = snapshot.config or {}
    parent = snapshot.parent_config or {}
    return {
        "checkpoint_id": config.get("configurable", {}).get("checkpoint_id"),
        "parent_checkpoint_id": parent.get("configurable", {}).get("checkpoint_id"),
        "created_at": snapshot.created_at,
        "next": list(snapshot.next or ()),
        "values": jsonable(snapshot.values),
        "metadata": jsonable(dict(snapshot.metadata or {})),
        "interrupts": [
            {"id": getattr(i, "id", None), "value": jsonable(i.value)}
            for i in (snapshot.interrupts or ())
        ],
        "tasks": [
            {"id": t.id, "name": t.name, "error": str(t.error) if t.error else None}
            for t in (snapshot.tasks or ())
        ],
    }
