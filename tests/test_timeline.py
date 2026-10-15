"""The reviewer's run timeline: ``GET /runs/<id>/trace`` (observability design, D7).

The app runs with an in-memory span exporter; a stand-in for Jaeger's query
API serves those spans in Jaeger's JSON shape, so the endpoint, its tenant
checks and its attribute whitelist are tested without a trace backend.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient
from tests.conftest import (
    BOOTSTRAP_KEY,
    TEST_DATABASE_URL,
    TEST_REDIS_URL,
    TestApp,
    build_settings,
    new_thread,
    requires_infra,
    run_and_wait,
)
from tests.test_tracing import _settled

from waypoint import create_app
from waypoint.agents.models import ScriptedChatModel
from waypoint.api import runs as runs_api
from waypoint.errors import NotFound
from waypoint.infra.security import Principal
from waypoint.settings import TracingSettings
from waypoint.timeline import TraceNotReadyError, TraceUnavailableError, timeline

MARKER = "timeline-marker-41c2"
PANEL = {"panel": True, "query_url": "http://jaeger.test:16686", "content": True}
"""Content on, to prove the whitelist drops prompt text even when spans carry it."""


def jaeger_json(spans: tuple[ReadableSpan, ...], trace_id: int) -> dict[str, Any]:
    """Spans as Jaeger's ``/api/traces/<id>`` returns them."""
    out = []
    for span in spans:
        if span.context.trace_id != trace_id:
            continue
        tags = [{"key": k, "value": v} for k, v in (span.attributes or {}).items()]
        if not span.status.is_ok:
            tags.append({"key": "error", "value": True})
        refs = (
            [{"refType": "CHILD_OF", "spanID": format(span.parent.span_id, "016x")}]
            if span.parent is not None
            else []
        )
        out.append(
            {
                "spanID": format(span.context.span_id, "016x"),
                "operationName": span.name,
                "references": refs,
                "startTime": (span.start_time or 0) // 1000,
                "duration": ((span.end_time or 0) - (span.start_time or 0)) // 1000,
                "tags": tags,
            }
        )
    return {"traceID": format(trace_id, "032x"), "spans": out}


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Factory: a traced app whose trace endpoint reads its own exported spans."""
    monkeypatch.setattr(
        ScriptedChatModel,
        "usage_script",
        staticmethod(lambda _t: {"input_tokens": 1_000, "output_tokens": 200}),
    )
    apps: list[TestApp] = []

    def factory(**overrides: Any) -> tuple[TestClient, InMemorySpanExporter]:
        exporter = InMemorySpanExporter()

        async def fetch(query_url: str, trace_id: str) -> dict[str, Any]:
            spans = exporter.get_finished_spans()
            found = jaeger_json(spans, int(trace_id, 16))
            if not found["spans"]:
                raise TraceNotReadyError
            return found

        monkeypatch.setattr(runs_api, "fetch_trace", fetch)
        settings = build_settings(model="claude-sonnet-5", tracing=PANEL, **overrides)
        app = TestApp(create_app(settings, span_exporter=exporter))
        apps.append(app)
        return app.test_client(), exporter

    yield factory
    for app in apps:
        app.close()


def test_the_panel_needs_a_trace_backend() -> None:
    with pytest.raises(ValueError, match="TRACING__QUERY_URL"):
        TracingSettings(panel=True)


@pytest.mark.parametrize("first_exported", [False, True])
def test_recovery_timeline_uses_the_original_start_and_final_cumulative_cost(
    first_exported: bool,
) -> None:
    def span(span_id: str, name: str, start: int, duration: int, cost: float) -> dict[str, Any]:
        return {
            "spanID": span_id,
            "operationName": name,
            "startTime": start,
            "duration": duration,
            "tags": [
                {"key": k, "value": v}
                for k, v in {
                    "waypoint.run_id": "run",
                    "waypoint.tenant_id": "tenant",
                    "waypoint.cost_usd": cost,
                    "waypoint.status": "completed" if span_id == "attempt2" else "handed_off",
                    "waypoint.run.created_at": "1970-01-01T00:00:00+00:00",
                }.items()
            ],
        }

    spans = [
        span("attempt2", "run.attempt", 30_000, 20_000, 3.0),
        span("node", "node model", 35_000, 5_000, 0),
    ]
    if first_exported:
        spans.append(span("attempt1", "run chat", 10_000, 10_000, 1.0))
    result = timeline({"traceID": "trace", "spans": spans}, run_id="run", tenant_id="tenant")
    assert result["duration_ms"] == 50
    assert result["cost_usd"] == 3.0
    assert len(result["steps"]) == 1
    assert result["steps"][0]["start_ms"] == 35


@pytest.mark.parametrize("status", ["handed_off", "ownership_lost", "running"])
def test_recovery_timeline_waits_for_the_terminal_attempt(status: str) -> None:
    trace = {
        "spans": [
            {
                "spanID": "first-attempt",
                "operationName": "run.attempt",
                "tags": [
                    {"key": "waypoint.run_id", "value": "run"},
                    {"key": "waypoint.tenant_id", "value": "tenant"},
                    {"key": "waypoint.status", "value": status},
                ],
            }
        ]
    }
    with pytest.raises(TraceNotReadyError):
        timeline(trace, run_id="run", tenant_id="tenant")


def test_off_by_default_the_endpoint_is_absent(client: TestClient) -> None:
    done = run_and_wait(client, new_thread(client, "chat"), {"message": "hi"})
    response = client.get(f"/api/v1/runs/{done['run_id']}/trace")
    assert response.status_code == 404
    assert "not enabled" in response.json()["detail"]


def test_a_chat_run_reads_as_steps_with_model_and_cost(panel: Any) -> None:
    client, exporter = panel()
    done = run_and_wait(client, new_thread(client, "chat"), {"message": f"hi {MARKER}"})
    _settled(exporter, "run chat")
    body = client.get(f"/api/v1/runs/{done['run_id']}/trace").json()

    assert body["status"] == "ready"
    assert body["cost_usd"] == pytest.approx(done["usage"]["cost_usd"])
    assert body["duration_ms"] > 0
    steps = {s["name"]: s for s in body["steps"]}
    assert {"node recall_memories", "node model"} <= set(steps)
    [call] = [s for s in body["steps"] if s["kind"] == "model"]
    assert call["parent"] == steps["node model"]["id"]
    assert call["model"] == "claude-sonnet-5"
    assert call["role"] == "assistant"
    assert (call["input_tokens"], call["output_tokens"]) == (1_000, 200)
    assert call["cost_usd"] == pytest.approx((1_000 * 2 + 200 * 10) / 1e6)
    assert all(s["start_ms"] >= 0 for s in body["steps"])
    # The whitelist: no ids, no tenant, and no prompt even though spans carry it.
    assert MARKER not in str(body)
    assert "tenant" not in str(body)


def test_a_research_run_shows_workers_searches_and_documents(panel: Any) -> None:
    client, exporter = panel(rag={"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 64})
    corpus_id = client.post("/api/v1/corpora", json={"name": "docs"}).json()["corpus_id"]
    files = [("files", ("s.md", b"# S\n\n## Resume\n\nUse Last-Event-ID.\n", "text/markdown"))]
    client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files)
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "resuming", "input": {"corpus_id": corpus_id}})
    done = run_and_wait(client, thread_id, {"resume": {"approved": True}})
    _settled(exporter, "run research", 2)
    body = client.get(f"/api/v1/runs/{done['run_id']}/trace").json()

    kinds = {s["kind"] for s in body["steps"]}
    assert {"node", "model", "retrieval"} <= kinds
    workers = {s["id"] for s in body["steps"] if s["name"] == "node investigate"}
    searches = [s for s in body["steps"] if s["kind"] == "retrieval"]
    assert searches
    assert all(s["parent"] in workers and s["hits"] >= 1 and s["mode"] for s in searches)


def test_pending_until_the_backend_has_the_run(panel: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = panel()
    done = run_and_wait(client, new_thread(client, "chat"), {"message": "hi"})

    async def not_yet(query_url: str, trace_id: str) -> dict[str, Any]:
        raise TraceNotReadyError

    monkeypatch.setattr(runs_api, "fetch_trace", not_yet)
    body = client.get(f"/api/v1/runs/{done['run_id']}/trace").json()
    assert body == {
        "status": "pending",
        "run_id": done["run_id"],
        "trace_id": None,
        "duration_ms": None,
        "cost_usd": None,
        "steps": [],
    }


def test_an_unreadable_backend_is_a_503(panel: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = panel()
    done = run_and_wait(client, new_thread(client, "chat"), {"message": "hi"})

    async def down(query_url: str, trace_id: str) -> dict[str, Any]:
        raise TraceUnavailableError("ConnectError")

    monkeypatch.setattr(runs_api, "fetch_trace", down)
    assert client.get(f"/api/v1/runs/{done['run_id']}/trace").status_code == 503


def test_another_tenants_run_is_not_found(panel: Any) -> None:
    client, _ = panel()
    done = run_and_wait(client, new_thread(client, "chat"), {"message": "hi"})
    app: Any = client.app
    other = Principal(tenant_id="someone-else", user_id="u", key_id="k", scopes=frozenset("*"))
    with pytest.raises(NotFound):
        client.portal.call(app.state.runtime.runs.trace_id, done["run_id"], other)  # type: ignore[union-attr]


def test_a_trace_that_names_another_run_is_refused() -> None:
    trace = {
        "traceID": "ab",
        "spans": [
            {
                "spanID": "01",
                "operationName": "run chat",
                "tags": [
                    {"key": "waypoint.run_id", "value": "other-run"},
                    {"key": "waypoint.tenant_id", "value": "acme"},
                ],
            }
        ],
    }
    from waypoint.timeline import TraceMismatchError

    with pytest.raises(TraceMismatchError):
        timeline(trace, run_id="my-run", tenant_id="acme")
    with pytest.raises(TraceMismatchError):
        timeline(trace, run_id="other-run", tenant_id="not-acme")


def test_a_partial_trace_is_pending_not_wrong() -> None:
    """The run span ends last; a trace without it is still being written."""
    trace = {
        "spans": [
            {
                "spanID": "02",
                "operationName": "node model",
                "tags": [
                    {"key": "waypoint.run_id", "value": "r"},
                    {"key": "waypoint.tenant_id", "value": "t"},
                ],
            }
        ]
    }
    with pytest.raises(TraceNotReadyError):
        timeline(trace, run_id="r", tenant_id="t")


# -- after the Redis record expires, run_audit still knows the trace -----------------------


@pytest.mark.integration
@requires_infra
def test_the_trace_outlives_the_run_record(panel: Any) -> None:
    client, exporter = panel(
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        limits={"rpm": 1000, "concurrent_runs": 50},
    )
    auth = {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}
    done = run_and_wait(client, new_thread(client, "chat", auth), {"message": "hi"}, auth)
    _settled(exporter, "run chat")

    import redis

    r = redis.Redis.from_url(str(TEST_REDIS_URL))
    try:
        assert r.delete(f"run:{done['run_id']}:meta") == 1  # as if an hour passed
    finally:
        r.close()
    body = client.get(f"/api/v1/runs/{done['run_id']}/trace", headers=auth).json()
    assert body["status"] == "ready"
    assert any(s["kind"] == "model" for s in body["steps"])
