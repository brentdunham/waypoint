"""Pricing, shared by the service and the evals (observability design, phase 0).

The parity tests are the point: moving ``call_cost`` and the price table out of
``evals/`` must not change a single reported cost. ``_old_call_cost`` and
``_old_models`` are the pre-move code from ``evals/scorecard.py`` (90a7e76),
kept verbatim as the oracle.
"""

import tomllib
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

import pytest

from evals.driver import Call, Transcript
from evals.scorecard import CaseResult, usage_summary
from waypoint.usage import Price, call_cost, load_prices, parse_prices

# -- the oracle: evals/scorecard.py before the move ------------------------------


def _old_models() -> dict[str, dict[str, float]]:
    text = files("waypoint").joinpath("prices.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    return {k: v for k, v in data.get("models", {}).items() if isinstance(v, dict)}


def _old_call_cost(
    price: Mapping[str, float], input_tokens: int, output_tokens: int, searches: int
) -> float:
    long = "long_threshold" in price and input_tokens > price["long_threshold"]
    rate_in = price["long_input"] if long else price["input"]
    rate_out = price["long_output"] if long else price["output"]
    tokens = input_tokens * rate_in + output_tokens * rate_out
    return tokens / 1_000_000 + searches * price.get("search", 0.0)


# -- call_cost ---------------------------------------------------------------------

LONG = Price(input=0.1, output=0.5, search=0.01,
             long_threshold=100_000, long_input=0.5, long_output=2.5)  # fmt: skip


def test_long_prompts_are_priced_at_the_long_rate() -> None:
    assert call_cost(LONG, 50_000, 1_000, 2) == pytest.approx(0.005 + 0.0005 + 0.02)
    assert call_cost(LONG, 200_000, 1_000, 0) == pytest.approx(0.1 + 0.0025)
    assert call_cost(Price(input=2.0, output=10.0), 1_000_000, 0, 5) == pytest.approx(2.0)


def test_the_threshold_itself_is_the_short_rate() -> None:
    assert call_cost(LONG, 100_000, 0) == pytest.approx(0.01)
    assert call_cost(LONG, 100_001, 0) == pytest.approx(100_001 * 0.5 / 1_000_000)


def test_searches_cost_per_request_and_default_to_free() -> None:
    assert call_cost(Price(input=1.0, output=5.0, search=0.01), 0, 0, 3) == pytest.approx(0.03)
    assert call_cost(Price(input=1.0, output=5.0), 0, 0, 3) == 0.0


def test_long_rates_come_as_a_set() -> None:
    with pytest.raises(ValueError, match="go together"):
        Price(input=1.0, output=5.0, long_threshold=100_000)


# -- the table -----------------------------------------------------------------------


def test_the_table_ships_in_the_package_and_parses() -> None:
    table = load_prices()
    assert table.as_of
    assert table.models, "no chat model prices"
    assert table.embeddings
    assert table.rerank
    assert load_prices() is table  # read once per process
    for name, price in table.models.items():
        assert price.input > 0, name
        assert price.output > 0, name


def test_parse_prices_ignores_non_table_model_keys() -> None:
    table = parse_prices(
        'as_of = "x"\n[models]\nnote = "ignored"\n[models.m]\ninput = 1\noutput = 2\n'
    )
    assert table.models == {"m": Price(input=1.0, output=2.0)}
    assert table.embeddings == {}
    assert table.rerank == {}


# -- parity with the pre-move scorecard ----------------------------------------------------

USAGES = [
    (0, 0, 0),
    (1, 1, 0),
    (12_345, 678, 0),
    (41_200, 2_100, 0),
    (99_999, 4_000, 2),
    (100_000, 4_000, 1),
    (100_001, 4_000, 1),
    (250_000, 8_000, 5),
    (1_000_000, 1_000_000, 10),
]
"""Token and search counts around every rate boundary in the table."""


@pytest.mark.parametrize("model", sorted(_old_models()))
@pytest.mark.parametrize(("tokens_in", "tokens_out", "searches"), USAGES)
def test_call_cost_matches_the_old_one(
    model: str, tokens_in: int, tokens_out: int, searches: int
) -> None:
    old = _old_call_cost(_old_models()[model], tokens_in, tokens_out, searches)
    new = call_cost(load_prices().models[model], tokens_in, tokens_out, searches)
    assert new == pytest.approx(old, rel=1e-12, abs=1e-12)


def _result(calls: list[Call], trial: int = 0, **replayed: int) -> CaseResult:
    from evals.cases import Case

    case = Case.model_validate({"id": "c", "input": "x", "reviewed": "2026-10-07"})
    transcript = Transcript(agent="research", case_id="c", trial=trial, models={})
    transcript.calls = calls
    transcript.replayed = dict(replayed)
    return CaseResult(case, transcript, [])


def _old_total(results: list[CaseResult]) -> float:
    prices = _old_models()
    return sum(
        _old_call_cost(prices[c.model], c.input_tokens, c.output_tokens, c.searches)
        for r in results
        for c in r.transcript.calls
    )


def test_usage_summary_prices_a_research_run_as_before() -> None:
    """A research-shaped run over every priced model, three trials."""
    models = sorted(_old_models())
    results = [
        _result(
            [
                Call(role, model, input_tokens=i, output_tokens=o, searches=s)
                for role, model, (i, o, s) in zip(
                    ("planner", "searcher", "critic", "writer", "verifier") * 4,
                    models * 4,
                    USAGES * 3,
                    strict=False,
                )
            ],
            trial=trial,
            searcher=trial,
        )
        for trial in range(3)
    ]
    summary: dict[str, Any] = usage_summary(results)
    old = _old_total(results)
    assert summary["cost_usd"] == round(old, 4)
    assert summary["cost_usd_per_run"] == round(old / 3, 4)
    assert summary["replayed"] == {"searcher": 0 + 1 + 2}


def test_usage_summary_pins_known_costs() -> None:
    """Numbers computed by the pre-move code, so a table edit is noticed too."""
    results = [
        _result(
            [
                Call("writer", "claude-sonnet-5", input_tokens=41_200, output_tokens=2_100),
                Call("searcher", "claude-haiku-5-5", 150_000, 3_000, searches=2),
                Call("critic", "claude-sonnet-4-5", input_tokens=10_000, output_tokens=500),
            ]
        )
    ]
    # writer 41200*2 + 2100*10 = 103 400 -> $0.1034
    # searcher long: 150000*0.5 + 3000*2.5 = 82 500 -> $0.0825, + 2 searches $0.02
    # critic 10000*3 + 500*15 = 37 500 -> $0.0375
    assert usage_summary(results)["cost_usd"] == pytest.approx(0.1034 + 0.1025 + 0.0375)


def test_one_unpriced_model_makes_the_total_unknown() -> None:
    results = [_result([Call("writer", "claude-sonnet-5", 10, 10), Call("x", "unpriced", 1, 1)])]
    summary = usage_summary(results)
    assert summary["cost_usd"] is None
    assert summary["cost_usd_per_run"] is None
