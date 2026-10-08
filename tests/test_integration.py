"""Tests for behaviour that only exists when the real infrastructure does.

Run them with:

    TEST_DATABASE_URL=postgresql://agents:agents@localhost:5432/agents_test \
    TEST_REDIS_URL=redis://localhost:6379/15 \
    uv run pytest -m integration
"""

import time

import pytest
from starlette.testclient import TestClient
from tests.conftest import BOOTSTRAP_KEY, new_thread, read_sse, requires_infra, run_and_wait

pytestmark = [pytest.mark.integration, requires_infra]


# -- auth -------------------------------------------------------------------


def test_requests_without_a_key_are_rejected(infra_client: TestClient) -> None:
    response = infra_client.get("/api/v1/threads")
    assert response.status_code == 401
    assert response.json()["type"].endswith("unauthorized")


def test_a_bad_key_is_rejected(infra_client: TestClient) -> None:
    response = infra_client.get("/api/v1/threads", headers={"Authorization": "Bearer ak_nope.nope"})
    assert response.status_code == 401


def test_bootstrap_key_works(infra_client: TestClient, auth: dict[str, str]) -> None:
    assert infra_client.get("/api/v1/threads", headers=auth).status_code == 200


def test_minted_keys_are_scoped_to_their_tenant(
    infra_client: TestClient, auth: dict[str, str]
) -> None:
    mine = new_thread(infra_client, "chat", auth)

    minted = infra_client.post(
        "/api/v1/keys",
        json={
            "tenant_id": "acme",
            "user_id": "u1",
            "scopes": ["agents:run", "threads:read", "threads:write"],
        },
        headers=auth,
    )
    assert minted.status_code == 201
    other = {"Authorization": f"Bearer {minted.json()['api_key']}"}

    # Another tenant's thread must look like it does not exist at all.
    assert infra_client.get(f"/api/v1/threads/{mine}/state", headers=other).status_code == 404
    assert infra_client.get("/api/v1/threads", headers=other).json()["threads"] == []


def test_a_key_without_the_admin_scope_cannot_mint_keys(
    infra_client: TestClient, auth: dict[str, str]
) -> None:
    minted = infra_client.post(
        "/api/v1/keys",
        json={"tenant_id": "acme", "user_id": "u2", "scopes": ["agents:run"]},
        headers=auth,
    ).json()["api_key"]
    response = infra_client.post(
        "/api/v1/keys",
        json={"tenant_id": "evil", "user_id": "u3"},
        headers={"Authorization": f"Bearer {minted}"},
    )
    assert response.status_code == 403


def test_revoked_keys_stop_working(infra_client: TestClient, auth: dict[str, str]) -> None:
    raw = infra_client.post(
        "/api/v1/keys",
        json={"tenant_id": "acme", "user_id": "u4", "scopes": ["threads:read"]},
        headers=auth,
    ).json()["api_key"]
    key_id = raw.removeprefix("ak_").split(".", 1)[0]
    headers = {"Authorization": f"Bearer {raw}"}

    assert infra_client.get("/api/v1/threads", headers=headers).status_code == 200
    assert infra_client.delete(f"/api/v1/keys/{key_id}", headers=auth).status_code == 204
    # The positive cache is per-process and cleared on revoke.
    assert infra_client.get("/api/v1/threads", headers=headers).status_code == 401


# -- durability ---------------------------------------------------------------


def test_state_survives_a_process_restart(make_app, auth: dict[str, str]) -> None:
    """A thread paused on an interrupt is resumable by a *different* app object."""
    from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

    config = {
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "auth_enabled": True,
        "bootstrap_api_key": BOOTSTRAP_KEY,
    }
    first = make_app(**config)
    client = first.test_client()
    thread_id = new_thread(client, "research", auth)
    paused = run_and_wait(client, thread_id, {"message": "geothermal"}, auth)
    assert paused["status"] == "interrupted"

    first.close()  # the lifespan's shutdown: runs cancelled, pools closed

    second = make_app(**config)
    resumed = run_and_wait(second.test_client(), thread_id, {"resume": {"approved": True}}, auth)
    assert resumed["status"] == "completed"
    assert resumed["result"]["report"]


# -- streaming ----------------------------------------------------------------


def test_sse_replays_from_last_event_id(infra_client: TestClient, auth: dict[str, str]) -> None:
    thread_id = new_thread(infra_client, "chat", auth)
    run = infra_client.post(
        f"/api/v1/threads/{thread_id}/runs", json={"message": "resumable stream"}, headers=auth
    ).json()
    url = run["events_url"]

    everything = read_sse(infra_client, url, auth)
    assert everything[-1] == "run.completed"

    # Re-reading from the very beginning gives the same story back: the run's
    # output lives in Redis, not in the connection that produced it.
    again = read_sse(infra_client, url, auth)
    assert again == everything

    # ...and starting after the first event drops exactly that one.
    first_id = _first_event_id(infra_client, url, auth)
    tail = read_sse(infra_client, f"{url}?last_event_id={first_id}", auth)
    assert tail == everything[1:]


