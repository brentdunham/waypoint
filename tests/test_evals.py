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
from evals.scorecard import fingerprint, regressions, render_markdown
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


def test_summary_says_when_the_baseline_measured_a_different_configuration() -> None:
    card = {
        "agent": "research", "date": "2026-10-12T10:00:00+00:00", "fingerprint": "new",
        "git_sha": "abc1234", "cases": 1, "trials": 1, "errors": 0, "judge_model": "j",
        "models": {"writer": "m"}, "checks": {}, "criteria": {},
        "usage": {"roles": {}, "cost_usd_per_run": None},
    }  # fmt: skip
    same = render_markdown(card, [], {**card, "fingerprint": "new"})
    assert "fingerprint `new`" in same
    assert "differs" not in same
    other = render_markdown(card, [], {**card, "fingerprint": "old"})
    assert "Configuration differs from the baseline" in other
    assert "git `abc1234`" in other


def test_a_prompt_change_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build_settings()
    models = resolved(settings, "research", {})
    before = fingerprint(settings, "research", models)
    monkeypatch.setattr(
        research, "SYNTH", research.SYNTH.replace("Cite sources inline", "Do not cite")
    )
    assert fingerprint(settings, "research", models) != before
    assert fingerprint(settings, "research", {**models, "critic": "x"}) != before


def test_service_only_settings_do_not_change_the_fingerprint() -> None:
    """Seeding the service's docs at boot cannot change an eval's score."""
    base = build_settings()
    models = resolved(base, "research", {})
    seeding = base.rag.model_copy(update={"seed_dir": "docs", "seed_corpus": "waypoint-docs"})
    upload = base.rag.model_copy(update={"max_upload_mb": 99})
    for rag in (seeding, upload):
        assert fingerprint(base.model_copy(update={"rag": rag}), "research", models) == fingerprint(
            base, "research", models
        )
    top_k = base.rag.model_copy(update={"top_k": base.rag.top_k + 1})
    assert fingerprint(base.model_copy(update={"rag": top_k}), "research", models) != fingerprint(
        base, "research", models
    )


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
    assert main(["accept", "--agent", "chat", str(card)]) == 2  # one trial: refused
    assert main(["accept", "--agent", "chat", "--min-trials", "1", str(card)]) == 0
    assert main([*argv, "--against-baseline"]) == 0
    assert not (out / "replay.sqlite").exists()  # the scripted model is never recorded


def test_recheck_rescores_saved_transcripts_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "results"
    assert (
        main(["run", "--fake", "--agent", "chat", "--case", "arithmetic", "--out", str(out)]) == 0
    )
    path = next(out.glob("chat/*/scorecard.json"))
    original = json.loads(path.read_text())
    tampered = {
        **original,
        "checks": {},
        "runs": [{**r, "failed_checks": {"x": "y"}} for r in original["runs"]],
    }
    path.write_text(json.dumps(tampered))

    assert main(["recheck", str(path.parent)]) == 0
    card = json.loads(path.read_text())
    assert card["checks"] == original["checks"]
    assert [r["failed_checks"] for r in card["runs"]] == [
        r["failed_checks"] for r in original["runs"]
    ]
    assert card["fingerprint"] == original["fingerprint"]
    assert card["usage"] == original["usage"]
    assert "rechecked" in card
    assert "rechecked" not in original


def test_a_transcript_survives_its_json_round_trip() -> None:
    transcript = Transcript(agent="research", case_id="c", trial=2, models={"writer": "m"})
    transcript.values = {"report": "x"}
    transcript.calls = [Call("writer", "m", input_tokens=5, output_tokens=6, searches=1)]
    transcript.replayed = {"planner": 1}
    transcript.replayed_calls = [Call("planner", "m", input_tokens=3)]
    transcript.error = "boom"
    again = Transcript.from_dict(json.loads(json.dumps(transcript.to_dict())))
    assert again.to_dict() == transcript.to_dict()


