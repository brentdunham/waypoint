"""Model routing: which model serves each role of each agent, per run.

Resolution is pure and tested directly. The API tests check that a run's
``models`` request is validated, resolved once, and visible on the run. The
cache test is the reason ``WorkerInput`` carries the searcher model.
"""

import asyncio
from typing import Any

import pytest
from flask.testing import FlaskClient
from langgraph.cache.memory import InMemoryCache
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from tests.conftest import build_settings, new_thread, run_and_wait

from waypoint import create_app
from waypoint.agents import models as models_module
from waypoint.agents.context import AgentContext
from waypoint.agents.graphs import research
from waypoint.agents.models import (
    ModelChoiceError,
    ModelConfigError,
    ModelRouter,
    check_model_config,
    resolve_models,
)
from waypoint.agents.registry import models_for, roles_by_agent

ROLES = ("planner", "searcher", "critic", "writer")


# -- resolution ----------------------------------------------------------------


def test_unset_everything_falls_back_to_model() -> None:
    settings = build_settings(model="base")
    assert resolve_models(settings, "research", ROLES) == dict.fromkeys(ROLES, "base")


def test_most_specific_setting_wins() -> None:
    settings = build_settings(
        model="base", models={"research": {"default": "agent", "critic": "role"}}
    )
    resolved = resolve_models(settings, "research", ROLES)
    assert resolved == {
        "planner": "agent",
        "searcher": "agent",
        "critic": "role",
        "writer": "agent",
    }


def test_run_override_beats_config_and_default_covers_the_rest() -> None:
    settings = build_settings(model="base", models={"research": {"critic": "role"}})
    resolved = resolve_models(
        settings, "research", ROLES, {"default": "run-all", "writer": "run-writer"}
    )
    assert resolved == {
        "planner": "run-all",
        "searcher": "run-all",
        "critic": "run-all",
        "writer": "run-writer",
    }


def test_env_names_are_case_insensitive_and_blank_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from waypoint.settings import Settings

    monkeypatch.setenv("MODELS__Research__CRITIC", "claude-opus-x")
    monkeypatch.setenv("MODELS__CHAT__ASSISTANT", "")
    settings = Settings(_env_file="")  # type: ignore[call-arg]
    assert settings.models == {"research": {"critic": "claude-opus-x"}}


# -- validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("models", "message"),
    [
        ({"reserch": {"default": "m"}}, "names no agent"),
        ({"research": {"crtic": "m"}}, "has no role 'crtic'"),
    ],
)
def test_config_typos_fail_at_boot(models: dict[str, dict[str, str]], message: str) -> None:
    with pytest.raises(ModelConfigError, match=message):
        check_model_config(build_settings(models=models), roles_by_agent())
    with pytest.raises(ModelConfigError, match=message):
        create_app(build_settings(models=models))


def test_allowed_models_default_to_those_already_configured() -> None:
    settings = build_settings(model="base", models={"research": {"critic": "big"}})
    assert settings.allowed_models == {"base", "big"}
    assert build_settings(models_allowed=["a", "b"]).allowed_models == {"a", "b"}


def test_a_run_cannot_pick_a_model_nobody_approved() -> None:
    settings = build_settings(model="base")
    with pytest.raises(ModelChoiceError, match="not allowed"):
        models_for(settings, "research", {"critic": "claude-opus-expensive"})
    with pytest.raises(ModelChoiceError, match="no model role"):
        models_for(settings, "research", {"assistant": "base"})


# -- through the API -------------------------------------------------------------


def test_run_records_the_models_it_used(make_app: Any) -> None:
    app = make_app(model="base", models_allowed=["base", "small"])
    client: FlaskClient = app.test_client()
    thread_id = new_thread(client, "research")
    started = run_and_wait(
        client, thread_id, {"message": "tidal power", "models": {"critic": "small"}}
    )
    assert started["status"] == "interrupted"

    done = run_and_wait(
        client, thread_id, {"resume": {"approved": True}, "models": {"default": "small"}}
    )
    # The resumed run's own choice applies, and the briefing says what made it.
    assert done["result"]["metadata"]["models"] == dict.fromkeys(ROLES, "small")
    record = client.get(f"/api/v1/runs/{done['run_id']}").get_json()
    assert record["models"] == dict.fromkeys(ROLES, "small")


def test_bad_model_request_is_a_400(client: FlaskClient) -> None:
    thread_id = new_thread(client, "chat")
    response = client.post(
        f"/api/v1/threads/{thread_id}/runs",
        json={"message": "hi", "models": {"assistant": "claude-opus-expensive"}},
    )
    assert response.status_code == 400
    assert "not allowed" in response.get_json()["detail"]


def test_agents_endpoint_lists_model_roles(client: FlaskClient) -> None:
    agents = {a["name"]: a for a in client.get("/api/v1/agents").get_json()["agents"]}
    assert agents["research"]["model_roles"] == list(ROLES)
    assert agents["chat"]["model_roles"] == ["assistant"]


# -- the router ----------------------------------------------------------------


def test_router_builds_each_model_once_and_honours_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str | None] = []
    real = models_module.build_model

    def spy(settings: Any, *, model: str | None = None, **kwargs: Any) -> Any:
        built.append(model)
        return real(settings, model=model, **kwargs)

    monkeypatch.setattr(models_module, "build_model", spy)
    router = ModelRouter(build_settings(model="base"), "research", ROLES)
    critic = router.bind("critic")

    def ctx(models: dict[str, str]) -> AgentContext:
        return AgentContext(
            tenant_id="t", user_id="u", request_id="r", run_id="x", agent="research",
            models=tuple(models.items()),
        )  # fmt: skip

    for _ in range(3):
        critic(ctx({}))
        critic(ctx({"critic": "big"}))
    assert built == ["base", "big"]
    with pytest.raises(KeyError):
        router.bind("assistant")


# -- the node cache --------------------------------------------------------------


def test_cached_answer_is_never_served_to_a_different_searcher_model() -> None:
    async def cache_hits(searchers: list[str]) -> list[int]:
        graph = research.build(
            build_settings(node_cache_ttl_s=60),
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
            cache=InMemoryCache(),
        )
        hits: list[int] = []
        for i, searcher in enumerate(searchers):
            config: Any = {"configurable": {"thread_id": f"t{i}"}}
            ctx = AgentContext(
                tenant_id="acme", user_id="u", request_id="r", run_id="x", agent="research",
                models=(("searcher", searcher),),
            )  # fmt: skip
            await graph.ainvoke({"topic": "heat pumps"}, config, context=ctx)
            count = 0
            async for chunk in graph.astream(
                Command(resume={"approved": True}), config, context=ctx, stream_mode="updates"
            ):
                if "investigate" in chunk and (chunk.get("__metadata__") or {}).get("cached"):
                    count += 1
            hits.append(count)
        return hits

    first, same_model, other_model = asyncio.run(cache_hits(["small", "small", "big"]))
    assert first == 0
    assert same_model > 0
    assert other_model == 0


def test_structured_output_method_comes_from_settings() -> None:
    from waypoint.agents.models import structured

    seen: dict[str, Any] = {}

    class Recorder:
        def with_structured_output(self, schema: Any, **kwargs: Any) -> str:
            seen.update(kwargs, schema=schema)
            return "bound"

    assert structured(Recorder(), dict, build_settings()) == "bound"  # type: ignore[arg-type]
    assert seen["method"] == "json_schema"
    structured(Recorder(), dict, build_settings(structured_output="function_calling"))  # type: ignore[arg-type]
    assert seen["method"] == "function_calling"
