from flask.testing import FlaskClient
from tests.conftest import new_thread, read_sse, run_and_wait


def test_chat_run_completes_and_returns_the_last_message(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    result = run_and_wait(client, thread_id, {"message": "hello"})
    assert result["status"] == "completed"
    assert "hello" in result["result"]["message"]["content"]


def test_run_returns_202_with_follow_urls(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    response = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "hi"})
    assert response.status_code == 202
    body = response.get_json()
    assert body["status"] == "running"
    assert body["events_url"].endswith("/events")


def test_sse_stream_ends_with_a_terminal_event(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "stream me"})
    events = read_sse(client, run.get_json()["events_url"])
    assert events[0] == "run.started"
    assert events[-1] == "run.completed"
    assert "node" in events  # per-super-step progress


def test_tokens_are_streamed_not_just_the_final_message(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "one two three"})
    events = read_sse(client, run.get_json()["events_url"])
    assert events.count("token") > 1


def test_empty_body_is_rejected(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    response = client.post(f"/api/v1/threads/{thread_id}/runs", json={})
    assert response.status_code == 400
    assert "resume" in response.get_json()["detail"]


def test_oversized_input_is_refused_before_the_model_is_called(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    response = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "x" * 100_001})
    assert response.status_code in {400, 422}


def test_unknown_run_is_404(client: FlaskClient) -> None:
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404
