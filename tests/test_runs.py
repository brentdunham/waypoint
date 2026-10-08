from starlette.testclient import TestClient
from tests.conftest import new_thread, read_sse, run_and_wait


def test_chat_run_completes_and_returns_the_last_message(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    result = run_and_wait(client, thread_id, {"message": "hello"})
    assert result["status"] == "completed"
    assert "hello" in result["result"]["message"]["content"]


def test_run_returns_202_with_follow_urls(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    response = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "hi"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["events_url"].endswith("/events")


def test_sse_stream_ends_with_a_terminal_event(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "stream me"})
    events = read_sse(client, run.json()["events_url"])
    assert events[0] == "run.started"
    assert events[-1] == "run.completed"
    assert "node" in events  # per-super-step progress


def test_tokens_are_streamed_not_just_the_final_message(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    run = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "one two three"})
    events = read_sse(client, run.json()["events_url"])
    assert events.count("token") > 1


def test_empty_body_is_rejected(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    response = client.post(f"/api/v1/threads/{thread_id}/runs", json={})
    assert response.status_code == 400
    assert "resume" in response.json()["detail"]


def test_oversized_input_is_refused_before_the_model_is_called(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    response = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "x" * 100_001})
    assert response.status_code in {400, 422}


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/runs/does-not-exist").status_code == 404


def _ids_and_names(client: TestClient, url: str, headers: dict[str, str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    event_id = ""
    with client.stream("GET", url, headers=headers) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["X-Accel-Buffering"] == "no"
        for line in response.iter_lines():
            if line.startswith("id: "):
                event_id = line.removeprefix("id: ")
            elif line.startswith("event: "):
                out.append((event_id, line.removeprefix("event: ")))
                if out[-1][1].startswith("run.") and out[-1][1] != "run.started":
                    break
    return out


def test_events_resume_after_last_event_id_header(client: TestClient) -> None:
    thread_id = new_thread(client, "chat")
    url = client.post(f"/api/v1/threads/{thread_id}/runs", json={"message": "a b c"}).json()[
        "events_url"
    ]
    everything = _ids_and_names(client, url, {})
    assert everything[-1][1] == "run.completed"
    tail = _ids_and_names(client, url, {"Last-Event-ID": everything[0][0]})
    assert tail == everything[1:]
