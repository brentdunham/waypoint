"""Long-term memory, exposed so you can see what the agent thinks it knows.

Memories live in LangGraph's ``BaseStore``, namespaced ``("memories", tenant,
user)``. They are cross-thread: what the agent learns in one conversation is
available in the next one.
"""

import datetime as dt
from typing import Any

from flask import Blueprint, jsonify, request

from waypoint.agents import get_runtime
from waypoint.agents.context import memory_namespace
from waypoint.aio import loop
from waypoint.infra.security import current_principal, requires
from waypoint.schemas import SCOPE_READ, SCOPE_WRITE, RememberFact

bp = Blueprint("memories", __name__, url_prefix="/memories")


def _namespace() -> tuple[str, ...]:
    principal = current_principal()
    return memory_namespace(principal.tenant_id, principal.user_id)


@bp.get("")
@requires(SCOPE_READ)
def index() -> Any:
    runtime = get_runtime()
    query = request.args.get("q")
    limit = min(int(request.args.get("limit", 20)), 100)

    async def search() -> list[dict[str, Any]]:
        items = await runtime.store.asearch(_namespace(), query=query, limit=limit)
        return [{"key": i.key, "value": i.value, "created_at": str(i.created_at)} for i in items]

    return jsonify(memories=loop.run_sync(search(), timeout=15))


@bp.post("")
@requires(SCOPE_WRITE)
def create() -> Any:
    payload = RememberFact.model_validate(request.get_json(silent=True) or {})
    runtime = get_runtime()
    key = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
    loop.run_sync(
        runtime.store.aput(_namespace(), key, {"fact": payload.fact, "created_at": key}),
        timeout=10,
    )
    return jsonify(key=key, fact=payload.fact), 201


@bp.delete("/<key>")
@requires(SCOPE_WRITE)
def destroy(key: str) -> Any:
    runtime = get_runtime()
    loop.run_sync(runtime.store.adelete(_namespace(), key), timeout=10)
    return "", 204
