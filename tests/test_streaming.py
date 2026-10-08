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
