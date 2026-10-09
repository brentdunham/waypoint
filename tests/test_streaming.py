"""Which tokens reach the client.

The UI renders the token stream as the assistant's reply. A research run makes
several model calls (planner, one per worker, synthesizer); only the briefing is
a reply. The rest must not stream, or the reply bubble fills with the plan and
every worker's answer interleaved.
"""

import json

from starlette.testclient import TestClient
from tests.conftest import new_thread, run_and_wait


def _token_nodes(client: TestClient, events_url: str) -> list[str]:
    nodes: list[str] = []
    event = ""
    with client.stream("GET", events_url) as response:
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: ") and event == "token":
                nodes.append(json.loads(line.removeprefix("data: "))["node"])
            if event.startswith("run.") and event != "run.started" and not line:
                break
    return nodes


def test_research_streams_only_the_briefing(client: TestClient) -> None:
    thread_id = new_thread(client, "research")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "tidal power"})
    assert _token_nodes(client, run.json()["events_url"]) == []  # planner is quiet

    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"resume": {"approved": True}})
    nodes = _token_nodes(client, run.json()["events_url"])
    assert nodes, "the briefing should still stream"
    assert set(nodes) == {"synthesize"}


def test_chat_streams_the_reply(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "one two three"})
    assert set(_token_nodes(client, run.json()["events_url"])) == {"model"}


def test_briefing_is_in_the_transcript_once(client: TestClient) -> None:
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "tidal power"})
    run_and_wait(client, thread_id, {"resume": {"approved": True}})
    messages = client.get(f"/api/v1/threads/{thread_id}/state").json()["values"]["messages"]
    ai = [m for m in messages if m["type"] == "ai"]
    assert len(ai) == 1


def _progress(client: TestClient, events_url: str) -> list[dict[str, object]]:
    """The research panel's ``custom`` events, in order."""
    out: list[dict[str, object]] = []
    event = ""
    with client.stream("GET", events_url) as response:
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: ") and event == "custom":
                out.append(json.loads(line.removeprefix("data: "))["payload"])
            if event.startswith("run.") and event != "run.started" and not line:
                break
    return out


def test_research_reports_what_it_is_starting(client: TestClient) -> None:
    """Node updates arrive only when a node ends; these say what just began."""
    thread_id = new_thread(client, "research")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "tidal power"})
    assert _progress(client, run.json()["events_url"]) == [
        {"kind": "research", "phase": "planning"}
    ]

    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"resume": {"approved": True}})
    events = _progress(client, run.json()["events_url"])
    phases = [e["phase"] for e in events]
    assert phases == ["searching"] * 3 + ["reviewing", "writing", "verifying"]
    searching = [e for e in events if e["phase"] == "searching"]
    assert all(e["round"] == 1 and str(e["question"]).endswith("?") for e in searching)
    assert events[3]["round"] == 1