def _first_event_id(client: TestClient, url: str, auth: dict[str, str]) -> str:
    with client.stream("GET", url, headers=auth) as response:
        for line in response.iter_lines():
            if line.startswith("id: "):
                return line.removeprefix("id: ")
    raise AssertionError("no event id seen")


# -- limits ---------------------------------------------------------------------


def test_concurrency_slots_are_enforced(make_app, auth: dict[str, str]) -> None:
    from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

    app = make_app(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        limits={"concurrent_runs": 2, "rpm": 1000},
    )
    client = app.test_client()
    thread_id = new_thread(client, "research", auth)

    codes = [
        client.post(
            f"/api/v1/threads/{thread_id}/runs", json={"message": f"topic {i}"}, headers=auth
        ).status_code
        for i in range(6)
    ]
    assert 429 in codes
    assert codes.count(202) <= 4  # slots free as runs finish, so allow some drift


def test_the_token_bucket_refuses_a_burst(make_app, auth: dict[str, str]) -> None:
    from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

    app = make_app(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        limits={"rpm": 3, "concurrent_runs": 50},
    )
    client = app.test_client()
    thread_id = new_thread(client, "chat", auth)

    codes = [
        client.post(
            f"/api/v1/threads/{thread_id}/runs", json={"message": "hi"}, headers=auth
        ).status_code
        for _ in range(6)
    ]
    assert codes.count(429) >= 2
    body = next(
        client.post(
            f"/api/v1/threads/{thread_id}/runs", json={"message": "hi"}, headers=auth
        ).json()
        for _ in range(1)
    )
    assert body["type"].endswith("rate-limited")
    assert "retry_after" in body or "concurrent_limit" in body


# -- cancellation ------------------------------------------------------------------


def test_a_running_job_can_be_cancelled(infra_client: TestClient, auth: dict[str, str]) -> None:
    thread_id = new_thread(infra_client, "research", auth)
    run = infra_client.post(
        f"/api/v1/threads/{thread_id}/runs", json={"message": "cancel me"}, headers=auth
    ).json()

    cancelled = infra_client.post(f"/api/v1/runs/{run['run_id']}/cancel", headers=auth)
    assert cancelled.status_code == 202

    deadline = time.time() + 5
    while time.time() < deadline:
        status = infra_client.get(f"/api/v1/runs/{run['run_id']}", headers=auth).json()
        if status["status"] != "running":
            break
        time.sleep(0.1)
    assert status["status"] == "cancelled"


def test_cancelling_a_finished_run_is_a_conflict(
    infra_client: TestClient, auth: dict[str, str]
) -> None:
    thread_id = new_thread(infra_client, "chat", auth)
    result = run_and_wait(infra_client, thread_id, {"message": "quick"}, auth)
    response = infra_client.post(f"/api/v1/runs/{result['run_id']}/cancel", headers=auth)
    assert response.status_code == 409


# -- readiness -----------------------------------------------------------------------


def test_readyz_sees_both_backends(infra_client: TestClient) -> None:
    checks = infra_client.get("/api/v1/readyz").json()["checks"]
    assert checks["postgres"] == "ok"
    assert checks["redis"] == "ok"


# -- schema migration ------------------------------------------------------------------


def test_workers_can_boot_concurrently_against_a_fresh_database(
    make_app, auth: dict[str, str]
) -> None:
    """Regression: gunicorn boots N workers at once, all of them migrating.

    Two failures live here. LangGraph's `setup()` inserts into its own
    migration table without guarding against a concurrent insert, so the loser
    dies on a duplicate key. And the obvious fix -- a blocking
    `pg_advisory_lock` -- deadlocks against the `CREATE INDEX CONCURRENTLY` in
    that same setup, because a blocked lock call is itself an in-flight
    transaction that CIC waits for.
    """
    import concurrent.futures as cf

    import psycopg
    from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

    assert TEST_DATABASE_URL
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("drop schema public cascade")
        conn.execute("create schema public")

    config = {
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "auth_enabled": True,
        "bootstrap_api_key": BOOTSTRAP_KEY,
    }

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(make_app, **config) for _ in range(3)]
        done, pending = cf.wait(futures, timeout=90)

    assert not pending, "a worker hung during migration -- the lock deadlocked again"
    apps = [future.result() for future in done]  # re-raises duplicate-key failures
    assert len(apps) == 3

    for application in apps:
        assert application.test_client().get("/api/v1/readyz").json()["status"] == "ready"

    # And the migrated schema actually works.
    client = apps[0].test_client()
    thread_id = new_thread(client, "chat", auth)
    assert (
        run_and_wait(client, thread_id, {"message": "post-migration"}, auth)["status"]
        == "completed"
    )


def test_the_placeholder_bootstrap_key_is_refused(make_app) -> None:
    """A credential published in .env.example must not open the door."""
    from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

    placeholder = "ak_dev000000.change-this-local-key"
    app = make_app(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=placeholder,
    )
    response = app.test_client().get(
        "/api/v1/threads", headers={"Authorization": f"Bearer {placeholder}"}
    )
    assert response.status_code == 401
