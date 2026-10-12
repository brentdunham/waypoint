"""Daily spending budgets (observability design, phase 2).

Settings, pricing checks, the meter's charging and the 429 shape run without
infrastructure. The buckets themselves are Lua in Redis, so their tests need
``TEST_REDIS_URL`` (and the end-to-end one Postgres too).
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from tests.conftest import (
    BOOTSTRAP_KEY,
    TEST_DATABASE_URL,
    TEST_REDIS_URL,
    build_settings,
    new_thread,
    requires_infra,
    run_and_wait,
)

from waypoint.agents.models import ScriptedChatModel
from waypoint.errors import OverBudget, register_error_handlers
from waypoint.infra.ratelimit import DAY_S, Limiter
from waypoint.settings import BudgetSettings, Settings
from waypoint.usage import (
    METER,
    Meter,
    Price,
    PriceTable,
    UnpricedModelError,
    check_prices,
    report_rerank,
)

ON = {"tenant_usd_per_day": 0.35, "global_usd_per_day": 0.70}


def _env(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file="")  # type: ignore[call-arg]


# -- settings ------------------------------------------------------------------------


def test_budgets_are_off_when_unset_outside_production() -> None:
    assert not build_settings().budget.active


def test_production_must_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="ENV=production needs spending caps"):
        _env(monkeypatch, ENV="production")
    assert not _env(monkeypatch, ENV="production", BUDGET__ENABLED="false").budget.active
    monkeypatch.delenv("BUDGET__ENABLED")
    on = _env(
        monkeypatch,
        ENV="production",
        BUDGET__TENANT_USD_PER_DAY="0.35",
        BUDGET__GLOBAL_USD_PER_DAY="0.70",
        BUDGET__TENANTS='{"brent": null, "acme": 2.0}',
    )
    assert on.budget.active
    assert on.budget.tenant_budget("rev-x") == 0.35
    assert on.budget.tenant_budget("acme") == 2.0
    assert on.budget.exempt("brent")
    assert on.budget.tenant_budget("brent") is None
    assert not on.budget.exempt("acme")


def test_half_a_budget_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="BUDGET__GLOBAL_USD_PER_DAY unset"):
        _env(monkeypatch, BUDGET__TENANT_USD_PER_DAY="0.35")
    with pytest.raises(ValueError, match="both daily amounts"):
        _env(monkeypatch, BUDGET__ENABLED="true")


def test_an_override_must_be_positive_or_null() -> None:
    with pytest.raises(ValueError, match="use null to exempt"):
        BudgetSettings(**ON, tenants={"acme": 0})


def test_enabled_false_wins_over_amounts() -> None:
    budget = BudgetSettings(enabled=False, **ON)
    assert not budget.active
    assert budget.tenant_budget("x") is None


# -- unpriced models (D3) ---------------------------------------------------------------

TABLE = PriceTable(as_of="t", models={"priced": Price(1.0, 5.0)}, embeddings={}, rerank={})


def test_an_unpriced_model_stops_the_boot_when_budgets_are_on() -> None:
    settings = build_settings(model="mystery", fake_model=False, budget=ON)
    with pytest.raises(UnpricedModelError, match="mystery"):
        check_prices(settings, TABLE)


def test_with_budgets_off_it_is_only_reported() -> None:
    assert check_prices(build_settings(model="mystery", fake_model=False), TABLE) == ["mystery"]
    assert check_prices(build_settings(model="priced", fake_model=False, budget=ON), TABLE) == []


# -- the 429 ---------------------------------------------------------------------------------


def test_over_budget_is_a_429_with_retry_after_in_whole_seconds() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/x")
    async def x() -> None:
        raise OverBudget("used up", scope="tenant", retry_after=12.2)

    response = TestClient(app).get("/x")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "13"
    body = response.json()
    assert body["type"].endswith("/over-budget")
    assert (body["scope"], body["retry_after"]) == ("tenant", 12.2)


# -- the meter charges as it goes -----------------------------------------------------------


def test_the_meter_charges_each_cost_once() -> None:
    charged: list[float] = []

    async def charge(usd: float) -> None:
        await asyncio.sleep(0)  # let concurrent flushes interleave
        charged.append(usd)

    async def scenario() -> Meter:
        meter = Meter(PriceTable("t", {}, {}, {"rerank-3": 1.0}), charge=charge)
        token = METER.set(meter)
        try:
            report_rerank("rerank-3", 1_000_000)  # $1
            await asyncio.gather(meter.flush(), meter.flush(), meter.flush())
            report_rerank("rerank-3", 500_000)  # $0.50
            await meter.flush()
            await meter.flush()  # nothing new
        finally:
            METER.reset(token)
        return meter

    meter = asyncio.run(scenario())
    assert charged == [pytest.approx(1.0), pytest.approx(0.5)]
    assert meter.charged_usd == pytest.approx(1.5)


def test_a_failed_charge_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    async def broken(usd: float) -> None:
        raise ConnectionError("redis down")

    async def scenario() -> None:
        meter = Meter(PriceTable("t", {}, {}, {"rerank-3": 1.0}), charge=broken)
        meter.add("rerank", "rerank-3", tokens=1, usd=0.25)
        await meter.flush()

    asyncio.run(scenario())
    assert "could not charge the budget" in caplog.text


def test_unpriced_costs_are_not_charged() -> None:
    charged: list[float] = []

    async def charge(usd: float) -> None:
        charged.append(usd)

    async def scenario() -> None:
        meter = Meter(TABLE, charge=charge)
        meter.add("embed", "nope", tokens=10, usd=None)
        await meter.flush()

    asyncio.run(scenario())
    assert charged == []


# -- the buckets, in Redis -------------------------------------------------------------------


@pytest.fixture
def limiter() -> Iterator[tuple[Limiter, Any]]:
    from redis.asyncio import Redis

    loop = asyncio.new_event_loop()
    client = Redis.from_url(str(TEST_REDIS_URL))
    budget = BudgetSettings(**ON, tenants={"brent": None, "big": 10.0})
    yield Limiter(client, rpm=1000, concurrent=10, lease_s=60, budget=budget), loop
    loop.run_until_complete(client.aclose())
    loop.close()


def _age(limiter: Limiter, loop: Any, key: str, seconds: float) -> None:
    """Pretend the bucket was last touched ``seconds`` earlier."""

    async def go() -> None:
        ts = float(await limiter._redis.hget(key, "ts"))
        await limiter._redis.hset(key, "ts", str(ts - seconds))

    loop.run_until_complete(go())


@requires_infra
def test_admission_needs_a_positive_balance(limiter: tuple[Limiter, Any]) -> None:
    lim, loop = limiter
    run = loop.run_until_complete
    run(lim.admit("rev-a"))  # a fresh bucket is full
    run(lim.charge("rev-a", 0.50))  # one run can take it below zero
    balance = run(lim.balance("rev-a"))
    assert balance is not None
    assert balance.balance_usd == pytest.approx(-0.15, abs=1e-3)
    with pytest.raises(OverBudget) as refused:
        run(lim.admit("rev-a"))
    assert refused.value.extra["scope"] == "tenant"
    # Back above zero takes 0.15 / (0.35 / 86400) seconds, about 37,000.
    assert refused.value.extra["retry_after"] == pytest.approx(0.15 / (0.35 / DAY_S), rel=0.01)


@requires_infra
def test_the_bucket_refills_over_the_day(limiter: tuple[Limiter, Any]) -> None:
    lim, loop = limiter
    run = loop.run_until_complete
    run(lim.charge("rev-a", 0.40))  # -0.05
    with pytest.raises(OverBudget):
        run(lim.admit("rev-a"))
    _age(lim, loop, "rl:{rev-a}:usd", DAY_S / 4)  # +0.0875
    run(lim.admit("rev-a"))
    _age(lim, loop, "rl:{rev-a}:usd", DAY_S * 10)  # never above the day's budget
    balance = run(lim.balance("rev-a"))
    assert balance is not None
    assert balance.balance_usd == pytest.approx(0.35)


@requires_infra
def test_the_global_budget_caps_all_tenants_together(limiter: tuple[Limiter, Any]) -> None:
    lim, loop = limiter
    run = loop.run_until_complete
    run(lim.charge("rev-a", 0.30))
    run(lim.charge("rev-b", 0.30))
    run(lim.charge("big", 0.20))  # a raised tenant still draws on the global budget
    with pytest.raises(OverBudget) as refused:
        run(lim.admit("rev-c"))  # its own bucket is full
    assert refused.value.extra["scope"] == "server"
    assert "$" not in str(refused.value)  # no global numbers for reviewers (D4)


@requires_infra
def test_an_exempt_tenant_is_neither_refused_nor_charged(limiter: tuple[Limiter, Any]) -> None:
    lim, loop = limiter
    run = loop.run_until_complete
    run(lim.charge("rev-a", 1.0))  # global now below zero
    run(lim.admit("brent"))
    run(lim.charge("brent", 100.0))
    assert run(lim.balance("brent")) is None
    assert run(lim._redis.exists("rl:{brent}:usd")) == 0


@requires_infra
def test_admissions_at_the_edge_both_pass_and_both_are_charged(
    limiter: tuple[Limiter, Any],
) -> None:
    """O3's bound: admission does not reserve, so concurrent starts can overshoot."""
    lim, loop = limiter
    run = loop.run_until_complete
    run(lim.charge("rev-a", 0.34))  # 0.01 left

    async def two_runs() -> None:
        await asyncio.gather(lim.admit("rev-a"), lim.admit("rev-a"))
        await asyncio.gather(lim.charge("rev-a", 0.30), lim.charge("rev-a", 0.30))

    run(two_runs())
    balance = run(lim.balance("rev-a"))
    assert balance is not None
    assert balance.balance_usd == pytest.approx(0.01 - 0.60, abs=1e-3)


