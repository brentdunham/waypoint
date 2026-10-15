"""Real Redis leases and Postgres checkpoints, with deterministic graph steps."""

import asyncio
import os
import sys
import time
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, build_settings, requires_infra

from waypoint.agents.runtime import AgentRuntime, start_runtime, stop_runtime
from waypoint.agents.types import Graph
from waypoint.errors import Conflict
from waypoint.infra.bus import RedisBus
from waypoint.infra.checkpoints import checkpoint_fence
from waypoint.infra.queue import OwnershipLostError, RedisRunQueue, RunJob
from waypoint.infra.runs import RunWorker
from waypoint.infra.security import Principal
from waypoint.settings import Settings
from waypoint.usage import METER, report_embedding

pytestmark = [pytest.mark.integration, requires_infra]


class State(TypedDict, total=False):
    value: str


async def lose(worker: RunWorker) -> None:
    """Stop without ACK, handoff, terminal publication or owner-key cleanup."""
    for task in worker._background:
        task.cancel()
    await asyncio.gather(*worker._background, return_exceptions=True)
    for run_id, task in list(worker._tasks.items()):
        worker._lost.add(run_id)
        task.cancel()
    await asyncio.gather(*worker._tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("durability", ["sync", "async", "exit"])
@pytest.mark.parametrize("ending", ["complete", "cancel", "attempts", "deadline", "handoff"])
def test_a_successor_finishes_an_abandoned_run(durability: str, ending: str) -> None:  # noqa: PLR0915 -- one recovery lifecycle, including cleanup

    async def scenario() -> None:  # noqa: PLR0915 -- keep the crash and successor on one loop
        settings = build_settings(
            database_url=TEST_DATABASE_URL,
            redis_url=TEST_REDIS_URL,
            queue={
                "lease_s": 0.6,
                "heartbeat_s": 0.1,
                "reclaim_s": 0.05,
                "drain_s": 0,
                "max_attempts": 1 if ending == "attempts" else 3,
            },
            limits={"run_timeout_s": 0.3 if ending == "deadline" else 10},
            stream={"block_ms": 100},
        )
        runtime = await start_runtime(settings)
        entered = asyncio.Event()
        calls: list[str] = []

        async def charge() -> None:
            report_embedding("voyageai:voyage-4", 100)
            meter = METER.get()
            assert meter is not None
            await meter.flush()

        async def first(_state: State) -> State:
            calls.append("first")
            await charge()
            return {"value": "checkpoint"}

        async def second(_state: State) -> State:
            calls.append("second")
            await charge()
            if calls.count("second") == 1:
                entered.set()
                await asyncio.Event().wait()
            return {"value": "finished"}

        builder = StateGraph(State)
        builder.add_node("first", first)
        builder.add_node("second", second)
        builder.add_edge(START, "first")
        builder.add_edge("first", "second")
        builder.add_edge("second", END)
        graph = builder.compile(checkpointer=runtime.checkpointer)
        runtime.graphs["chat"] = graph

        def worker() -> RunWorker:
            return RunWorker(
                queue=runtime.queue,
                bus=runtime.bus,
                db=runtime.db,
                limiter=runtime.limiter,
                settings=settings,
                graphs=runtime.graphs,
            )

        a, b = worker(), worker()
        try:
            caller = Principal("recovery", "alice", "test", frozenset({"*"}))
            thread = await runtime.threads.create(caller, agent="chat")
            record = await runtime.runs.start(
                agent="chat",
                thread_id=thread["thread_id"],
                principal=caller,
                request_id="test",
                payload={"value": "input"},
                durability=durability,
            )
            await a.start()
            await asyncio.wait_for(entered.wait(), 3)
            # A queued or active thread is still excluded across API managers.
            with pytest.raises(Conflict):
                await runtime.runs.start(
                    agent="chat",
                    thread_id=thread["thread_id"],
                    principal=caller,
                    request_id="competing",
                    payload={},
                )
            if ending == "handoff":
                await a.aclose()
            else:
                await lose(a)
            if ending == "cancel":
                await runtime.runs.cancel(record.run_id, caller)
            await b.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                final = await runtime.runs.require(record.run_id, caller)
                stats = await runtime.queue.stats()
                if final.status != "running" and stats == {"queued": 0, "running": 0}:
                    break
                await asyncio.sleep(0.02)
            assert final.status == {
                "cancel": "cancelled",
                "attempts": "failed",
                "deadline": "failed",
            }.get(ending, "completed")
            assert stats == {"queued": 0, "running": 0}
            events = await runtime.bus.read(record.run_id, last_id="0", block_ms=1)
            terminals = [
                event
                for event in events
                if event.type in {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
            ]
            assert len(terminals) == 1
            assert final.usage is not None
            if ending in {"complete", "handoff"}:
                reclaimed = [e for e in events if e.type == "run.reclaimed"]
                assert len(reclaimed) == 1
                assert reclaimed[0].data == {"attempt": 2, "from_checkpoint": durability != "exit"}
                assert calls == (
                    ["first", "second", "first", "second"]
                    if durability == "exit"
                    else ["first", "second", "second"]
                )
                assert final.usage["roles"]["embed"]["tokens"] == len(calls) * 100
                state = await graph.aget_state({"configurable": {"thread_id": thread["thread_id"]}})
                assert state.values["value"] == "finished"
            else:
                assert calls == ["first", "second"]
                assert final.usage["roles"]["embed"]["tokens"] == 200
            assert runtime.limiter is not None
            assert await runtime.limiter.in_flight("recovery") == 0
        finally:
            await a.aclose()
            await b.aclose()
            await stop_runtime(runtime)

    asyncio.run(scenario())


def test_expired_owner_cannot_publish_finalize_renew_or_release_its_successor() -> None:
    async def scenario() -> None:
        from redis.asyncio import Redis

        redis = Redis.from_url(str(TEST_REDIS_URL))
        queue = RedisRunQueue(redis)
        bus = RedisBus(redis, maxlen=100, ttl_s=60)
        try:
            await queue.ensure()
            job = RunJob("fence", "thread", "chat", "tenant", "user", "now", "request")
            await queue.enqueue(job)
            raw = await queue.read("old", block_ms=1)
            assert raw is not None
            old = await queue.own(raw, "old", lease_s=0.05)
            await asyncio.sleep(0.08)
            raw = await queue.reclaim("new", lease_s=0.05)
            assert raw is not None
            new = await queue.own(raw, "new", lease_s=1)
            with pytest.raises(OwnershipLostError):
                await bus.append("fence", "token", {"text": "stale"}, owner=old.owner)
            with pytest.raises(OwnershipLostError):
                await bus.finish(
                    "fence", "run.completed", {}, {"thread_id": "thread"}, owner=old.owner
                )
            assert not await queue.renew(old, "old", lease_s=1)
            await queue.complete(old)
            await queue.handoff(old, "old", lease_s=1)
            assert await queue.stats() == {"queued": 0, "running": 1}
            assert await queue.renew(new, "new", lease_s=1)
            with pytest.raises(Conflict):
                await queue.enqueue(
                    RunJob("other", "thread", "chat", "tenant", "user", "now", "request")
                )
            await queue.complete(new)
            assert await queue.stats() == {"queued": 0, "running": 0}
        finally:
            await redis.aclose()

    asyncio.run(scenario())


async def crash_graph(runtime: AgentRuntime) -> Graph:
    async def spend() -> None:
        report_embedding("voyageai:voyage-4", 100)
        meter = METER.get()
        assert meter is not None
        await meter.flush()

    async def first(_state: State) -> State:
        await runtime.redis.incr("crash:first")
        await spend()
        return {"value": "checkpoint"}

    async def second(_state: State) -> State:
        visit = await runtime.redis.incr("crash:second")
        await spend()
        if visit == 1:
            await runtime.redis.set("crash:ready", "1")
            await asyncio.Event().wait()
        return {"value": "finished"}

    builder = StateGraph(State)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    return builder.compile(checkpointer=runtime.checkpointer)


def crash_settings() -> Settings:
    return build_settings(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        queue={"lease_s": 0.6, "heartbeat_s": 0.1, "reclaim_s": 0.05, "drain_s": 0},
        stream={"block_ms": 100},
        limits={"run_timeout_s": 20},
    )


async def crash_process() -> None:
    """Executed only in the disposable child; SIGKILL must skip every finally."""
    settings = crash_settings()
    runtime = await start_runtime(settings)
    runtime.graphs["chat"] = await crash_graph(runtime)
    worker = RunWorker(
        queue=runtime.queue,
        bus=runtime.bus,
        db=runtime.db,
        limiter=runtime.limiter,
        settings=settings,
        graphs=runtime.graphs,
    )
    await worker.start()
    await asyncio.Event().wait()


@pytest.mark.parametrize("durability", ["sync", "async", "exit"])
def test_sigkill_recovers_without_running_the_predecessors_cleanup(durability: str) -> None:
    async def scenario() -> None:
        settings = crash_settings()
        runtime = await start_runtime(settings)
        runtime.graphs["chat"] = await crash_graph(runtime)
        successor = RunWorker(
            queue=runtime.queue,
            bus=runtime.bus,
            db=runtime.db,
            limiter=runtime.limiter,
            settings=settings,
            graphs=runtime.graphs,
        )
        process = None
        try:
            caller = Principal("crash", "alice", "test", frozenset({"*"}))
            thread = await runtime.threads.create(caller, agent="chat")
            run = await runtime.runs.start(
                agent="chat",
                thread_id=thread["thread_id"],
                principal=caller,
                request_id="crash",
                payload={"value": "input"},
                durability=durability,
            )
            process = await asyncio.create_subprocess_exec(
                *[
                    sys.executable,
                    "-c",
                    "import asyncio; from tests.test_queue_recovery import crash_process; asyncio.run(crash_process())",
                ],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(8):
                while not await runtime.redis.exists("crash:ready"):
                    if process.returncode is not None:
                        pytest.fail((await process.communicate())[1].decode())
                    await asyncio.sleep(0.02)
            process.kill()
            await asyncio.wait_for(process.wait(), 3)
            assert (await runtime.runs.require(run.run_id, caller)).status == "running"
            saved = await runtime.graphs["chat"].aget_state(
                {"configurable": {"thread_id": thread["thread_id"]}}
            )
            # async may lose the first step's checkpoint even though the next
            # node had started. Assert recovery from what actually landed.
            expected_first = (
                2 if durability == "exit" or saved.values.get("value") != "checkpoint" else 1
            )
            if durability == "sync":
                assert expected_first == 1
            await successor.start()
            async with asyncio.timeout(5):
                async for event in runtime.runs.events(run.run_id):
                    if event.type in {"run.completed", "run.failed", "run.cancelled"}:
                        break
            final = await runtime.runs.require(run.run_id, caller)
            assert final.status == "completed"
            assert int(await runtime.redis.get("crash:first")) == expected_first
            assert int(await runtime.redis.get("crash:second")) == 2
            assert final.usage["roles"]["embed"]["tokens"] == (expected_first + 2) * 100
            events = await runtime.bus.read(run.run_id, last_id="0", block_ms=1)
            assert sum(e.type == "run.reclaimed" for e in events) == 1
            assert sum(e.type == "run.completed" for e in events) == 1
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), 3)
                await process.communicate()
            await successor.aclose()
            await stop_runtime(runtime)

    asyncio.run(scenario())


def test_a_delayed_predecessor_cannot_overwrite_the_successors_checkpoint() -> None:
    async def scenario() -> None:
        runtime = await start_runtime(crash_settings())
        try:
            caller = Principal("fence", "alice", "test", frozenset({"*"}))
            thread = await runtime.threads.create(caller, agent="chat")
            builder = StateGraph(State)
            builder.add_node("step", lambda state: {"value": state["value"]})
            builder.add_edge(START, "step")
            builder.add_edge("step", END)
            graph = builder.compile(checkpointer=runtime.checkpointer)
            queue = runtime.queue
            job = RunJob(
                "checkpoint-fence", thread["thread_id"], "chat", "fence", "alice", "now", "req"
            )
            await queue.enqueue(job)
            raw = await queue.read("old", block_ms=1)
            assert raw is not None
            old = await queue.own(raw, "old", lease_s=1)
            config = {"configurable": {"thread_id": thread["thread_id"]}}
            async with checkpoint_fence(runtime.db, queue, old, "old", 1):
                await graph.ainvoke({"value": "old"}, config)
                await queue.handoff(old, "old", lease_s=1)
                raw = await queue.reclaim("new", lease_s=1)
                assert raw is not None
                new = await queue.own(raw, "new", lease_s=1)
                async with checkpoint_fence(runtime.db, queue, new, "new", 1):
                    await graph.ainvoke({"value": "successor"}, config)
                with pytest.raises(OwnershipLostError):
                    await graph.aupdate_state(config, {"value": "stale"})
                head = await graph.aget_state(config)
                with pytest.raises(OwnershipLostError):
                    await runtime.checkpointer.aput_writes(
                        head.config, [("value", "stale")], "task"
                    )
            assert (await graph.aget_state(config)).values["value"] == "successor"
            await queue.complete(new)
        finally:
            await stop_runtime(runtime)

    asyncio.run(scenario())


def test_terminal_publication_before_a_crash_does_not_execute_the_graph_again() -> None:
    async def scenario() -> None:
        runtime = await start_runtime(crash_settings())
        successor = RunWorker(
            queue=runtime.queue,
            bus=runtime.bus,
            db=runtime.db,
            limiter=runtime.limiter,
            settings=runtime.settings,
            graphs=runtime.graphs,
        )
        try:
            caller = Principal("published", "alice", "test", frozenset({"*"}))
            thread = await runtime.threads.create(caller, agent="chat")
            run = await runtime.runs.start(
                agent="chat",
                thread_id=thread["thread_id"],
                principal=caller,
                request_id="test",
                payload={},
            )
            claimed = await runtime.queue.read("lost", block_ms=1)
            assert claimed is not None
            claimed = await runtime.queue.own(claimed, "lost", lease_s=0.05)
            run.status = "completed"
            await runtime.bus.finish(
                run.run_id, "run.completed", {"result": {}}, run.public(), owner=claimed.owner
            )
            await asyncio.sleep(0.08)
            await successor.start()
            async with asyncio.timeout(3):
                while await runtime.queue.stats() != {"queued": 0, "running": 0}:  # noqa: ASYNC110 -- observe Redis cleanup by another task
                    await asyncio.sleep(0.02)
            events = await runtime.bus.read(run.run_id, last_id="0", block_ms=1)
            assert [e.type for e in events] == ["run.started", "run.completed"]
        finally:
            await successor.aclose()
            await stop_runtime(runtime)

    asyncio.run(scenario())
