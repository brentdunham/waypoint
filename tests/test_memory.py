"""Long-term memory is cross-thread and namespaced per tenant + user."""

from flask.testing import FlaskClient
from tests.conftest import new_thread, run_and_wait


def test_facts_round_trip(client: FlaskClient) -> None:
    created = client.post("/api/v1/memories", json={"fact": "prefers metric units"})
    assert created.status_code == 201

    memories = client.get("/api/v1/memories").get_json()["memories"]
    assert any(m["value"]["fact"] == "prefers metric units" for m in memories)


def test_memories_are_visible_to_a_new_thread(client: FlaskClient) -> None:
    client.post("/api/v1/memories", json={"fact": "works in aerospace"})
    thread_id = new_thread(client, "chat")
    run_and_wait(client, thread_id, {"message": "what do you know about me?"})

    state = client.get(f"/api/v1/threads/{thread_id}/state").get_json()
    assert "works in aerospace" in state["values"]["recalled"]


def test_forgetting_removes_the_fact(client: FlaskClient) -> None:
    key = client.post("/api/v1/memories", json={"fact": "temporary"}).get_json()["key"]
    assert client.delete(f"/api/v1/memories/{key}").status_code == 204
    memories = client.get("/api/v1/memories").get_json()["memories"]
    assert all(m["key"] != key for m in memories)
