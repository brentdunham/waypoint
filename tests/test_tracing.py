"""OpenTelemetry tracing (observability design, phase 3). Unit tier.

An in-memory exporter stands in for OTLP, and the scripted model reports
scripted usage, so one run's whole trace can be read back and checked: the
request span, the run span linked to it (not parented), node spans under the
run, model spans under their nodes with GenAI and ``waypoint.*`` attributes,
retrieval under the worker that searched, and no prompt text unless
``TRACING__CONTENT`` asks for it.
"""

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient
from tests.conftest import TestApp, build_settings, new_thread, read_sse, run_and_wait

from waypoint import create_app
from waypoint.agents.models import ScriptedChatModel
from waypoint.tracing import NOOP, TraceHandler, Tracing, setup_tracing

MARKER = "tracing-marker-7f3a"
"""In the user's message, so a test can look for prompt text in spans."""
USAGE = {"input_tokens": 1_000, "output_tokens": 200}
RAG = {"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 64}


@pytest.fixture
def scripted_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ScriptedChatModel, "usage_script", staticmethod(lambda _t: USAGE))


@pytest.fixture
def traced() -> Iterator[Any]:
    """Factory: an app whose spans land in an in-memory exporter."""
    apps: list[TestApp] = []

    def factory(**overrides: Any) -> tuple[TestClient, InMemorySpanExporter]:
        exporter = InMemorySpanExporter()
        app = TestApp(create_app(build_settings(**overrides), span_exporter=exporter))
        apps.append(app)
        return app.test_client(), exporter

    yield factory
    for app in apps:
        app.close()


def _by_name(spans: tuple[ReadableSpan, ...], prefix: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name.startswith(prefix)]


def _settled(exporter: InMemorySpanExporter, run: str, count: int = 1) -> tuple[ReadableSpan, ...]:
    """The finished spans once ``count`` run spans named ``run`` have ended.

    The run span ends in the run task's ``finally``, just after the terminal
    event a test waits for, so reading at once can race it.
    """
    deadline = time.monotonic() + 5
    while True:
        spans = exporter.get_finished_spans()
        if len(_by_name(spans, run)) >= count or time.monotonic() > deadline:
            return spans
        time.sleep(0.01)


def _parent(span: ReadableSpan, spans: tuple[ReadableSpan, ...]) -> ReadableSpan | None:
    if span.parent is None:
        return None
    return next((s for s in spans if s.context.span_id == span.parent.span_id), None)


# -- off by default ---------------------------------------------------------------------------


def test_tracing_is_off_without_an_endpoint(client: TestClient) -> None:
    assert setup_tracing(build_settings()) is NOOP
    done = run_and_wait(client, new_thread(client, "chat"), {"message": "hi"})
    record = client.get(f"/api/v1/runs/{done['run_id']}").json()
    assert record["trace_id"] is None


def test_an_endpoint_turns_it_on() -> None:
    tracing = setup_tracing(build_settings(otel_exporter_otlp_endpoint="http://localhost:4318"))
    try:
        assert tracing.enabled
    finally:
        tracing.shutdown()


# -- one chat run, end to end -----------------------------------------------------------------


@pytest.mark.usefixtures("scripted_usage")
def test_a_chat_run_is_one_trace_linked_to_its_request(traced: Any) -> None:
    client, exporter = traced(model="claude-sonnet-5")
    thread_id = new_thread(client, "chat")
    done = run_and_wait(client, thread_id, {"message": f"hello {MARKER}"})
    spans = _settled(exporter, "run chat")

    [run] = _by_name(spans, "run chat")
    assert run.parent is None  # its own trace (O8)
    record = client.get(f"/api/v1/runs/{done['run_id']}").json()
    assert record["trace_id"] == format(run.context.trace_id, "032x")
    assert run.attributes["waypoint.thread_id"] == thread_id
    assert run.attributes["waypoint.status"] == "completed"
    assert run.attributes["waypoint.cost_usd"] == pytest.approx(done["usage"]["cost_usd"])

    # Linked to the POST that started it, which is a different trace.
    # Named by route, not by URL: no ids in span names.
    [request] = [s for s in spans if s.name == "POST /api/v1/threads/{thread_id}/runs"]
    assert [link.context.span_id for link in run.links] == [request.context.span_id]
    assert request.context.trace_id != run.context.trace_id

    # Nodes under the run, the model call under its node.
    nodes = {s.name: s for s in _by_name(spans, "node ")}
    assert {"node recall_memories", "node model"} <= set(nodes)
    assert all(_parent(n, spans) is run for n in nodes.values())
    [chat] = _by_name(spans, "chat ")
    assert _parent(chat, spans) is nodes["node model"]
    attrs = chat.attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-5"
    assert attrs["gen_ai.usage.input_tokens"] == 1_000
    assert attrs["gen_ai.usage.output_tokens"] == 200
    assert attrs["waypoint.cost_usd"] == pytest.approx((1_000 * 2 + 200 * 10) / 1e6)
    assert attrs["waypoint.role"] == "assistant"
    assert attrs["waypoint.run_id"] == done["run_id"]
    assert attrs["waypoint.tenant_id"]

    # O9: the prompt is nowhere in the trace.
    for span in spans:
        for value in (span.attributes or {}).values():
            assert MARKER not in str(value), (span.name, value)


