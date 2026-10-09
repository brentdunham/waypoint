"""The citation verifier: the step after the writer.

Wrong or missing citations are fixed in code from the verifier's verdict; only
sentences no finding supports go back to the writer. The splitter and
``cite`` are tested directly, the node through a research run with the
scripted writer and verifier swapped per test.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from starlette.testclient import TestClient
from tests.conftest import new_thread, run_and_wait

from waypoint.agents.citations import cite, split_sentences
from waypoint.agents.graphs import research
from waypoint.agents.graphs.research import SENTENCES_MARKER
from waypoint.agents.models import ScriptedChatModel

# -- sentences and citation marks -------------------------------------------------


def test_trailing_citations_stay_with_their_sentence() -> None:
    assert split_sentences(
        "Tides are regular [1]. Output peaks twice a day. [2][3] Costs fell."
    ) == [
        "Tides are regular [1].",
        "Output peaks twice a day. [2][3]",
        "Costs fell.",
    ]


def test_abbreviations_and_decimals_do_not_split() -> None:
    assert split_sentences("Capacity rose 2.5 GW in 2025.") == ["Capacity rose 2.5 GW in 2025."]


def test_cite_replaces_marks_and_keeps_final_punctuation() -> None:
    assert cite("Output peaks twice a day [4].", [2, 3]) == "Output peaks twice a day [2][3]."
    assert cite("Output peaks twice a day. [4]", [2]) == "Output peaks twice a day [2]."
    assert cite("No mark here", [1]) == "No mark here [1]"


# -- the node --------------------------------------------------------------------

BRIEFING = (
    "Tidal output is predictable years ahead [2]. "
    "Most sites need a strong current. "
    "Tidal power will be cheaper than solar by 2030 [1].\n"
    "Second paragraph stays as it is [1]."
)


def _sentences(prompt: str) -> list[dict[str, Any]]:
    return list(json.loads(prompt.rsplit(SENTENCES_MARKER, 1)[-1].strip()))


def _verdicts(by_index: dict[int, tuple[str, list[int]]]) -> Callable[[str], dict[str, Any]]:
    def script(prompt: str) -> dict[str, Any]:
        indices = {s["index"] for s in _sentences(prompt)}
        return {
            "issues": [
                {"index": i, "verdict": verdict, "sources": sources, "note": "n"}
                for i, (verdict, sources) in by_index.items()
                if i in indices
            ]
        }

    return script


@dataclass
class Writer:
    rewrites: list[dict[str, Any]] = field(default_factory=list)
    """What the next revision request returns; a test fills it."""
    asked: list[str] = field(default_factory=list)
    """Revision prompts the scripted writer saw."""


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> Writer:
    """The scripted writer drafts ``BRIEFING`` and answers revision requests."""
    state = Writer()
    original = ScriptedChatModel._next

    def scripted(self: ScriptedChatModel, messages: list[BaseMessage]) -> AIMessage:
        text = str(messages[-1].content) if messages else ""
        if text.startswith("Write a short briefing"):
            return AIMessage(content=BRIEFING)
        return original(self, messages)

    def revision(prompt: str) -> dict[str, Any]:
        state.asked.append(prompt)
        return {"rewrites": state.rewrites}

    monkeypatch.setattr(ScriptedChatModel, "_next", scripted)
    monkeypatch.setitem(ScriptedChatModel.structured_scripts, "Revision", revision)
    return state


def _research(client: TestClient) -> dict[str, Any]:
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "tidal power"})
    result: dict[str, Any] = run_and_wait(client, thread_id, {"resume": {"approved": True}})[
        "result"
    ]
    return result


def _body(report: str) -> str:
    return report.partition("\n\nSources\n")[0]


def test_supported_briefing_is_unchanged(client: TestClient, writer: Writer) -> None:
    result = _research(client)
    assert _body(result["report"]) == BRIEFING
    assert result["metadata"]["verification"] == {
        "sentences": 4,
        "recited": 0,
        "unsupported": 0,
        "revised": 0,
    }


def test_wrong_and_missing_citations_are_fixed_without_the_writer(
    client: TestClient, writer: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _verdicts(
        {0: ("wrong_source", [3]), 1: ("needs_citation", [1, 2]), 3: ("wrong_source", [99])}
    )
    monkeypatch.setitem(ScriptedChatModel.structured_scripts, "Verification", script)
    result = _research(client)
    assert _body(result["report"]) == (
        "Tidal output is predictable years ahead [3]. "
        "Most sites need a strong current [1][2]. "
        "Tidal power will be cheaper than solar by 2030 [1].\n"
        "Second paragraph stays as it is [1]."  # [99] is not a source: left alone
    )
    assert result["report"].count("\n\nSources\n") == 1
    assert result["metadata"]["verification"]["recited"] == 2
    assert not writer.asked  # no revision was asked for


def test_unsupported_sentences_are_rewritten_one_by_one(
    client: TestClient, writer: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        ScriptedChatModel.structured_scripts,
        "Verification",
        _verdicts({1: ("unsupported", []), 2: ("unsupported", [])}),
    )
    writer.rewrites.extend(
        [
            {"index": 1, "text": ""},  # deleted
            {"index": 2, "text": "Costs are falling, by how much is disputed [3]."},
            {"index": 0, "text": "Not flagged, so not applied [1]."},
        ]
    )
    result = _research(client)
    assert _body(result["report"]) == (
        "Tidal output is predictable years ahead [2]. "
        "Costs are falling, by how much is disputed [3].\n"
        "Second paragraph stays as it is [1]."
    )
    assert '"index": 2' in writer.asked[0]
    assert "Tidal power will be cheaper" in writer.asked[0]
    verification = result["metadata"]["verification"]
    assert verification["unsupported"] == 2
    assert verification["revised"] == 2
    assert result["metadata"]["finding_count"]  # the writer's metadata is kept


def test_a_rewrite_citing_a_missing_source_is_discarded(
    client: TestClient, writer: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        ScriptedChatModel.structured_scripts, "Verification", _verdicts({2: ("unsupported", [])})
    )
    writer.rewrites.append({"index": 2, "text": "Tidal power is cheap [42]."})
    result = _research(client)
    assert _body(result["report"]) == BRIEFING
    assert result["metadata"]["verification"]["revised"] == 0


def test_the_briefing_is_replaced_not_repeated(client: TestClient, writer: Writer) -> None:
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "tidal power"})
    run_and_wait(client, thread_id, {"resume": {"approved": True}})
    state = client.get(f"/api/v1/threads/{thread_id}/state").json()
    replies = [m for m in state["values"]["messages"] if m["type"] == "ai"]
    assert sum(BRIEFING.split("\n", maxsplit=1)[0][:20] in str(m["content"]) for m in replies) == 1


def test_unparseable_verifier_output_ships_the_draft(
    client: TestClient, writer: Writer, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def broken(prompt: str) -> dict[str, Any]:
        calls.append(prompt)
        return {"issues": "not a list"}

    monkeypatch.setitem(ScriptedChatModel.structured_scripts, "Verification", broken)
    result = _research(client)
    assert _body(result["report"]) == BRIEFING
    assert result["metadata"]["verification"] == {"sentences": 4, "skipped": "unparseable output"}
    assert len(calls) == research.VERIFY_ATTEMPTS
