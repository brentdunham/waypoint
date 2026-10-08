"""Checkpoint history, forking, and resuming from an edited past."""

from flask.testing import FlaskClient
from tests.conftest import new_thread, run_and_wait


def test_every_super_step_leaves_a_checkpoint(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    run_and_wait(client, thread_id, {"message": "first"})

    checkpoints = client.get(f"/api/v1/threads/{thread_id}/history").get_json()["checkpoints"]
    assert len(checkpoints) >= 3
    assert checkpoints[0]["checkpoint_id"]
    # Newest first, and each one points at its parent.
    assert checkpoints[0]["parent_checkpoint_id"] == checkpoints[1]["checkpoint_id"]


def test_state_can_be_rewritten_into_a_new_branch(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    run_and_wait(client, thread_id, {"message": "original question"})
    checkpoints = client.get(f"/api/v1/threads/{thread_id}/history").get_json()["checkpoints"]
    target = checkpoints[-2]["checkpoint_id"]

    response = client.post(
        f"/api/v1/threads/{thread_id}/state",
        json={
            "checkpoint_id": target,
            "values": {"messages": [{"role": "user", "content": "rewritten question"}]},
        },
    )
    assert response.status_code == 201
    forked = response.get_json()["checkpoint_id"]
    assert forked is not None
    assert forked != target

    result = run_and_wait(client, thread_id, {"input": {}, "checkpoint_id": forked})
    assert result["status"] == "completed"


def test_reading_state_at_an_old_checkpoint(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    run_and_wait(client, thread_id, {"message": "hello"})
    checkpoints = client.get(f"/api/v1/threads/{thread_id}/history").get_json()["checkpoints"]
    oldest = checkpoints[-1]["checkpoint_id"]

    snapshot = client.get(f"/api/v1/threads/{thread_id}/state?checkpoint_id={oldest}").get_json()
    assert snapshot["checkpoint_id"] == oldest
