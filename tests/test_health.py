from starlette.testclient import TestClient


def test_healthz_never_touches_dependencies(client: TestClient) -> None:
    body = client.get("/api/v1/healthz").json()
    assert body["status"] == "ok"


def test_readyz_reports_each_dependency(client: TestClient) -> None:
    response = client.get("/api/v1/readyz")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ready"
    # No infra configured in the unit tier, so both report "off", not "down".
    assert body["checks"] == {"graphs": "ok", "postgres": "off", "redis": "off"}


def test_agents_are_discoverable(client: TestClient) -> None:
    agents = client.get("/api/v1/agents").json()["agents"]
    names = {a["name"] for a in agents}
    assert {"chat", "research"} <= names
    research = next(a for a in agents if a["name"] == "research")
    assert research["supports_interrupts"] is True
