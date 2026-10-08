"""Human-in-the-loop: the graph pauses, a second request resumes it."""

from starlette.testclient import TestClient
from tests.conftest import new_thread, run_and_wait


def test_research_pauses_for_approval(client: TestClient) -> None:
    thread_id = new_thread(client, "research")
    result = run_and_wait(client, thread_id, {"message": "tidal power"})

    assert result["status"] == "interrupted"
    interrupt = result["interrupts"][0]["value"]
    assert interrupt["kind"] == "approve_plan"
    assert len(interrupt["plan"]) >= 2

    state = client.get(f"/api/v1/threads/{thread_id}/state").json()
    assert state["next"] == ["approve"]


def test_approving_runs_the_fan_out_and_produces_a_report(client: TestClient) -> None:
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "tidal power"})

    result = run_and_wait(client, thread_id, {"resume": {"approved": True}})
    assert result["status"] == "completed"
    assert result["result"]["report"]
    # One finding per planned question -- the reducer accumulated them all.
    assert len(result["result"]["findings"]) >= 2


def test_the_human_can_edit_the_plan_while_approving(client: TestClient) -> None:
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "tidal power"})

    result = run_and_wait(
        client,
        thread_id,
        {"resume": {"approved": True, "plan": ["Only this question?"]}},
    )
    findings = result["result"]["findings"]
    assert len(findings) == 1
    assert findings[0]["question"] == "Only this question?"


def test_declining_stops_the_graph_without_running_workers(client: TestClient) -> None:
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "tidal power"})

    result = run_and_wait(client, thread_id, {"resume": {"approved": False}})
    assert result["status"] == "completed"
    assert "declined" in result["result"]["report"].lower()
    assert result["result"].get("findings") in (None, [])


def test_an_interrupt_survives_a_new_client(client: TestClient, app) -> None:
    """The pause lives in the checkpointer, not in the request that created it."""
    thread_id = new_thread(client, "research")
    run_and_wait(client, thread_id, {"message": "tidal power"})

    fresh = app.test_client()
    state = fresh.get(f"/api/v1/threads/{thread_id}/state").json()
    assert state["interrupts"]
