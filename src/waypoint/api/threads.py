"""Threads: ownership, state inspection, and time travel."""

from typing import Any

from flask import Blueprint, jsonify, request
from langchain_core.runnables import RunnableConfig

from waypoint.agents import get_runtime, get_spec
from waypoint.aio import loop
from waypoint.errors import BadRequest
from waypoint.infra.security import current_principal, requires
from waypoint.schemas import SCOPE_READ, SCOPE_WRITE, CreateThread, UpdateState
from waypoint.serialization import snapshot_to_dict

bp = Blueprint("threads", __name__, url_prefix="/threads")


def _body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise BadRequest("Expected a JSON object body.")
    return payload


def _config(thread_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread_id, "checkpoint_ns": ""}
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return RunnableConfig(configurable=configurable)


@bp.post("")
@requires(SCOPE_WRITE)
def create() -> Any:
    payload = CreateThread.model_validate(_body())
    get_spec(payload.agent)  # 404 early rather than at first run
    runtime = get_runtime()
    thread = loop.run_sync(
        runtime.threads.create(current_principal(), agent=payload.agent, title=payload.title),
        timeout=10,
    )
    return jsonify(thread), 201


@bp.get("")
@requires(SCOPE_READ)
def index() -> Any:
    limit = min(int(request.args.get("limit", 50)), 200)
    runtime = get_runtime()
    threads = loop.run_sync(runtime.threads.list(current_principal(), limit=limit), timeout=10)
    return jsonify(threads=threads)


@bp.get("/<thread_id>")
@requires(SCOPE_READ)
def show(thread_id: str) -> Any:
    runtime = get_runtime()
    return jsonify(loop.run_sync(runtime.threads.require(thread_id, current_principal())))


@bp.delete("/<thread_id>")
@requires(SCOPE_WRITE)
def destroy(thread_id: str) -> Any:
    runtime = get_runtime()
    loop.run_sync(runtime.threads.delete(thread_id, current_principal()), timeout=10)
    return "", 204


@bp.get("/<thread_id>/state")
@requires(SCOPE_READ)
def state(thread_id: str) -> Any:
    """Current head state, including any pending interrupt."""
    runtime = get_runtime()
    thread = loop.run_sync(runtime.threads.require(thread_id, current_principal()))
    graph = runtime.graph(thread["agent"])
    checkpoint_id = request.args.get("checkpoint_id")
    snapshot = loop.run_sync(graph.aget_state(_config(thread_id, checkpoint_id)), timeout=15)
    return jsonify(snapshot_to_dict(snapshot))


@bp.get("/<thread_id>/history")
@requires(SCOPE_READ)
def history(thread_id: str) -> Any:
    """Every checkpoint, newest first. This is the time-travel index."""
    limit = min(int(request.args.get("limit", 20)), 100)
    runtime = get_runtime()
    thread = loop.run_sync(runtime.threads.require(thread_id, current_principal()))
    graph = runtime.graph(thread["agent"])

    async def collect() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async for snapshot in graph.aget_state_history(_config(thread_id), limit=limit):
            out.append(snapshot_to_dict(snapshot))
        return out

    return jsonify(checkpoints=loop.run_sync(collect(), timeout=30))


@bp.post("/<thread_id>/state")
@requires(SCOPE_WRITE)
def update_state(thread_id: str) -> Any:
    """Rewrite state as if ``as_node`` had produced it.

    Combined with ``checkpoint_id`` this forks history: the thread gains a new
    branch from that point, and the next run continues down the branch.
    """
    payload = UpdateState.model_validate(_body())
    runtime = get_runtime()
    thread = loop.run_sync(runtime.threads.require(thread_id, current_principal()))
    graph = runtime.graph(thread["agent"])
    new_config = loop.run_sync(
        graph.aupdate_state(
            _config(thread_id, payload.checkpoint_id),
            payload.values,
            as_node=payload.as_node,
        ),
        timeout=15,
    )
    return jsonify(
        checkpoint_id=new_config.get("configurable", {}).get("checkpoint_id"),
        thread_id=thread_id,
    ), 201
