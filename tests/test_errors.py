from typing import Any

import pytest
from starlette.testclient import TestClient
from tests.conftest import TestApp


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith("http-error")


def test_validation_failure_lists_fields(client: TestClient) -> None:
    response = client.post("/api/v1/threads", json={"nope": 1})
    assert response.status_code == 422
    body = response.json()
    assert body["type"].endswith("validation-failed")
    assert body["errors"]


def test_unknown_agent_is_404_and_lists_alternatives(client: TestClient) -> None:
    response = client.post("/api/v1/threads", json={"agent": "nonexistent"})
    assert response.status_code == 404
    assert "chat" in response.json()["available"]


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


def test_bad_query_parameter_is_422_not_500(client: TestClient) -> None:
    response = client.get("/api/v1/threads", params={"limit": "abc"})
    assert response.status_code == 422
    body = response.json()
    assert body["type"].endswith("validation-failed")
    assert body["errors"][0]["loc"] == ["query", "limit"]


def test_body_errors_name_the_field_without_a_body_prefix(client: TestClient) -> None:
    response = client.post("/api/v1/threads", json={"agent": ""})
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["agent"]


def test_oversized_body_is_413_problem_json(client: TestClient) -> None:
    from waypoint import MAX_BODY_BYTES

    big = b"x" * (MAX_BODY_BYTES + 1)
    declared = client.post("/api/v1/threads", content=big, headers={"X-Request-ID": "r1"})
    assert declared.status_code == 413
    assert declared.headers["content-type"] == "application/problem+json"
    assert declared.json()["type"].endswith("payload-too-large")
    assert declared.headers["X-Request-ID"] == "r1"

    def chunks():  # no Content-Length: counted as it arrives
        for _ in range(9):
            yield b"x" * (1024 * 1024)

    chunked = client.post(
        "/api/v1/threads", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert chunked.status_code == 413
    assert chunked.json()["type"].endswith("payload-too-large")


def test_unhandled_error_is_500_problem_json_with_request_id(
    app: TestApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_self: object) -> dict[str, str]:
        raise RuntimeError("secret internals")

    monkeypatch.setattr(type(app.runtime), "health", boom)
    response = app.test_client().get("/api/v1/readyz", headers={"X-Request-ID": "r2"})
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["X-Request-ID"] == "r2"
    assert "secret internals" not in response.text
    assert response.json()["request_id"] == "r2"


def test_cors_preflight_allows_the_ui_and_its_headers(make_app: Any) -> None:
    client = make_app(cors_origins=["http://localhost:5173"]).test_client()
    response = client.options(
        "/api/v1/threads",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,last-event-id",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "X-Request-ID" in response.headers
    simple = client.get("/api/v1/healthz", headers={"Origin": "http://localhost:5173"})
    assert simple.headers["access-control-expose-headers"] == "X-Request-ID"
    other = client.get("/api/v1/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_openapi_documents_every_route_and_the_api_key(client: TestClient) -> None:
    spec = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/threads/{thread_id}/runs" in spec["paths"]
    assert "/api/v1/runs/{run_id}/events" in spec["paths"]
    assert {"HTTPBearer", "APIKeyHeader"} <= set(spec["components"]["securitySchemes"])
    started = spec["paths"]["/api/v1/threads/{thread_id}/runs"]["post"]["responses"]
    assert {"200", "202"} <= set(started)
    assert client.get("/api/v1/docs").status_code == 200
