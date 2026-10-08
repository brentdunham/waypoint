"""A quiet run must not kill its event stream, and a lost stream is not a failed run.

Regression for: research runs went silent for longer than the 5s XREAD block
while workers searched the web. redis-py's default socket timeout is also 5s,
so the blocking read timed out client-side, the SSE handler emitted a fake
``run.failed``, and the UI reported a healthy run as failed.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from flask import Flask
from pydantic import ValidationError
from tests.conftest import TEST_REDIS_URL, build_settings, new_thread, requires_infra

from waypoint.agents.runtime import EXTENSION_KEY
from waypoint.infra.bus import Event, RedisBus
from waypoint.infra.redis import create_client


def test_block_must_fit_inside_the_socket_timeout() -> None:
    with pytest.raises(ValidationError, match="STREAM__BLOCK_MS=5000"):
        build_settings(redis_socket_timeout_s=5, stream={"block_ms": 5000})
    build_settings(redis_socket_timeout_s=6, stream={"block_ms": 5000})  # 1s margin is fine


def test_defaults_leave_headroom() -> None:
    settings = build_settings()
    assert settings.stream.block_ms / 1000 <= settings.redis_socket_timeout_s - 1


@pytest.mark.integration
@requires_infra
def test_idle_blocking_read_returns_empty_instead_of_timing_out() -> None:
    async def read(socket_timeout_s: float) -> list[Event]:
        client = create_client(str(TEST_REDIS_URL), socket_timeout_s=socket_timeout_s)
        try:
            bus = RedisBus(client, maxlen=10, ttl_s=60)
            return await bus.read("idle-run", last_id="0", block_ms=1500)
        finally:
            await client.aclose()

    assert asyncio.run(read(socket_timeout_s=3)) == []

    # The mechanism of the original bug: a socket timeout shorter than the
    # block makes an idle read raise instead of returning nothing.
    from redis.exceptions import TimeoutError as RedisTimeout

    with pytest.raises(RedisTimeout):
        asyncio.run(read(socket_timeout_s=1))


def test_lost_stream_ends_without_inventing_a_terminal_event(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = app.test_client()
    thread_id = new_thread(client, "chat")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "hi"}).get_json()

    async def broken(run_id: str, **_: Any) -> AsyncIterator[Event]:
        yield Event(id="1-0", type="run.started", data={"run_id": run_id})
        raise ConnectionError("redis went away")

    runtime = app.extensions[EXTENSION_KEY]
    monkeypatch.setattr(runtime.runs, "events", broken)
    body = client.get(run["events_url"]).get_data(as_text=True)

    assert "event: run.started" in body
    assert "run.failed" not in body  # the run did not fail; following it did
    assert body.rstrip().endswith(": stream lost, reconnect with Last-Event-ID")
