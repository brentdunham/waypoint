from flask.testing import FlaskClient


def test_healthz_never_touches_dependencies(client: FlaskClient) -> None:
    body = client.get("/api/v1/healthz").get_json()
    assert body["status"] == "ok"
    assert body["loop"] == "running"


def test_readyz_reports_each_dependency(client: FlaskClient) -> None:
    response = client.get("/api/v1/readyz")
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "ready"
    # No infra configured in the unit tier, so both report "off", not "down".
    assert body["checks"] == {"graphs": "ok", "postgres": "off", "redis": "off"}


def test_agents_are_discoverable(client: FlaskClient) -> None:
    agents = client.get("/api/v1/agents").get_json()["agents"]
    names = {a["name"] for a in agents}
    assert {"chat", "research"} <= names
    research = next(a for a in agents if a["name"] == "research")
    assert research["supports_interrupts"] is True
