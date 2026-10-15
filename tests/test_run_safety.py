"""Ownership and admission must agree across users, endpoints and processes."""

import asyncio
import time
from typing import Any

import pytest
from tests.conftest import new_thread, read_sse, run_and_wait

from waypoint.agents.models import ScriptedChatModel
from waypoint.errors import Forbidden, NotFound
from waypoint.infra.security import Principal, authenticate


def principal(user: str, tenant: str = "acme", *extra: str) -> Principal:
    return Principal(
        tenant, user, user, frozenset({"threads:read", "threads:write", "agents:run", *extra})
    )


def test_run_endpoints_enforce_the_threads_user_ownership(make_app: Any) -> None:
    app = make_app()
    caller = [principal("alice")]
    app.app.dependency_overrides[authenticate] = lambda: caller[0]
    client = app.test_client()
    thread = new_thread(client, "chat")
    result = run_and_wait(client, thread, {"message": "alice private reply"})
    run_id = result["run_id"]
    for user, tenant, expected in [("bob", "acme", 403), ("eve", "elsewhere", 404)]:
        caller[0] = principal(user, tenant)
        assert client.get(f"/api/v1/threads/{thread}").status_code == expected
        assert client.get("/api/v1/threads").json()["threads"] == []
        for path in (f"/runs/{run_id}", f"/runs/{run_id}/events"):
            assert client.get(f"/api/v1{path}").status_code == expected
        assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == expected
    for scope in ("tenant:admin", "*"):
        caller[0] = principal("admin", "acme", scope)
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 200
        assert len(client.get("/api/v1/threads").json()["threads"]) == 1


def test_historical_trace_checks_user_ownership_after_redis_expiry(make_app: Any) -> None:
    app = make_app()

    class Audit:
        async def fetch_one(self, *_: Any) -> dict[str, str]:
            return {"tenant_id": "acme", "user_id": "alice", "trace_id": "a" * 32}

    app.runtime.runs._db = Audit()
    with pytest.raises(Forbidden):
        app.call(app.runtime.runs.trace_id, "expired", principal("bob"))
    with pytest.raises(NotFound):
        app.call(app.runtime.runs.trace_id, "expired", principal("eve", "elsewhere"))
    assert app.call(app.runtime.runs.trace_id, "expired", principal("alice")) == "a" * 32
    app.runtime.runs._db = None


def test_an_active_thread_refuses_a_second_run_then_accepts_the_next(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ScriptedChatModel._next

    def slow(self: ScriptedChatModel, messages: Any) -> Any:
        time.sleep(0.15)
        return original(self, messages)

    monkeypatch.setattr(ScriptedChatModel, "_next", slow)
    app = make_app()
    client = app.test_client()
    thread = new_thread(client, "chat")
    first = client.post(f"/api/v1/threads/{thread}/runs", json={"message": "first"})
    assert first.status_code == 202
    second = client.post(f"/api/v1/threads/{thread}/runs", json={"message": "second"})
    assert second.status_code == 409
    assert read_sse(client, first.json()["events_url"])[-1] == "run.completed"
    assert run_and_wait(client, thread, {"message": "third"})["status"] == "completed"


def test_remember_replaying_one_tool_call_does_not_append_a_second_fact() -> None:
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode
    from langgraph.store.memory import InMemoryStore

    from waypoint.agents.context import AgentContext
    from waypoint.agents.state import ChatState
    from waypoint.agents.tools import remember

    async def scenario() -> None:
        store = InMemoryStore()
        builder = StateGraph(ChatState, context_schema=AgentContext)
        builder.add_node("tools", ToolNode([remember]))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        graph = builder.compile(checkpointer=InMemorySaver(), store=store)
        context = AgentContext("acme", "alice", "req", "run", "chat")
        config: Any = {"configurable": {"thread_id": "same-thread"}}
        for call_id in ("one", "one", "two"):
            await graph.ainvoke(
                {
                    "messages": [
                        AIMessage(
                            "",
                            tool_calls=[
                                {"name": "remember", "args": {"fact": "tea"}, "id": call_id}
                            ],
                        )
                    ]
                },
                config,
                context=context,
            )
        assert len(await store.asearch(context.memory_namespace)) == 2

    asyncio.run(scenario())


def test_separate_user_turns_have_distinct_memory_writes(client: Any) -> None:
    thread = new_thread(client, "chat")
    for fact in ("likes tea", "uses metric"):
        result = run_and_wait(
            client, thread, {"message": f'[[tool:remember({{"fact": "{fact}"}})]]'}
        )
        assert result["status"] == "completed"
    facts = {item["value"]["fact"] for item in client.get("/api/v1/memories").json()["memories"]}
    assert facts == {"likes tea", "uses metric"}
