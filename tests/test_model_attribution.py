"""Every agent output says which model produced it, and keeps saying so after a reload."""

from langchain_core.messages import AIMessage
from starlette.testclient import TestClient
from tests.conftest import new_thread, read_sse, run_and_wait

from waypoint.agents.graphs.research import ROLES
from waypoint.agents.models import model_of, stamp_model
from waypoint.serialization import message_to_dict

RESEARCH = dict.fromkeys(ROLES, "base") | {"searcher": "small", "writer": "big"}


def test_reported_model_wins_over_configured() -> None:
    reported = AIMessage("hi", response_metadata={"model_name": "claude-x-20260101"})
    assert model_of(stamp_model(reported, "claude-x"), "claude-x") == "claude-x-20260101"
    bare = stamp_model(AIMessage("hi"), "claude-x")
    assert message_to_dict(bare)["model"] == "claude-x"
    assert "model" not in message_to_dict(AIMessage("no metadata"))


def test_chat_reply_records_its_model(make_app: object) -> None:
    client: TestClient = make_app(
        model="base", models={"chat": {"assistant": "small"}}
    ).test_client()  # type: ignore[operator]
    thread_id = new_thread(client, "chat")
    done = run_and_wait(client, thread_id, {"message": "hello"})
    assert done["models"] == {"assistant": "small"}
    messages = client.get(f"/api/v1/threads/{thread_id}/state").json()["values"]["messages"]
    assert [m.get("model") for m in messages if m["type"] == "ai"] == ["small"]


def test_research_records_each_roles_model(make_app: object) -> None:
    client: TestClient = make_app(  # type: ignore[operator]
        model="base", models={"research": {"searcher": "small", "writer": "big"}}
    ).test_client()
    thread_id = new_thread(client, "research")
    paused = run_and_wait(client, thread_id, {"message": "tidal power"})
    assert paused["models"] == RESEARCH
    state = client.get(f"/api/v1/threads/{thread_id}/state").json()
    assert state["interrupts"][0]["value"]["models"] == RESEARCH  # shown on the approval card

    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"resume": {"approved": True}})
    assert read_sse(client, run.json()["events_url"])[-1] == "run.completed"
    values = client.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    assert {f["model"] for f in values["findings"]} == {"small"}
    assert [m.get("model") for m in values["messages"] if m["type"] == "ai"] == ["big"]
    assert values["metadata"]["models"] == RESEARCH


def test_run_started_event_carries_the_model_map(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    url = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "hi"}).json()[
        "events_url"
    ]
    with client.stream("GET", url) as response:
        for line in response.iter_lines():
            if line.startswith("data: ") and '"models"' in line:
                assert '"assistant"' in line
                break
        else:  # pragma: no cover
            raise AssertionError("run.started had no models")