def test_judge_knows_the_date_and_prompt_changes_invalidate_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt

    assert dt.date.today().isoformat() in judge_module.system_prompt()
    assert "not evidence of fabrication" in judge_module.system_prompt()
    before = judge_module.rubric_hash("research")
    monkeypatch.setattr(judge_module, "SYSTEM", judge_module.SYSTEM + " Extra rule.")
    assert judge_module.rubric_hash("research") != before


# -- replay ------------------------------------------------------------------------


def _research_case() -> Case:
    return _case(input="tidal power", checks={"completed": True})


def _run(settings: Any, store: Any, *, trial: int = 0, fresh: bool = False) -> Transcript:
    from evals.replay import Replay

    session = Replay(store, "c", trial, read=not fresh)
    models = resolved(settings, "research", {})
    return asyncio.run(
        run_case(settings, "research", _research_case(), models=models, trial=trial, replay=session)
    )


def test_replay_answers_a_repeat_run_from_the_store(tmp_path: Path) -> None:
    from evals.replay import ReplayStore

    settings = build_settings()
    store = ReplayStore(tmp_path / "replay.sqlite")
    first = _run(settings, store)
    assert first.error is None
    assert first.replayed == {}
    second = _run(settings, store)
    assert second.error is None
    assert set(second.replayed) == {"planner", "searcher", "critic", "writer", "verifier"}
    assert second.values["report"] == first.values["report"]
    assert second.values["findings"] == first.values["findings"]


def test_replay_reruns_only_what_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evals.replay import ReplayStore

    settings = build_settings()
    store = ReplayStore(tmp_path / "replay.sqlite")
    _run(settings, store)
    monkeypatch.setattr(research, "SYNTH", research.SYNTH.replace("No filler.", "Be brief."))
    changed = _run(settings, store)
    # The writer ran live. The scripted writer's text does not depend on its
    # prompt, so the verifier saw the same briefing and replayed.
    assert set(changed.replayed) == {"planner", "searcher", "critic", "verifier"}


def test_replay_keeps_trials_apart_and_fresh_does_not_read(tmp_path: Path) -> None:
    from evals.replay import ReplayStore

    settings = build_settings()
    store = ReplayStore(tmp_path / "replay.sqlite")
    _run(settings, store)
    assert _run(settings, store, trial=1).replayed == {}
    assert _run(settings, store, fresh=True).replayed == {}


def test_replayed_calls_keep_what_they_cost_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run's cost if live: billed calls plus replayed ones at their recorded usage."""
    from evals.replay import ReplayStore
    from evals.scorecard import CaseResult, usage_summary
    from waypoint.agents.models import ScriptedChatModel

    monkeypatch.setattr(
        ScriptedChatModel,
        "usage_script",
        staticmethod(lambda _t: {"input_tokens": 1_000, "output_tokens": 100, "searches": 1}),
    )
    settings = build_settings(model="claude-haiku-5-5")
    store = ReplayStore(tmp_path / "replay.sqlite")
    live = usage_summary([CaseResult(_research_case(), _run(settings, store), [])])
    again = usage_summary([CaseResult(_research_case(), _run(settings, store), [])])

    assert live["cost_usd_per_run"] > 0
    assert again["cost_usd_per_run"] == 0  # everything replayed: nothing billed
    assert again["cost_usd_if_live_per_run"] == live["cost_usd_per_run"]
    # The scripted model answers structured output without a chat model call,
    # so those replays have no usage to recall: the figure says it is a floor.
    assert set(again["replayed_without_usage"]) <= {"planner", "critic", "verifier"}
    assert "searcher" not in again["replayed_without_usage"]


def test_a_store_from_before_usage_was_recorded_still_opens(tmp_path: Path) -> None:
    import sqlite3

    from evals.replay import ReplayStore

    path = tmp_path / "replay.sqlite"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE calls (key TEXT PRIMARY KEY, role TEXT, model TEXT, value TEXT, recorded TEXT)"
    )
    old.execute("INSERT INTO calls VALUES ('k', 'writer', 'm', '{}', 'then')")
    old.commit()
    old.close()
    store = ReplayStore(path)
    assert store.get("k") == ("{}", None)
    store.put("k2", "writer", "m", "{}", '{"input_tokens": 1}')
    assert store.get("k2") == ("{}", '{"input_tokens": 1}')


