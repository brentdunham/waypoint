"""The research agent: structured plan, cited findings, a bounded critic loop.

Runs on ScriptedChatModel. Its worker replies are shaped like real web-search
replies (content blocks with citations), and the critic is steered per test by
swapping its entry in ``ScriptedChatModel.structured_scripts``.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from langchain_core.messages import AIMessage
from tests.conftest import new_thread, run_and_wait

from waypoint.agents.citations import answer_and_sources, number_sources
from waypoint.agents.models import FINDINGS_MARKER, ScriptedChatModel

type Script = Callable[[str], dict[str, Any]]


def _findings_in(prompt: str) -> list[dict[str, Any]]:
    return list(json.loads(prompt.rsplit(FINDINGS_MARKER, 1)[-1].strip()))


def _critic(*, follow_ups: list[str], confidence: float = 0.9) -> Script:
    def script(prompt: str) -> dict[str, Any]:
        return {
            "assessments": [
                {"question": f["question"], "confidence": confidence, "issue": ""}
                for f in _findings_in(prompt)
            ],
            "follow_ups": follow_ups,
        }

    return script


def _research(client: FlaskClient, topic: str = "tidal power") -> dict[str, Any]:
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": topic})
    return run_and_wait(client, thread_id, {"resume": {"approved": True}})


# -- citations ---------------------------------------------------------------


def test_sources_come_from_citation_metadata_not_prose() -> None:
    message = AIMessage(
        content=[
            {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}},
            {
                "type": "web_search_tool_result",
                "tool_use_id": "s1",
                "content": [{"type": "web_search_result", "url": "https://consulted.example"}],
            },
            {"type": "text", "text": "Tidal output is predictable "},
            {
                "type": "text",
                "text": "and grew 4% in 2025.",
                "citations": [
                    {"url": "https://a.example", "title": "A"},
                    {"url": "https://b.example", "title": ""},
                ],
            },
            {"type": "text", "text": " See https://invented.example.", "citations": None},
            {"type": "text", "text": "", "citations": [{"url": "https://a.example", "title": "A"}]},
        ]
    )
    answer, sources = answer_and_sources(message)
    assert (
        answer == "Tidal output is predictable and grew 4% in 2025. See https://invented.example."
    )
    # Cited only, deduplicated, in order; a consulted-but-uncited result and a
    # URL typed into the prose are not sources. Missing title falls back to URL.
    assert sources == [
        {"url": "https://a.example", "title": "A"},
        {"url": "https://b.example", "title": "https://b.example"},
    ]


def test_plain_string_reply_has_no_sources() -> None:
    assert answer_and_sources(AIMessage(content=" just text ")) == ("just text", [])


def test_sources_are_numbered_once_across_findings() -> None:
    a, b, c = ({"url": f"https://{x}.example", "title": x} for x in "abc")
    findings: Any = [{"sources": [a, b]}, {"sources": [b, c]}]
    assert number_sources(findings) == {a["url"]: 1, b["url"]: 2, c["url"]: 3}


# -- graph -------------------------------------------------------------------


def test_plan_comes_with_a_rationale_per_question(client: FlaskClient) -> None:
    thread_id = new_thread(client, "research")
    result = run_and_wait(client, thread_id, {"message": "tidal power"})
    interrupt = result["interrupts"][0]["value"]
    assert len(interrupt["rationale"]) == len(interrupt["plan"]) == 3
    assert all(interrupt["rationale"])


def test_findings_are_cited_scored_and_listed_in_the_report(client: FlaskClient) -> None:
    result = _research(client)["result"]
    findings = result["findings"]
    assert findings
    assert all(f["sources"] for f in findings)
    assert all(f["confidence"] == 0.8 for f in findings)  # the scripted critic's score
    assert result["report"].endswith(
        "\n".join(
            f"[{i}] {f['sources'][0]['title']} - {f['sources'][0]['url']}"
            for i, f in enumerate(findings, start=1)
        )
    )
    assert "\n\nSources\n[1] " in result["report"]
    assert result["metadata"]["rounds"] == 1
    assert result["metadata"]["source_count"] == len(findings)


def test_critic_follow_ups_run_one_more_wave_then_stop(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Asks every round, including a duplicate of a planned question. Only the
    # new question may run, and only once: round 2 is the last (max_rounds=2).
    monkeypatch.setitem(
        ScriptedChatModel.structured_scripts,
        "Critique",
        _critic(follow_ups=["What is tidal power", "Who builds tidal turbines?"]),
    )
    result = _research(client)["result"]
    questions = [f["question"] for f in result["findings"]]
    assert questions[-1] == "Who builds tidal turbines?"
    assert len(questions) == 4
    assert questions.count("Who builds tidal turbines?") == 1
    assert [f["round"] for f in result["findings"]] == [1, 1, 1, 2]
    assert all(f["confidence"] == 0.9 for f in result["findings"])
    assert result["metadata"]["rounds"] == 2


def test_one_round_means_no_follow_ups(
    make_app: Callable[..., Flask], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        ScriptedChatModel.structured_scripts, "Critique", _critic(follow_ups=["Anything else?"])
    )
    client = make_app(research={"max_rounds": 1}).test_client()
    findings = _research(client)["result"]["findings"]
    assert len(findings) == 3


def test_uncited_findings_are_capped_whatever_the_critic_says(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from waypoint.agents.graphs import research

    monkeypatch.setitem(
        ScriptedChatModel.structured_scripts, "Critique", _critic(follow_ups=[], confidence=0.95)
    )
    monkeypatch.setattr(research, "answer_and_sources", lambda m: ("unsourced claim", []))
    result = _research(client)["result"]
    assert all(f["confidence"] == research.UNCITED_CAP for f in result["findings"])
    assert "Sources\n" not in result["report"]


def test_critic_scoring_an_unknown_question_falls_back(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def critic(prompt: str) -> dict[str, Any]:
        return {
            "assessments": [{"question": "something else", "confidence": 1.0, "issue": ""}],
            "follow_ups": [],
        }

    monkeypatch.setitem(ScriptedChatModel.structured_scripts, "Critique", critic)
    findings = _research(client)["result"]["findings"]
    assert all(f["confidence"] == 0.5 for f in findings)


def test_rejected_web_search_fails_with_the_fix_in_the_message(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BadRequestError(Exception):  # same name as the anthropic SDK's
        pass

    original = ScriptedChatModel._next

    def refuse(self: ScriptedChatModel, messages: list[Any]) -> AIMessage:
        if str(messages[-1].content).startswith("Answer this research question"):
            raise BadRequestError("web search is not enabled")
        return original(self, messages)

    monkeypatch.setattr(ScriptedChatModel, "_next", refuse)
    result = _research(client)
    assert result["status"] == "failed"
    assert "RESEARCH__WEB_SEARCH=false" in result["error"]


# -- malformed structured output -----------------------------------------------


def test_a_malformed_plan_is_retried_not_fatal(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by the live evals: a list sent as a JSON string failed the whole run,
    because LangGraph's default retry policy never retries a ValueError."""
    good = ScriptedChatModel.structured_scripts["ResearchPlan"]
    calls: list[int] = []

    def flaky(prompt: str) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            return {"questions": json.dumps(good(prompt)["questions"])}
        return good(prompt)

    monkeypatch.setitem(ScriptedChatModel.structured_scripts, "ResearchPlan", flaky)
    thread_id = new_thread(client, "research")
    paused = run_and_wait(client, thread_id, {"message": "tidal power"})
    assert paused["status"] == "interrupted"
    assert len(calls) == 2


# -- the date ----------------------------------------------------------------------


def test_every_model_call_is_told_the_date(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it the agent called its own recent search results fabricated."""
    import datetime as dt

    seen: list[str] = []
    for name in ("ResearchPlan", "Critique"):
        original = ScriptedChatModel.structured_scripts[name]

        def spy(prompt: str, _original: Any = original) -> dict[str, Any]:
            seen.append(prompt)
            return dict(_original(prompt))

        monkeypatch.setitem(ScriptedChatModel.structured_scripts, name, spy)
    original_next = ScriptedChatModel._next

    def next_spy(self: ScriptedChatModel, messages: Any) -> Any:
        seen.append(str(messages[-1].content))
        return original_next(self, messages)

    monkeypatch.setattr(ScriptedChatModel, "_next", next_spy)
    _research(client)
    today = f"Today is {dt.date.today().isoformat()}."
    kinds = {p.split("\n", 1)[0][:30] for p in seen}
    assert len(kinds) >= 4  # planner, worker, critic, writer
    assert all(today in p for p in seen), [p[:80] for p in seen if today not in p]
