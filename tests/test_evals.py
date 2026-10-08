"""The eval harness, run offline on the scripted model.

These prove the harness works: datasets parse, the driver answers interrupts
and attributes model calls to roles, checks catch what they claim to, the
judge path and calibration routing behave, and the baseline gate fires on a
real drop but not on noise. They say nothing about agent quality; that is what
``python -m evals`` against a real model is for.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import build_settings

from evals import calibrate as calibrate_module
from evals import judge as judge_module
from evals.__main__ import main
from evals.calibrate import calibrate, load_examples
from evals.cases import Case, load_dataset
from evals.checks import CHECKS, run_checks
from evals.driver import Call, Transcript, run_case
from evals.judge import Judge, assign, load_rubric
from evals.runner import parse_overrides, resolved, self_judging
from evals.scorecard import fingerprint, regressions
from waypoint.agents.graphs import research

AGENTS = ("chat", "research")


def _case(**fields: Any) -> Case:
    return Case.model_validate({"id": "c", "input": "x", "reviewed": "2026-10-07", **fields})


# -- the files on disk -----------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_dataset_rubric_and_calibration_agree(agent: str) -> None:
    rubric = load_rubric(agent)
    for case in load_dataset(agent):
        assert set(case.checks) <= set(CHECKS), case.id
        assert set(case.criteria) <= set(rubric), case.id
    for example in load_examples(agent):
        assert example.criterion in rubric, example.id
        # Each calibrated criterion needs both outcomes, or agreement is trivial.
    labels: dict[str, set[bool]] = {}
    for example in load_examples(agent):
        labels.setdefault(example.criterion, set()).add(example.label >= judge_module.PASS)
    assert all(v == {True, False} for v in labels.values()), labels


# -- driver ------------------------------------------------------------------------


def test_research_case_runs_through_approval_and_attributes_calls() -> None:
    settings = build_settings()
    models = resolved(settings, "research", {"writer": "w-model"})
    transcript = asyncio.run(
        run_case(settings, "research", _case(input="tidal power"), models=models)
    )
    assert transcript.error is None
    assert transcript.interrupts[0]["kind"] == "approve_plan"
    assert transcript.values["report"].startswith("Scripted briefing")
    roles = {(c.role, c.model) for c in transcript.calls}
    assert ("writer", "w-model") in roles
    assert ("searcher", settings.model) in roles


def test_declined_case_stops_and_runs_no_search() -> None:
    settings = build_settings()
    case = _case(resume=[{"approved": False}], checks={"declined_without_search": True})
    transcript = asyncio.run(
        run_case(settings, "research", case, models=resolved(settings, "research", {}))
    )
    results = run_checks(transcript, case.checks, settings)
    assert all(r.passed for r in results), results


def test_chat_case_sees_its_seeded_memories_and_multi_turn() -> None:
    settings = build_settings()
    case = _case(input=["hello", "again"], seed=["is learning Rust"], checks={"recalled": "rust"})
    transcript = asyncio.run(
        run_case(settings, "chat", case, models=resolved(settings, "chat", {}))
    )
    assert [m["type"] for m in transcript.values["messages"]] == ["human", "ai", "human", "ai"]
    assert run_checks(transcript, case.checks, settings)[-1].passed


def test_agent_failure_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings(limits={"run_timeout_s": 0.001})
    transcript = asyncio.run(
        run_case(settings, "research", _case(), models=resolved(settings, "research", {}))
    )
    assert transcript.error is not None
    assert [r.passed for r in run_checks(transcript, {"plan_bounds": True}, settings)] == [
        False,
        False,
    ]


# -- checks --------------------------------------------------------------------------


def _briefing(body: str, listed: list[int]) -> Transcript:
    sources = "\n".join(f"[{n}] T - https://x/{n}" for n in listed)
    return Transcript(
        agent="research",
        case_id="c",
        trial=0,
        models={},
        values={"report": f"{body}\n\nSources\n{sources}" if listed else body, "findings": []},
    )


def test_citation_checks_catch_unlisted_ranges_and_unused_sources() -> None:
    settings = build_settings()
    good = _briefing("A [1]. B [2][1].", [1, 2])
    assert CHECKS["citations_resolve"](good, True, settings).passed
    assert CHECKS["sources_cited"](good, True, settings).passed

    bad = _briefing("A [1]. B [3]. C [1-2].", [1, 2])
    result = CHECKS["citations_resolve"](bad, True, settings)
    assert result.passed is False
    assert "[3] not listed" in result.detail
    assert "range" in result.detail
    unused = CHECKS["sources_cited"](_briefing("A [1].", [1, 2, 3, 4]), True, settings)
    assert unused.passed is False
    assert unused.detail == "1/4 sources cited"
    assert CHECKS["sources_cited"](_briefing("A [1][2].", [1, 2, 3]), 0.6, settings).passed
    assert (
        CHECKS["sources_cited"](_briefing("No citations.", [1, 2]), True, settings).passed is False
    )


def test_source_checks_do_not_apply_without_web_search() -> None:
    settings = build_settings(research={"web_search": False})
    assert CHECKS["sources_cited"](_briefing("A.", []), True, settings).passed is None


def test_chat_tool_checks() -> None:
    settings = build_settings()
    t = Transcript(
        agent="chat",
        case_id="c",
        trial=0,
        models={},
        values={
            "messages": [
                {"type": "ai", "tool_calls": [{"name": "remember", "args": {"fact": "Likes tea"}}]},
                {"type": "ai", "content": "Noted."},
            ]
        },
    )
    assert CHECKS["remembered"](t, "tea", settings).passed
    assert CHECKS["tool_called"](t, "now", settings).passed is False
    assert CHECKS["tool_not_called"](t, "remember", settings).passed is False
    assert CHECKS["no_tools"](t, True, settings).passed is False


# -- judge and calibration --------------------------------------------------------------


@pytest.fixture
def routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "routing.json"
    monkeypatch.setattr(judge_module, "ROUTING", path)
    monkeypatch.setattr(calibrate_module, "ROUTING", path)
    return path


def test_calibration_routes_a_failing_criterion_to_untrusted(routing: Path) -> None:
    # The scripted judge scores everything 3, so it agrees only with the pass
    # labels, which are at most half of each criterion's examples.
    settings = build_settings()
    rows = asyncio.run(calibrate(settings, "chat"))
    assert {r.criterion for r in rows} == set(load_rubric("chat"))
    assert all(r.agreement <= 0.5 for r in rows)
    saved = json.loads(routing.read_text())["chat"]
    assert saved["judge"] == settings.evals.judge_model
    assert saved["criteria"] == dict.fromkeys(load_rubric("chat"))
    assert assign(settings, "chat", "correct").status == "untrusted"

    case = _case(criteria=["correct"])
    transcript = Transcript(agent="chat", case_id="c", trial=0, models={}, values={})
    grades = asyncio.run(
        Judge(settings).grade_transcript("chat", transcript, case, load_rubric("chat"))
    )
    assert grades[0].score is None
    assert grades[0].status == "untrusted"


def test_calibration_passes_with_a_lower_threshold_and_goes_stale(routing: Path) -> None:
    settings = build_settings(evals={"min_agreement": 0.5})  # "correct": 2 of 4 labels pass
    asyncio.run(calibrate(settings, "chat"))
    assert assign(settings, "chat", "correct").status == "calibrated"
    # Measured for another judge model: no longer valid for this one.
    other = build_settings(evals={"min_agreement": 0.5, "judge_model": "another-judge"})
    assert assign(other, "chat", "correct").status == "uncalibrated"


def test_escalation_is_used_when_it_agrees(routing: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings(evals={"judge_escalation_model": "bigger"})

    async def measure(judge: Any, agent: str, examples: Any, model: str, criterion: str) -> Any:
        agreement = 1.0 if model == "bigger" else 0.5
        return calibrate_module.CriterionCalibration(criterion, model, 4, agreement, 0.0, [])

    monkeypatch.setattr(calibrate_module, "measure", measure)
    asyncio.run(calibrate(settings, "chat"))
    assert assign(settings, "chat", "correct") == judge_module.Assignment("bigger", "escalated")


def test_self_judging_is_flagged() -> None:
    settings = build_settings(evals={"judge_model": "same"})
    models = resolved(settings, "research", {"writer": "same"})
    warnings = self_judging(settings, "research", models)
    assert any(w.startswith("grounded") for w in warnings)
    assert not any(w.startswith("plan_quality") for w in warnings)


def test_overrides_parse_role_and_agent_scoped_forms() -> None:
    assert parse_overrides("research", ["critic=a", "research.writer=b", "chat.assistant=c"]) == {
        "critic": "a",
        "writer": "b",
    }
    with pytest.raises(ValueError, match="roles"):
        parse_overrides("research", ["assistant=a"])


# -- scorecard and gate ------------------------------------------------------------------


def _card(check_rate: float, criterion_mean: float, *, stdev: float = 0.0) -> dict[str, Any]:
    return {
        "checks": {"sources_cited": {"pass_rate": {"mean": check_rate, "stdev": stdev}}},
        "criteria": {"grounded": {"mean": {"mean": criterion_mean, "stdev": stdev}}},
    }


def test_gate_fires_on_a_real_drop_but_not_on_noise() -> None:
    assert regressions(_card(0.9, 3.5), _card(0.85, 3.4)) == []
    found = regressions(_card(0.9, 3.5), _card(0.4, 2.6))
    assert {r.metric for r in found} == {
        "check sources_cited pass rate",
        "criterion grounded mean",
    }
    # A noisy baseline widens the tolerance.
    assert regressions(_card(0.9, 3.5, stdev=0.3), _card(0.5, 3.0)) == []


def test_a_prompt_change_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings()
    models = resolved(settings, "research", {})
    before = fingerprint(settings, "research", models)
    monkeypatch.setattr(
        research, "SYNTH", research.SYNTH.replace("Cite sources inline", "Do not cite")
    )
    assert fingerprint(settings, "research", models) != before
    assert fingerprint(settings, "research", {**models, "critic": "x"}) != before


def test_usage_is_summed_per_role_with_cost_when_priced() -> None:
    from evals.scorecard import CaseResult, usage_summary

    transcript = Transcript(agent="research", case_id="c", trial=0, models={})
    transcript.calls = [
        Call("writer", "claude-haiku-4-5", input_tokens=1_000_000, output_tokens=0),
        Call("critic", "unpriced-model", input_tokens=10, output_tokens=10),
    ]
    summary = usage_summary([CaseResult(_case(), transcript, [])])
    assert summary["roles"]["writer"]["input_tokens"] == 1_000_000
    assert summary["cost_usd"] is None  # one unpriced model makes the total unknown


# -- the CLI ---------------------------------------------------------------------------------


def test_cli_run_accept_and_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no developer .env
    monkeypatch.setattr("evals.scorecard.BASELINES", tmp_path / "baselines")
    out = tmp_path / "results"
    argv = ["run", "--fake", "--agent", "chat", "--case", "arithmetic", "--out", str(out)]
    assert main(argv) == 0
    card = next(out.glob("chat/*/scorecard.json"))
    assert json.loads(card.read_text())["checks"]["completed"]["passed"] == 1
    assert main(["accept", "--agent", "chat", str(card)]) == 0
    assert main([*argv, "--against-baseline"]) == 0


def test_judge_knows_the_date_and_prompt_changes_invalidate_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt

    assert dt.date.today().isoformat() in judge_module.system_prompt()
    assert "not evidence of fabrication" in judge_module.system_prompt()
    before = judge_module.rubric_hash("research")
    monkeypatch.setattr(judge_module, "SYSTEM", judge_module.SYSTEM + " Extra rule.")
    assert judge_module.rubric_hash("research") != before
