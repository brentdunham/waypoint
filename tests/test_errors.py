from flask.testing import FlaskClient


def test_unknown_route_is_problem_json(client: FlaskClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.content_type == "application/problem+json"
    assert response.get_json()["type"].endswith("http-error")


def test_validation_failure_lists_fields(client: FlaskClient) -> None:
    response = client.post("/api/v1/threads", json={"nope": 1})
    assert response.status_code == 422
    body = response.get_json()
    assert body["type"].endswith("validation-failed")
    assert body["errors"]


def test_unknown_agent_is_404_and_lists_alternatives(client: FlaskClient) -> None:
    response = client.post("/api/v1/threads", json={"agent": "nonexistent"})
    assert response.status_code == 404
    assert "chat" in response.get_json()["available"]


def test_every_response_carries_a_request_id(client: FlaskClient) -> None:
    response = client.get("/api/v1/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"