def test_content_goes_on_spans_only_when_asked(traced: Any) -> None:
    client, exporter = traced(tracing={"content": True})
    run_and_wait(client, new_thread(client, "chat"), {"message": f"hello {MARKER}"})
    [chat] = _by_name(_settled(exporter, "run chat"), "chat ")
    assert MARKER in str(chat.attributes["waypoint.prompt"])
    assert "waypoint.completion" in chat.attributes


def test_health_probes_are_not_traced(traced: Any) -> None:
    client, exporter = traced()
    exporter.clear()
    assert client.get("/api/v1/healthz").status_code == 200
    assert exporter.get_finished_spans() == ()


def test_request_spans_are_named_by_route_never_by_url(traced: Any) -> None:
    client, exporter = traced()
    thread_id = new_thread(client, "chat")
    client.get(f"/api/v1/threads/{thread_id}/state")
    client.get("/api/v1/no/such/thing-123")
    names = {s.name for s in exporter.get_finished_spans()}
    assert "GET /api/v1/threads/{thread_id}/state" in names
    assert "GET" in names  # the 404: no route, so no path
    assert not any(thread_id in n or "thing-123" in n for n in names)


def test_the_run_started_event_carries_the_trace_id(traced: Any) -> None:
    client, _ = traced()
    thread_id = new_thread(client, "chat")
    started = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "hi"}).json()
    with client.stream("GET", started["events_url"]) as response:
        for line in response.iter_lines():
            if line.startswith("data: ") and '"trace_id"' in line:
                assert started["trace_id"] in line
                break
        else:  # pragma: no cover
            raise AssertionError("run.started had no trace_id")


# -- research: two runs, retrieval under its worker -----------------------------------------------


@pytest.mark.usefixtures("scripted_usage")
def test_a_resumed_run_is_a_new_trace_on_the_same_thread(traced: Any) -> None:
    client, exporter = traced(rag=RAG)
    corpus_id = str(client.post("/api/v1/corpora", json={"name": "docs"}).json()["corpus_id"])
    files = [
        (
            "files",
            ("streams.md", b"# Streams\n\n## Resume\n\nUse Last-Event-ID.\n", "text/markdown"),
        )
    ]
    upload = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files).json()
    assert read_sse(client, upload["events_url"])[-1] == "run.completed"

    thread_id = new_thread(client, "research")
    body = {"message": "how streams resume", "input": {"corpus_id": corpus_id}}
    paused = run_and_wait(client, thread_id, body)
    done = run_and_wait(client, thread_id, {"resume": {"approved": True}})
    assert (paused["status"], done["status"]) == ("interrupted", "completed")

    spans = _settled(exporter, "run research", 2)
    runs = _by_name(spans, "run research")
    assert len(runs) == 2
    first, second = sorted(runs, key=lambda s: s.start_time or 0)
    assert first.context.trace_id != second.context.trace_id
    assert first.attributes["waypoint.status"] == "interrupted"
    assert (first.attributes["waypoint.resume"], second.attributes["waypoint.resume"]) == (
        False,
        True,
    )
    assert {r.attributes["waypoint.thread_id"] for r in runs} == {thread_id}

    # The approval interrupt is how the plan pauses, not an error.
    approve = [
        s for s in _by_name(spans, "node approve") if s.context.trace_id == first.context.trace_id
    ]
    assert approve
    assert all(s.status.is_ok for s in approve)

    retrievals = _by_name(spans, "rag.retrieve")
    assert retrievals, "every worker searches the corpus"
    for span in retrievals:
        parent = _parent(span, spans)
        assert parent is not None
        assert parent.name == "node investigate"
        assert span.context.trace_id == second.context.trace_id
        assert span.attributes["waypoint.rag.mode"]
        assert span.attributes["waypoint.rag.hits"] >= 1

    # The searcher's model spans sit under the same worker nodes.
    searchers = [
        s for s in _by_name(spans, "chat ") if s.attributes.get("waypoint.role") == "searcher"
    ]
    assert searchers
    assert all(_parent(s, spans).name == "node investigate" for s in searchers)  # type: ignore[union-attr]


