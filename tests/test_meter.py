"""Cost per run (observability design, phase 1).

The scripted model reports scripted usage (``ScriptedChatModel.usage_script``),
so a fake run has known tokens and searches, and these check what the meter,
the run record, the terminal event and ``run_audit`` make of them.
"""

import asyncio
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from starlette.testclient import TestClient
from tests.conftest import new_thread, read_sse, requires_infra, run_and_wait

from waypoint.agents.models import ScriptedChatModel
from waypoint.usage import (
    METER,
    Meter,
    Price,
    PriceTable,
    call_cost,
    report_embedding,
    report_rerank,
    usage_of,
)

TABLE = PriceTable(
    as_of="test",
    models={
        "big": Price(input=2.0, output=10.0, search=0.01),
        "small": Price(input=0.1, output=0.5, search=0.01),
    },
    embeddings={"voyageai:voyage-4": 0.06},
    rerank={"rerank-3": 0.05},
)


def _reply(**usage: Any) -> AIMessage:
    searches = usage.pop("searches", 0)
    usage.setdefault("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
    return AIMessage(
        "x",
        usage_metadata=usage,  # type: ignore[arg-type]
        response_metadata={"usage": {"server_tool_use": {"web_search_requests": searches}}},
    )


# -- cache pricing -----------------------------------------------------------------


def test_cache_reads_and_writes_are_priced_at_their_multiples() -> None:
    price = Price(input=1.0, output=5.0)
    # 1M uncached + 1M read at 0.1 + 1M 5-minute write at 1.25 + 1M 1-hour write at 2
    usd = call_cost(
        price,
        1_000_000,
        0,
        cache_read_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )
    assert usd == pytest.approx(1.0 + 0.1 + 1.25 + 2.0)


def test_the_long_rate_counts_cached_input_too() -> None:
    price = Price(input=0.1, output=0.5, long_threshold=100, long_input=0.5, long_output=2.5)
    # 50 uncached + 60 read = 110 input tokens: over the threshold
    assert call_cost(price, 50, 0, cache_read_tokens=60) == pytest.approx(
        (50 + 60 * 0.1) * 0.5 / 1_000_000
    )


def test_usage_of_splits_cached_input_out_of_langchains_total() -> None:
    message = _reply(
        input_tokens=1_000,  # langchain's total, cached parts included
        output_tokens=50,
        total_tokens=1_050,
        input_token_details={
            "cache_read": 600,
            "cache_creation": 0,
            "ephemeral_5m_input_tokens": 100,
            "ephemeral_1h_input_tokens": 200,
        },
        searches=3,
    )
    usage = usage_of(message)
    assert usage.input_tokens == 100
    assert (usage.cache_read_tokens, usage.cache_write_5m_tokens) == (600, 100)
    assert usage.cache_write_1h_tokens == 200
    assert usage.total_input_tokens == 1_000
    assert (usage.output_tokens, usage.searches) == (50, 3)


def test_a_reply_without_usage_counts_zero() -> None:
    assert usage_of(AIMessage("x")).total_input_tokens == 0


# -- the meter ---------------------------------------------------------------------------


def _call(meter: Meter, message: AIMessage, **metadata: Any) -> None:
    run_id = uuid.uuid4()

    async def go() -> None:
        await meter.on_chat_model_start({}, [[]], run_id=run_id, metadata=metadata)
        await meter.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id
        )

    asyncio.run(go())


def test_meter_sums_per_role_and_prices_each_call() -> None:
    meter = Meter(TABLE)
    for _ in range(2):
        _call(
            meter,
            _reply(input_tokens=1_000, output_tokens=100, searches=2),
            model_role="searcher",
            model_name="small",
        )
    _call(
        meter,
        _reply(input_tokens=10_000, output_tokens=1_000),
        model_role="writer",
        model_name="big",
    )
    summary = meter.summary()
    searcher = summary["roles"]["searcher"]
    assert (searcher["calls"], searcher["input_tokens"], searcher["searches"]) == (2, 2_000, 4)
    assert searcher["cost_usd"] == pytest.approx(2 * (1_000 * 0.1 + 100 * 0.5) / 1e6 + 0.04)
    assert summary["roles"]["writer"]["cost_usd"] == pytest.approx(0.03)
    assert summary["cost_usd"] == pytest.approx(searcher["cost_usd"] + 0.03)
    assert (summary["input_tokens"], summary["output_tokens"], summary["searches"]) == (
        12_000,
        1_200,
        4,
    )


def test_an_unpriced_model_makes_the_total_unknown_not_low() -> None:
    meter = Meter(TABLE)
    _call(meter, _reply(input_tokens=10, output_tokens=10), model_role="a", model_name="big")
    _call(meter, _reply(input_tokens=10, output_tokens=10), model_role="b", model_name="nope")
    summary = meter.summary()
    assert summary["roles"]["a"]["cost_usd"] is not None
    assert summary["roles"]["b"]["cost_usd"] is None
    assert summary["cost_usd"] is None
    assert summary["input_tokens"] == 20  # tokens are still counted


def test_a_model_without_role_metadata_is_named_by_langchain() -> None:
    meter = Meter(TABLE)
    _call(meter, _reply(input_tokens=10, output_tokens=0), ls_model_name="small")
    assert meter.summary()["roles"]["unknown"]["model"] == "small"


