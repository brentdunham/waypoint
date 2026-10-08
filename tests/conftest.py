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
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from waypoint import create_app
from waypoint.agents.runtime import AgentRuntime
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


class TestApp:
    """A booted app: its lifespan has run, so the runtime exists.

    One ``TestClient`` per app, entered once, because entering it is what runs
    the lifespan; a second client would build a second runtime. Its event loop
    lives in a portal thread for the life of the app, and so do the runs.
    """

    __test__ = False  # not a test class, whatever its name says

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.__enter__()
        self._open = True

    def test_client(self) -> TestClient:
        return self.client

    @property
    def runtime(self) -> AgentRuntime:
        runtime: AgentRuntime = self.app.state.runtime
        return runtime

    def call[T](self, fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run ``await fn(*args)`` on the app's event loop."""
        portal = self.client.portal
        assert portal is not None
        return portal.call(fn, *args)

    def close(self) -> None:
        """Run the lifespan's shutdown. Safe to call twice."""
        if self._open:
            self._open = False
            self.client.__exit__(None, None, None)


@pytest.fixture
def make_app() -> Iterator[Any]:
    """Factory so a test can ask for a differently configured app."""
    created: list[TestApp] = []

    def factory(**overrides: Any) -> TestApp:
        application = TestApp(create_app(build_settings(**overrides)))
        created.append(application)
        return application

    yield factory

    for application in created:
        application.close()


@pytest.fixture
def app(make_app: Any) -> TestApp:
    return make_app()  # type: ignore[no-any-return]


@pytest.fixture
def client(app: TestApp) -> TestClient:
    return app.test_client()


@pytest.fixture
def infra_app(make_app: Any) -> TestApp:
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
def infra_client(infra_app: TestApp) -> TestClient:
    return infra_app.test_client()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}


@pytest.fixture
def thread(client: TestClient) -> str:
    response = client.post("/api/v1/threads", json={"agent": "chat", "title": "t"})
    assert response.status_code == 201
    return str(response.json()["thread_id"])


def new_thread(client: TestClient, agent: str, headers: dict[str, str] | None = None) -> str:
    response = client.post("/api/v1/threads", json={"agent": agent}, headers=headers or {})
    assert response.status_code == 201, response.json()
    return str(response.json()["thread_id"])


def run_and_wait(
    client: TestClient, thread_id: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/threads/{thread_id}/runs?wait=true", json=body, headers=headers or {}
    )
    assert response.status_code == 200, response.json()
    return dict(response.json())


def read_sse(client: TestClient, url: str, headers: dict[str, str] | None = None) -> list[str]:
    """Consume an SSE response and return the ordered list of event names."""
    names: list[str] = []
    with client.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event: "):
                names.append(line.removeprefix("event: "))
            if names and names[-1] in TERMINAL_EVENTS:
                break
    return names


TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted"})


def a_uuid() -> str:
    return str(uuid.uuid4())