def test_replay_key_sees_max_uses_and_masks_the_date() -> None:
    import datetime as dt

    from langchain_core.messages import HumanMessage

    from evals.replay import _input_key, _signature
    from waypoint.agents.models import build_model

    settings = build_settings(fake_model=False, anthropic_api_key="sk-test-not-sent")
    model = build_model(settings, model="claude-haiku-5-5")

    def searcher(max_uses: int) -> str:
        tool = {"type": research.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}
        return _signature(model.bind_tools([tool]))

    assert searcher(2) != searcher(3)
    assert "sk-test-not-sent" not in searcher(2)
    today = HumanMessage(f"Today is {dt.date.today().isoformat()}.", id="a")
    other_day = HumanMessage("Today is 2020-01-01.", id="b")
    assert "<today>" in _input_key([today])
    assert _input_key([today]) == _input_key([HumanMessage(today.content, id="c")])
    assert _input_key([today]) != _input_key([other_day])


def test_accept_needs_three_trials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evals.scorecard import AcceptError, accept

    monkeypatch.setattr("evals.scorecard.BASELINES", tmp_path / "baselines")
    card = tmp_path / "scorecard.json"
    card.write_text(json.dumps({"agent": "research", "trials": 1}))
    with pytest.raises(AcceptError, match="3"):
        accept("research", card)
    card.write_text(json.dumps({"agent": "research", "trials": 3}))
    assert accept("research", card).exists()


# -- uncited search answers ----------------------------------------------------------


def _uncited_reply(question: str) -> Any:
    from langchain_core.messages import AIMessage

    results = [
        {"type": "web_search_result", "url": f"https://example.com/r{n}", "title": f"Result {n}"}
        for n in range(8)
    ]
    return AIMessage(
        content=[
            {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}},
            {"type": "web_search_tool_result", "tool_use_id": "s1", "content": results},
            {"type": "text", "text": f"Uncited answer to: {question}"},
        ]
    )


def test_an_uncited_search_answer_falls_back_to_its_results_and_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from waypoint.agents.models import ScriptedChatModel

    original = ScriptedChatModel._next

    def next_(self: ScriptedChatModel, messages: list[Any]) -> Any:
        text = str(messages[-1].content) if messages else ""
        if text.startswith("Answer this research question"):
            return _uncited_reply(text.rsplit("Question:", 1)[-1].strip())
        return original(self, messages)

    monkeypatch.setattr(ScriptedChatModel, "_next", next_)
    settings = build_settings()
    case = _case(input="tidal power", checks={"findings_sourced": True, "findings_cited": True})
    transcript = asyncio.run(
        run_case(settings, "research", case, models=resolved(settings, "research", {}))
    )
    assert transcript.error is None
    findings = transcript.values["findings"]
    assert findings
    for finding in findings:
        assert finding["cited"] is False
        assert len(finding["sources"]) == research.RETRIEVED_SOURCES
        assert finding["confidence"] <= research.UNCITED_CAP
    results = {r.name: r.passed for r in run_checks(transcript, case.checks, settings)}
    assert results["findings_sourced"] is True
    assert results["findings_cited"] is False


def test_retrieved_sources_skip_errors_and_duplicates() -> None:
    from langchain_core.messages import AIMessage

    from waypoint.agents.citations import retrieved_sources

    reply = AIMessage(
        content=[
            {"type": "web_search_tool_result", "content": {"type": "error", "error_code": "x"}},
            {"type": "web_search_tool_result", "content": [
                {"url": "https://a.example", "title": "A"},
                {"url": "https://a.example", "title": "A again"},
                {"url": "https://b.example"},
            ]},
        ]
    )  # fmt: skip
    assert retrieved_sources(reply, 5) == [
        {"url": "https://a.example", "title": "A"},
        {"url": "https://b.example", "title": "https://b.example"},
    ]
    assert len(retrieved_sources(reply, 1)) == 1
    assert retrieved_sources(AIMessage(content="plain"), 5) == []


