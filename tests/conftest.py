"""Test fixtures.

Two tiers, on purpose:

* **unit** -- no Postgres, no Redis, no network. The app boots on the in-memory
  checkpointer, store and event bus, and the LLM is ``ScriptedChatModel``.
  These run anywhere, in about a second, and cover the graphs and the API.
* **integration** -- marked ``@pytest.mark.integration``, skipped unless
  ``TEST_DATABASE_URL`` and ``TEST_REDIS_URL`` are set. These cover the things
  that only exist when real infrastructure does: durable checkpoints, resumable
  streams, distributed rate limits, key storage.
"""

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from waypoint import create_app
from waypoint.aio import loop
from waypoint.settings import Settings

BOOTSTRAP_KEY = "ak_test000000.test-key-do-not-use-in-production"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")

requires_infra = pytest.mark.skipif(
    not (TEST_DATABASE_URL and TEST_REDIS_URL),
    reason="set TEST_DATABASE_URL and TEST_REDIS_URL to run integration tests",
)


def build_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "debug": False,
        "fake_model": True,
        "auth_enabled": False,
        "json_logs": False,
        "log_level": "WARNING",
        "database_url": None,
        "redis_url": None,
        "node_cache_ttl_s": 0,
        "secret_key": "test",
        "embeddings_model": None,
        "openai_api_key": None,
    }
    base.update(overrides)
    # _env_file=None: the developer's .env (real keys, EMBEDDINGS_MODEL, a live
    # DATABASE_URL) must never leak into a test app's configuration.
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def clean_redis() -> None:
    """Each integration test starts with an empty Redis database.

    Rate-limit buckets and run streams are keyed by tenant and run id, so
    without this a burst test poisons the next test's budget. TEST_REDIS_URL
    must point at a scratch database (``/15``, not ``/0``) -- this flushes it.
    """
    if not TEST_REDIS_URL:
        return
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL)
    try:
        client.flushdb()
    finally:
        client.close()


@pytest.fixture
def make_app() -> Iterator[Any]:
    """Factory so a test can ask for a differently configured app."""
    created: list[Flask] = []

    def factory(**overrides: Any) -> Flask:
        application = create_app(build_settings(**overrides))
        created.append(application)
        return application

    yield factory

    for application in created:
        from waypoint.agents.runtime import shutdown_runtime

        shutdown_runtime(application)
    loop.stop()


@pytest.fixture
def app(make_app: Any) -> Flask:
    return make_app()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def infra_app(make_app: Any) -> Flask:
    """An app wired to real Postgres and Redis, isolated per test run."""
    return make_app(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        node_cache_ttl_s=60,
        # Limits get their own tests; here they must not interfere.
        limits={"rpm": 1000, "concurrent_runs": 50},
    )


@pytest.fixture
def infra_client(infra_app: Flask) -> FlaskClient:
    return infra_app.test_client()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}


@pytest.fixture
def thread(client: FlaskClient) -> str:
    response = client.post("/api/v1/threads", json={"agent": "chat", "title": "t"})
    assert response.status_code == 201
    return str(response.get_json()["thread_id"])


def new_thread(client: FlaskClient, agent: str, headers: dict[str, str] | None = None) -> str:
    response = client.post("/api/v1/threads", json={"agent": agent}, headers=headers or {})
    assert response.status_code == 201, response.get_json()
    return str(response.get_json()["thread_id"])


def run_and_wait(
    client: FlaskClient, thread_id: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/threads/{thread_id}/runs?wait=true", json=body, headers=headers or {}
    )
    assert response.status_code == 200, response.get_json()
    return dict(response.get_json())


def read_sse(client: FlaskClient, url: str, headers: dict[str, str] | None = None) -> list[str]:
    """Consume an SSE response and return the ordered list of event names."""
    response = client.get(url, headers=headers or {}, buffered=False)
    assert response.status_code == 200
    names: list[str] = []
    for chunk in response.response:
        for line in chunk.decode().splitlines():
            if line.startswith("event: "):
                names.append(line.removeprefix("event: "))
        if names and names[-1] in {
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.interrupted",
        }:
            break
    response.close()
    return names


def a_uuid() -> str:
    return str(uuid.uuid4())
