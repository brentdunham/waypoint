"""The run queue: the API queues runs, workers drive them (docs/design/queue.md)."""

import asyncio
import time
from typing import Any

import pytest
from starlette.testclient import TestClient
from tests.conftest import (
    BOOTSTRAP_KEY,
    TEST_DATABASE_URL,
    TEST_REDIS_URL,
    TestApp,
    new_thread,
    read_sse,
    requires_infra,
)

from waypoint.agents.models import ScriptedChatModel
from waypoint.infra.queue import MemoryRunQueue, RunJob

AUTH = {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}


def infra(**overrides: Any) -> dict[str, Any]:
    return {
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "auth_enabled": True,
        "bootstrap_api_key": BOOTSTRAP_KEY,
        "limits": {"rpm": 1000, "concurrent_runs": 50},
        **overrides,
    }


def wait_for(client: TestClient, run_id: str, *, seconds: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while True:
        record = client.get(f"/api/v1/runs/{run_id}", headers=AUTH).json()
        if record["status"] != "running" or time.monotonic() > deadline:
            return dict(record)
        time.sleep(0.05)


# -- unit ----------------------------------------------------------------------------------


def test_a_job_comes_off_the_queue_as_it_went_on() -> None:
    job = RunJob(
        run_id="r",
        thread_id="t",
        agent="chat",
        tenant_id="acme",
        user_id="u",
        created_at="2026-10-15T00:00:00+00:00",
        request_id="req",
        scopes=["agents:run"],
        payload={"message": "hi"},
        resume=False,  # a falsy resume value is still a resume
        durability="sync",
        models={"assistant": "m"},
        trace_id="0" * 31 + "1",
        link={"trace_id": "a" * 32, "span_id": "b" * 16},
        slot="s1",
    )
    assert RunJob.loads(job.dumps()) == job
    # A job written by a newer API, with a field this worker does not know.
    assert RunJob.loads(job.dumps()[:-1] + ',"later":1}') == job


def test_the_memory_queue_hands_each_run_to_one_reader() -> None:
    async def scenario() -> list[str | None]:
        queue = MemoryRunQueue()
        job = RunJob("r", "t", "chat", "acme", "u", "now", "req")
        await queue.enqueue(job)
        first = await queue.read("a", block_ms=100)
        second = await queue.read("b", block_ms=100)
        assert first is not None
        assert await queue.stats() == {"queued": 0, "running": 1}
        await queue.ack(first.entry_id)
        assert await queue.stats() == {"queued": 0, "running": 0}
        return [first.job.run_id, second.job.run_id if second else None]

    assert asyncio.run(scenario()) == ["r", None]


def test_without_redis_the_api_drives_its_own_runs(app: TestApp) -> None:
    assert app.runtime.role == "api"
    assert app.runtime.worker is not None
    assert app.workers == []


# -- integration ---------------------------------------------------------------------------


@pytest.mark.integration
@requires_infra
def test_with_redis_the_api_only_queues_and_a_worker_drives(make_app: Any) -> None:
    app = make_app(workers=0, **infra())
    client = app.test_client()
    assert app.runtime.worker is None  # nothing in the API process drives runs

    thread_id = new_thread(client, "chat", AUTH)
    run = client.post(
        f"/api/v1/threads/{thread_id}/runs", json={"message": "queued"}, headers=AUTH
    ).json()
    time.sleep(0.3)
    assert client.get(f"/api/v1/runs/{run['run_id']}", headers=AUTH).json()["status"] == "running"
    checks = client.get("/api/v1/readyz").json()
    assert checks["status"] == "ready"  # no worker is a reason runs wait, not an outage
    assert checks["checks"]["workers"] == "unavailable"
    assert checks["checks"]["queue"] == "1 queued, 0 running"

    worker = app.start_worker()
    assert wait_for(client, run["run_id"])["status"] == "completed"
    assert worker.worker is not None
    assert worker.worker.claimed == 1
    checks = client.get("/api/v1/readyz").json()["checks"]
    assert checks["workers"] == "ok (1)"
    assert checks["queue"] == "0 queued, 0 running"  # acked and deleted


@pytest.mark.integration
@requires_infra
def test_two_workers_share_the_queue(make_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each reply takes a moment (the scripted model runs in a thread), and each
    # worker drives one run at a time, so the second worker gets work too.
    original = ScriptedChatModel._next

    def slow(self: ScriptedChatModel, messages: Any) -> Any:
        time.sleep(0.3)
        return original(self, messages)

    monkeypatch.setattr(ScriptedChatModel, "_next", slow)
    app = make_app(workers=2, **infra(queue={"concurrency": 1, "drain_s": 0.5}))
    client = app.test_client()

    runs = [
        client.post(
            f"/api/v1/threads/{new_thread(client, 'chat', AUTH)}/runs",
            json={"message": f"run {i}"},
            headers=AUTH,
        ).json()
        for i in range(4)
    ]
    assert [wait_for(client, r["run_id"])["status"] for r in runs] == ["completed"] * 4
    claimed = [w.worker.claimed for w in app.workers if w.worker is not None]
    assert sum(claimed) == 4
    assert all(n > 0 for n in claimed), claimed


@pytest.mark.integration
@requires_infra
def test_a_run_cancelled_while_queued_never_starts(make_app: Any) -> None:
    app = make_app(workers=0, **infra())
    client = app.test_client()
    thread_id = new_thread(client, "chat", AUTH)
    run = client.post(
        f"/api/v1/threads/{thread_id}/runs", json={"message": "never"}, headers=AUTH
    ).json()
    assert client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=AUTH).status_code == 202

    app.start_worker()  # takes it off the queue, sees the flag, ends it
    assert wait_for(client, run["run_id"])["status"] == "cancelled"
    assert read_sse(client, run["events_url"], AUTH) == ["run.started", "run.cancelled"]
    state = client.get(f"/api/v1/threads/{thread_id}/state", headers=AUTH).json()
    assert not state["values"].get("messages")  # the graph never ran
    limiter = app.runtime.limiter
    assert limiter is not None
    assert app.call(limiter.in_flight, "bootstrap") == 0  # its slot came back
