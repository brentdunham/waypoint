"""Long-term memory, exposed so you can see what the agent thinks it knows.

Memories live in LangGraph's ``BaseStore``, namespaced ``("memories", tenant,
user)``. They are cross-thread: what the agent learns in one conversation is
available in the next one.
"""

import datetime as dt

from fastapi import APIRouter, Response

from waypoint.agents.context import memory_namespace
from waypoint.api.deps import Reader, Runtime, Writer
from waypoint.infra.security import Principal
from waypoint.schemas import Memory, MemoryCreated, MemoryList, RememberFact

router = APIRouter(prefix="/memories", tags=["memories"])


def _namespace(principal: Principal) -> tuple[str, ...]:
    return memory_namespace(principal.tenant_id, principal.user_id)


@router.get("")
async def index(
    runtime: Runtime, principal: Reader, q: str | None = None, limit: int = 20
) -> MemoryList:
    """``q`` ranks by meaning when EMBEDDINGS_MODEL is set; otherwise it is ignored."""
    items = await runtime.store.asearch(_namespace(principal), query=q, limit=min(limit, 100))
    return MemoryList(
        memories=[Memory(key=i.key, value=i.value, created_at=str(i.created_at)) for i in items]
    )


@router.post("", status_code=201)
async def create(payload: RememberFact, runtime: Runtime, principal: Writer) -> MemoryCreated:
    key = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%f")
    await runtime.store.aput(_namespace(principal), key, {"fact": payload.fact, "created_at": key})
    return MemoryCreated(key=key, fact=payload.fact)


@router.delete("/{key}", status_code=204)
async def destroy(key: str, runtime: Runtime, principal: Writer) -> Response:
    await runtime.store.adelete(_namespace(principal), key)
    return Response(status_code=204)
