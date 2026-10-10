"""Threads: ownership, state inspection, and time travel."""

from typing import Any

from fastapi import APIRouter, Response
from langchain_core.runnables import RunnableConfig

from waypoint.agents import get_spec
from waypoint.api.deps import Reader, Runtime, Writer
from waypoint.errors import BadRequest
from waypoint.schemas import (
    CreateThread,
    History,
    Snapshot,
    StateUpdated,
    Thread,
    ThreadList,
    UpdateState,
)
from waypoint.serialization import snapshot_to_dict

router = APIRouter(prefix="/threads", tags=["threads"])


def _config(thread_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return RunnableConfig(configurable=configurable)


@router.post("", status_code=201)
async def create(payload: CreateThread, runtime: Runtime, principal: Writer) -> Thread:
    spec = get_spec(payload.agent)  # 404 early rather than at first run
    if not spec.listed:
        raise BadRequest(f"Agent {spec.name!r} is not started directly; see the API docs.")
    thread = await runtime.threads.create(principal, agent=payload.agent, title=payload.title)
    return Thread.model_validate(thread)


@router.get("")
async def index(runtime: Runtime, principal: Reader, limit: int = 50) -> ThreadList:
    threads = await runtime.threads.list(principal, limit=min(limit, 200))
    return ThreadList(threads=[Thread.model_validate(t) for t in threads])


@router.get("/{thread_id}")
async def show(thread_id: str, runtime: Runtime, principal: Reader) -> Thread:
    return Thread.model_validate(await runtime.threads.require(thread_id, principal))


@router.delete("/{thread_id}", status_code=204)
async def destroy(thread_id: str, runtime: Runtime, principal: Writer) -> Response:
    await runtime.threads.delete(thread_id, principal)
    return Response(status_code=204)


@router.get("/{thread_id}/state")
async def state(
    thread_id: str, runtime: Runtime, principal: Reader, checkpoint_id: str | None = None
) -> Snapshot:
    """Current head state, including any pending interrupt. ``checkpoint_id``
    reads an earlier checkpoint instead."""
    thread = await runtime.threads.require(thread_id, principal)
    graph = runtime.graph(thread["agent"])
    snapshot = await graph.aget_state(_config(thread_id, checkpoint_id))
    return Snapshot.model_validate(snapshot_to_dict(snapshot))


@router.get("/{thread_id}/history")
async def history(thread_id: str, runtime: Runtime, principal: Reader, limit: int = 20) -> History:
    """Every checkpoint, newest first. This is the time-travel index."""
    thread = await runtime.threads.require(thread_id, principal)
    graph = runtime.graph(thread["agent"])
    checkpoints = [
        Snapshot.model_validate(snapshot_to_dict(snapshot))
        async for snapshot in graph.aget_state_history(_config(thread_id), limit=min(limit, 100))
    ]
    return History(checkpoints=checkpoints)


@router.post("/{thread_id}/state", status_code=201)
async def update_state(
    thread_id: str, payload: UpdateState, runtime: Runtime, principal: Writer
) -> StateUpdated:
    """Rewrite state as if ``as_node`` had produced it.

    Combined with ``checkpoint_id`` this forks history: the thread gains a new
    branch from that point, and the next run continues down the branch.
    """
    thread = await runtime.threads.require(thread_id, principal)
    graph = runtime.graph(thread["agent"])
    new_config = await graph.aupdate_state(
        _config(thread_id, payload.checkpoint_id), payload.values, as_node=payload.as_node
    )
    return StateUpdated(
        checkpoint_id=new_config.get("configurable", {}).get("checkpoint_id"),
        thread_id=thread_id,
    )