def test_claims_cited_counts_claim_sentences_and_moves_trailing_citations_back() -> None:
    from evals.checks import claim_sentences

    body = (
        "Sodium-ion cells cost about the same as LFP cells in 2025 [1]. "
        "Projections put them ten to twenty percent lower at scale. [2][3]\n"
        "Energy density is lower, at roughly 150 Wh per kilogram for current cells.\n"
        "In short: it depends."
    )
    sentences = claim_sentences(body)
    assert len(sentences) == 3  # "In short: it depends." is too short to count
    assert sentences[1].endswith("[2][3]")
    transcript = Transcript(agent="research", case_id="c", trial=0, models={})
    transcript.values = {"report": body + "\n\nSources\n[1] A - u\n[2] B - u\n[3] C - u"}
    result = CHECKS["claims_cited"](transcript, True, build_settings())
    assert result.passed is False
    assert result.detail == "2/3 claim sentences cited"
    assert CHECKS["claims_cited"](transcript, 0.6, build_settings()).passed is True


def test_claims_cited_leaves_out_uncited_statements_about_the_research() -> None:
    from evals.checks import about_the_research

    gaps = [
        "No field data below minus twenty degrees was found in any source.",
        "Electrode corrosion's role could not be assessed from the available evidence.",
        "Confidence here is low because the studies are small and mostly vendor funded.",
        "No source directly compares the older and newer units in the field (confidence 0.5).",
    ]
    claims = [
        "Sodium-ion cells cost about the same as LFP cells in 2025 at pack level.",
        "Field trials found heat pumps kept eighty percent of capacity at minus fifteen.",
        "Sodium-ion does not need lithium, which shields it from lithium price swings.",
    ]
    assert all(about_the_research(x) for x in gaps)
    assert not any(about_the_research(x) for x in claims)

    body = " ".join([*(f"{x[:-1]} [1]." for x in claims), *gaps])
    transcript = Transcript(agent="research", case_id="c", trial=0, models={})
    transcript.values = {"report": body + "\n\nSources\n[1] A - u"}
    result = CHECKS["claims_cited"](transcript, True, build_settings())
    assert result.passed is True
    assert result.detail == "3/3 claim sentences cited, 4 uncited about the research not counted"


def test_a_cited_statement_about_the_research_still_counts() -> None:
    body = (
        "The meta-analysis is genuine and verifiable, though it does not settle task type [1].\n"
        "Remote workers reported higher output in three of the five large field studies."
    )
    transcript = Transcript(agent="research", case_id="c", trial=0, models={})
    transcript.values = {"report": body + "\n\nSources\n[1] A - u"}
    result = CHECKS["claims_cited"](transcript, True, build_settings())
    assert result.detail == "1/2 claim sentences cited"


def test_judge_sees_citation_numbers_quotes_and_retrieved_only_sources() -> None:
    from evals.judge import material_for

    a = {"url": "https://a", "title": "A", "quotes": ["sodium is abundant"]}
    b = {"url": "https://b", "title": "B"}
    transcript = Transcript(agent="research", case_id="c", trial=0, models={})
    transcript.values = {
        "topic": "t",
        "report": "r",
        "findings": [
            {"question": "q1", "answer": "x", "sources": [a], "confidence": 0.8, "issue": ""},
            {"question": "q2", "answer": "y", "sources": [b, a], "confidence": 0.3, "issue": "",
             "cited": False},
        ],
    }  # fmt: skip
    findings = material_for("briefing", transcript, _case())["findings"]
    assert findings[0]["sources"] == [{"n": 1, "title": "A", "quotes": ["sodium is abundant"]}]
    assert findings[1]["sources"] == [
        {"n": 2, "title": "B", "retrieved_only": True},
        {"n": 1, "title": "A", "quotes": ["sodium is abundant"], "retrieved_only": True},
    ]