# -- the handler on its own ---------------------------------------------------------------------


def _handler() -> tuple[TraceHandler, InMemorySpanExporter, Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = Tracing(tracer=provider.get_tracer("t"), provider=provider)
    root = tracing.tracer.start_span("run test")
    return TraceHandler(tracing, root, attributes={"waypoint.run_id": "r"}), exporter, root


def test_concurrent_calls_nest_by_parent_run_not_by_context() -> None:
    handler, exporter, root = _handler()
    graph, a, b, inner, call_a, call_b = (uuid.uuid4() for _ in range(6))

    async def scenario() -> None:
        await handler.on_chain_start({}, {}, run_id=graph, name="LangGraph")
        await handler.on_chain_start(
            {}, {}, run_id=a, parent_run_id=graph, name="w", metadata={"langgraph_node": "w"}
        )
        await handler.on_chain_start(
            {}, {}, run_id=b, parent_run_id=graph, name="w", metadata={"langgraph_node": "w"}
        )
        # A node's inner runnable inherits the metadata but gets no span.
        await handler.on_chain_start(
            {},
            {},
            run_id=inner,
            parent_run_id=b,
            name="RunnableSequence",
            metadata={"langgraph_node": "w"},
        )
        # Interleaved: B's call starts before A's.
        await handler.on_chat_model_start(
            {}, [[]], run_id=call_b, parent_run_id=inner, metadata={"model_name": "m"}
        )
        await handler.on_chat_model_start(
            {}, [[]], run_id=call_a, parent_run_id=a, metadata={"model_name": "m"}
        )
        result = LLMResult(generations=[[ChatGeneration(message=AIMessage("x"))]])
        await handler.on_llm_end(result, run_id=call_a)
        await handler.on_llm_end(result, run_id=call_b)
        for run in (inner, b, a, graph):
            await handler.on_chain_end({}, run_id=run)

    asyncio.run(scenario())
    root.end()
    spans = exporter.get_finished_spans()
    nodes = _by_name(spans, "node w")
    assert len(nodes) == 2
    assert all(
        _parent(n, spans) is not None and _parent(n, spans).name == "run test" for n in nodes
    )  # type: ignore[union-attr]
    calls = _by_name(spans, "chat m")
    assert len(calls) == 2
    assert {_parent(c, spans).context.span_id for c in calls} == {n.context.span_id for n in nodes}  # type: ignore[union-attr]


def test_unfinished_spans_are_closed_with_the_run() -> None:
    handler, exporter, _ = _handler()
    node = uuid.uuid4()
    asyncio.run(
        handler.on_chain_start({}, {}, run_id=node, name="n", metadata={"langgraph_node": "n"})
    )
    handler.close()  # a cancelled run never sees on_chain_end
    [span] = _by_name(exporter.get_finished_spans(), "node n")
    assert span.attributes["waypoint.unfinished"] is True


def test_a_failing_model_call_marks_its_span() -> None:
    handler, exporter, _ = _handler()
    call = uuid.uuid4()

    async def scenario() -> None:
        await handler.on_chat_model_start({}, [[]], run_id=call, metadata={"model_name": "m"})
        await handler.on_llm_error(TimeoutError("slow"), run_id=call)

    asyncio.run(scenario())
    [span] = _by_name(exporter.get_finished_spans(), "chat m")
    assert not span.status.is_ok
    assert span.attributes["error.type"] == "TimeoutError"