def test_a_broken_reply_costs_a_usage_line_not_the_run() -> None:
    meter = Meter(TABLE)
    bad = _reply(input_tokens=1, output_tokens=0)
    bad.usage_metadata = {"input_tokens": "lots"}  # type: ignore[typeddict-item]  # not a number
    _call(meter, bad, model_role="writer", model_name="big")  # must not raise
    assert "writer" not in meter.summary()["roles"]


def test_embeddings_and_reranking_report_into_the_current_meter() -> None:
    report_embedding("voyageai:voyage-4", 1_000)  # no meter: nothing happens
    meter = Meter(TABLE)
    token = METER.set(meter)
    try:
        report_embedding("voyageai:voyage-4", 1_000_000)
        report_rerank("rerank-3", 2_000_000)
        report_rerank("rerank-3", 0)
    finally:
        METER.reset(token)
    roles = meter.summary()["roles"]
    assert roles["embed"] == {"model": "voyageai:voyage-4", "tokens": 1_000_000, "cost_usd": 0.06}
    assert roles["rerank"] == {"model": "rerank-3", "tokens": 2_000_000, "cost_usd": 0.1}
    assert meter.summary()["cost_usd"] == pytest.approx(0.16)


# -- end to end, on the scripted model -------------------------------------------------

USAGE = {"input_tokens": 1_000, "output_tokens": 200, "cache_read": 500, "searches": 0}
"""Every scripted reply: 1,500 input tokens (500 of them cache reads), 200 out."""


@pytest.fixture
def scripted_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    def script(text: str) -> dict[str, int]:
        if text.startswith("Answer this research question"):
            return USAGE | {"searches": 2}
        return USAGE

    monkeypatch.setattr(ScriptedChatModel, "usage_script", staticmethod(script))


def _priced(model: str, calls: int, searches: int = 0) -> float:
    from waypoint.usage import load_prices

    price = load_prices().models[model]
    one = call_cost(price, 1_000, 200, cache_read_tokens=500)
    return calls * one + searches * price.search


@pytest.mark.usefixtures("scripted_usage")
def test_a_chat_run_reports_its_cost(make_app: Any) -> None:
    client: TestClient = make_app(model="claude-haiku-5-5").test_client()
    thread_id = new_thread(client, "chat")
    done = run_and_wait(client, thread_id, {"message": "hello"})
    usage = done["usage"]
    assert usage["roles"]["assistant"]["calls"] == 1
    assert usage["roles"]["assistant"]["cache_read_tokens"] == 500
    assert (usage["input_tokens"], usage["output_tokens"]) == (1_500, 200)
    assert usage["cost_usd"] == pytest.approx(_priced("claude-haiku-5-5", 1), abs=1e-6)
    record = client.get(f"/api/v1/runs/{done['run_id']}").json()
    assert record["usage"] == usage


@pytest.mark.usefixtures("scripted_usage")
def test_a_research_run_reports_cost_per_role_on_each_leg(make_app: Any) -> None:
    client: TestClient = make_app(
        model="claude-sonnet-5", models={"research": {"searcher": "claude-haiku-5-5"}}
    ).test_client()
    thread_id = new_thread(client, "research")
    paused = run_and_wait(client, thread_id, {"message": "tidal power"})
    assert paused["status"] == "interrupted"
    # The plan is structured output, which the scripted model answers without
    # a chat model call: nothing to bill before approval.
    assert paused["usage"]["cost_usd"] == 0

    resumed = run_and_wait(client, thread_id, {"resume": {"approved": True}})
    assert resumed["status"] == "completed"
    roles = resumed["usage"]["roles"]
    searcher, writer = roles["searcher"], roles["writer"]
    assert searcher["model"] == "claude-haiku-5-5"
    assert searcher["searches"] == 2 * searcher["calls"]
    assert writer["calls"] == 1
    assert searcher["cost_usd"] == pytest.approx(
        _priced("claude-haiku-5-5", searcher["calls"], searcher["searches"]), abs=1e-6
    )
    assert writer["cost_usd"] == pytest.approx(_priced("claude-sonnet-5", 1), abs=1e-6)
    assert resumed["usage"]["cost_usd"] == pytest.approx(
        sum(r["cost_usd"] for r in roles.values()), abs=1e-5
    )


def test_a_run_without_usage_reports_zero(client: TestClient) -> None:
    done = run_and_wait(client, new_thread(client, "chat"), {"message": "hi"})
    assert done["usage"]["cost_usd"] == 0
    assert done["usage"]["input_tokens"] == 0


def test_usage_needs_the_database(client: TestClient) -> None:
    response = client.get("/api/v1/usage")
    assert response.status_code == 503


# -- run_audit and GET /usage, against Postgres ---------------------------------------


@pytest.mark.integration
@requires_infra
@pytest.mark.usefixtures("scripted_usage")
def test_run_audit_records_cost_and_usage_sums_it(
    infra_client: TestClient, auth: dict[str, str]
) -> None:
    thread_id = new_thread(infra_client, "chat", auth)
    done = run_and_wait(infra_client, thread_id, {"message": "hello"}, auth)
    events = read_sse(infra_client, f"/api/v1/runs/{done['run_id']}/events", auth)
    assert events[-1] == "run.completed"
    usage = infra_client.get("/api/v1/usage", headers=auth).json()
    assert usage["runs"] >= 1
    assert usage["cost_usd"] >= done["usage"]["cost_usd"] > 0
    assert usage["input_tokens"] >= 1_500
    assert usage["by_agent"]["chat"]["runs"] >= 1
    assert infra_client.get("/api/v1/usage?days=0", headers=auth).status_code == 422