# -- end to end ---------------------------------------------------------------------------------


@pytest.mark.integration
@requires_infra
def test_a_tenant_over_budget_gets_a_clean_429(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ScriptedChatModel,
        "usage_script",
        staticmethod(lambda _text: {"input_tokens": 100_000, "output_tokens": 10_000}),
    )
    app = make_app(
        model="claude-sonnet-5",  # $0.30 per scripted call: one chat run spends the budget
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        auth_enabled=True,
        bootstrap_api_key=BOOTSTRAP_KEY,
        limits={"rpm": 1000, "concurrent_runs": 50},
        budget={"tenant_usd_per_day": 0.25, "global_usd_per_day": 10.0},
    )
    client: TestClient = app.test_client()
    auth = {"Authorization": f"Bearer {BOOTSTRAP_KEY}"}
    thread_id = new_thread(client, "chat", auth)
    done = run_and_wait(client, thread_id, {"message": "hello"}, auth)
    assert done["usage"]["cost_usd"] == pytest.approx(0.30)

    refused = client.post(
        f"/api/v1/threads/{thread_id}/runs", json={"message": "again"}, headers=auth
    )
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert refused.json()["type"].endswith("/over-budget")

    usage = client.get("/api/v1/usage", headers=auth).json()
    assert usage["budget_usd_per_day"] == 0.25
    assert usage["balance_usd"] == pytest.approx(-0.05, abs=1e-3)
    assert usage["cost_usd"] == pytest.approx(0.30)
