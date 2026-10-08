"""Tenant isolation at the graph layer, and slot accounting on a failed start.

These compile graphs directly with an in-memory node cache, because the app's
unit tier runs without one (the cache is Redis-backed) and would never exercise
the cache key at all.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from flask.testing import FlaskClient
from langgraph.cache.memory import InMemoryCache
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from tests.conftest import build_settings, new_thread, run_and_wait

from waypoint.agents.context import AgentContext
from waypoint.agents.graphs import chat, research
from waypoint.infra.bus import MemoryBus
from waypoint.infra.runs import RunManager
from waypoint.infra.security import Principal


def _context(tenant: str, user: str, agent: str) -> AgentContext:
    return AgentContext(tenant_id=tenant, user_id=user, request_id="r", run_id="x", agent=agent)


def test_recall_does_not_serve_one_tenants_memories_to_another() -> None:
    """Identical input state (fixed message id) must not hit a shared cache entry."""

    async def scenario() -> tuple[list[str], list[str]]:
        store = InMemoryStore()
        await store.aput(("memories", "acme", "alice"), "k", {"fact": "alice's secret"})
        graph = chat.build(
            build_settings(node_cache_ttl_s=60),
            checkpointer=InMemorySaver(),
            store=store,
            cache=InMemoryCache(),
        )
        turn = {"messages": [{"role": "user", "content": "what do you know?", "id": "m1"}]}
        a = await graph.ainvoke(
            turn, {"configurable": {"thread_id": "t1"}}, context=_context("acme", "alice", "chat")
        )
        b = await graph.ainvoke(
            turn, {"configurable": {"thread_id": "t2"}}, context=_context("evil", "eve", "chat")
        )
        return a["recalled"], b["recalled"]

    alice, eve = asyncio.run(scenario())
    assert alice == ["alice's secret"]
    assert eve == []


def test_research_cache_is_shared_within_a_tenant_but_not_across() -> None:
    async def cache_hits(tenants: list[str]) -> list[int]:
        graph = research.build(
            build_settings(node_cache_ttl_s=60),
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
            cache=InMemoryCache(),
        )
        hits: list[int] = []
        for i, tenant in enumerate(tenants):
            config: Any = {"configurable": {"thread_id": f"t{i}"}}
            ctx = _context(tenant, "u", "research")
            await graph.ainvoke({"topic": "heat pumps"}, config, context=ctx)
            count = 0
            async for chunk in graph.astream(
                Command(resume={"approved": True}), config, context=ctx, stream_mode="updates"
            ):
                if "investigate" in chunk and (chunk.get("__metadata__") or {}).get("cached"):
                    count += 1
            hits.append(count)
        return hits

    first, same_tenant, other_tenant = asyncio.run(cache_hits(["acme", "acme", "evil"]))
    assert first == 0
    assert same_tenant > 0  # the cache still does its job
    assert other_tenant == 0  # ...but never across a tenant boundary


def test_remember_tool_writes_where_the_memories_api_reads(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    run_and_wait(client, thread_id, {"message": '[[tool:remember({"fact": "drinks tea"})]]'})
    memories = client.get("/api/v1/memories").get_json()["memories"]
    assert [m["value"]["fact"] for m in memories] == ["drinks tea"]


class _CountingLimiter:
    def __init__(self) -> None:
        self.held = 0

    @contextlib.asynccontextmanager
    async def slot(self, tenant_id: str) -> AsyncIterator[str]:
        self.held += 1
        try:
            yield "member"
        finally:
            self.held -= 1


class _BrokenBus(MemoryBus):
    async def append(self, run_id: str, type_: str, data: dict[str, Any]) -> Any:
        raise ConnectionError("redis went away")


def test_failed_start_gives_the_concurrency_slot_back() -> None:
    limiter = _CountingLimiter()

    async def scenario() -> None:
        manager = RunManager(
            bus=_BrokenBus(maxlen=100),
            db=None,
            limiter=limiter,  # type: ignore[arg-type]
            settings=build_settings(),
            graphs={"chat": object()},  # type: ignore[dict-item]
        )
        principal = Principal(tenant_id="acme", user_id="u", key_id="k", scopes=frozenset("*"))
        with pytest.raises(ConnectionError):
            await manager.start(
                agent="chat", thread_id="t", principal=principal, request_id="r", payload={}
            )
        # Checked inside the loop: asyncio.run's shutdown finalizes abandoned
        # async generators, which would release a leaked slot and hide the bug.
        assert limiter.held == 0

    asyncio.run(scenario())
