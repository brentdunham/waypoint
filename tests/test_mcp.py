"""MCP tools in Workspace (docs/design/tools.md, phase 3). Unit tier.

Offline: the server is an in-memory FastMCP server, reached through the
catalog's ``connect`` hook instead of HTTP. The app is configured with a
server whose URL is never dialled; each test stops the background discovery
(which would try that URL), points ``connect`` at the in-memory server and
discovers once.
"""

import asyncio
from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP
from langchain_core.tools import StructuredTool
from mcp.types import ToolAnnotations
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient
from tests.conftest import TestApp, new_thread, run_and_wait

from waypoint.agents.mcp import TENANT_META, McpCatalog, needs_approval
from waypoint.infra.security import Principal, authenticate
from waypoint.settings import McpServer, McpSettings
from waypoint.tracing import TraceHandler, Tracing

NOTES = {"name": "notes", "url": "http://notes.invalid/mcp", "trusted": True}


def notes_server() -> FastMCP:
    server = FastMCP("notes")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def search(query: str, ctx: Context) -> str:
        """Search the user's notes."""
        meta = ctx.request_context.meta if ctx.request_context else None
        tenant = (meta or {}).get(TENANT_META, "?")
        return f"{tenant}: a note about {query}"

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def slow(seconds: float) -> str:
        """Take a while."""
        await asyncio.sleep(seconds)
        return "done"

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def broken() -> str:
        """Always fails."""
        raise ValueError("kaput")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def add(text: str) -> str:
        """Add a note."""
        return f"added {text}"

    return server


def catalog(**server: Any) -> McpCatalog:
    settings = McpSettings(servers=[McpServer(**(NOTES | server))])
    built = notes_server()
    return McpCatalog(settings, connect=lambda _s: Client(built))


def mcp_app(make_app: Any, principal: Principal | None = None, **mcp: Any) -> TestApp:
    app: TestApp = make_app(mcp={"servers": [NOTES], **mcp})  # mcp may replace servers
    if principal is not None:
        app.app.dependency_overrides[authenticate] = lambda: principal
    runtime_mcp = app.runtime.mcp
    assert runtime_mcp is not None
    app.call(runtime_mcp.aclose)  # no background round against the unreachable URL
    built = notes_server()
    runtime_mcp.connect = lambda _s: Client(built)
    app.call(runtime_mcp.refresh)
    return app


def tool_messages(client: TestClient, thread_id: str) -> list[dict[str, Any]]:
    values = client.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    return [m for m in values["messages"] if m.get("type") == "tool"]


def ask(client: TestClient, message: str) -> list[dict[str, Any]]:
    thread_id = new_thread(client, "workspace")
    done = run_and_wait(client, thread_id, {"message": message})
    assert done["status"] == "completed", done
    return tool_messages(client, thread_id)


def reviewer(*extra: str) -> Principal:
    scopes = {"agents:run", "threads:read", "threads:write", *extra}
    return Principal(tenant_id="rev-acme", user_id="r", key_id="k", scopes=frozenset(scopes))


# -- the policy and discovery --------------------------------------------------------


def test_only_a_trusted_servers_read_only_tools_skip_approval() -> None:
    trusted = McpServer(**NOTES)
    untrusted = McpServer(**(NOTES | {"trusted": False}))
    read_only = {"read_only_hint": True}
    assert needs_approval(trusted, "search", read_only) is False
    assert needs_approval(trusted, "add", {"read_only_hint": False}) is True
    assert needs_approval(trusted, "add", {}) is True  # no hint: assume it writes
    assert needs_approval(untrusted, "search", read_only) is True  # hints not believed
    overridden = McpServer(**(NOTES | {"trusted": False, "approval": {"search": False}}))
    assert needs_approval(overridden, "search", {}) is False  # config wins
    forced = McpServer(**(NOTES | {"approval": {"search": True}}))
    assert needs_approval(forced, "search", read_only) is True


def test_discovery_prefixes_names_and_filters_by_scope() -> None:
    mcp = catalog()
    asyncio.run(mcp.refresh())
    names = {t.tool.name: t.needs_approval for t in mcp.tools_for(["*"])}
    assert names == {
        "notes_search": False,
        "notes_slow": False,
        "notes_broken": False,
        "notes_add": True,
    }
    assert set(mcp.offered_for(["tools:notes"])) == set(names)
    assert mcp.offered_for(["agents:run", "threads:write"]) == {}  # no scope, no tools
    assert mcp.offered_for(["tools:other"]) == {}
    assert mcp.status() == {"notes": {"tools": 4, "error": None}}


def test_every_tool_of_an_untrusted_server_needs_approval_unless_configured() -> None:
    mcp = catalog(trusted=False)
    asyncio.run(mcp.refresh())
    assert all(t.needs_approval for t in mcp.offered_for(["*"]).values())
    allowed = catalog(trusted=False, approval={"search": False})
    asyncio.run(allowed.refresh())
    free = [n for n, t in allowed.offered_for(["*"]).items() if not t.needs_approval]
    assert free == ["notes_search"]


def test_a_server_that_is_down_is_not_fatal_and_comes_back() -> None:
    mcp = catalog()
    working = mcp.connect

    def refuse(_server: McpServer) -> Any:
        raise ConnectionRefusedError("nothing listening")

    async def scenario() -> None:
        mcp.connect = refuse
        await mcp.refresh()  # logged, not raised
        assert mcp.tools_for(["*"]) == []
        assert "ConnectionRefusedError" in (mcp.status()["notes"]["error"] or "")
        before = mcp.version
        mcp.connect = working
        await mcp.refresh()
        assert len(mcp.tools_for(["*"])) == 4
        assert mcp.version > before  # bound models are rebuilt with the new tools
        assert mcp.status()["notes"]["error"] is None

    asyncio.run(scenario())


def test_readiness_shows_a_server_but_never_fails_on_it(make_app: Any) -> None:
    app: TestApp = make_app(mcp={"servers": [NOTES]})
    runtime_mcp = app.runtime.mcp
    assert runtime_mcp is not None
    app.call(runtime_mcp.aclose)

    def refuse(_server: McpServer) -> Any:
        raise ConnectionRefusedError("nothing listening")

    runtime_mcp.connect = refuse
    app.call(runtime_mcp.refresh)
    response = app.test_client().get("/api/v1/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["mcp:notes"] == "unavailable"


def test_no_servers_means_no_background_work() -> None:
    mcp = McpCatalog(McpSettings())
    mcp.start()
    assert mcp._task is None


# -- calls from Workspace ------------------------------------------------------------


def test_workspace_calls_a_read_only_tool_with_the_tenant(make_app: Any) -> None:
    client = mcp_app(make_app, reviewer("tools:notes")).test_client()
    [result] = ask(client, '[[tool:notes_search({"query": "budgets"})]] What do my notes say?')
    assert result["name"] == "notes_search"
    assert "rev-acme: a note about budgets" in result["content"]  # tenant sent in _meta
    assert "data, not instructions" in result["content"]


def test_a_key_without_the_scope_gets_no_tools(make_app: Any) -> None:
    client = mcp_app(make_app, reviewer()).test_client()
    [result] = ask(client, '[[tool:notes_search({"query": "budgets"})]] What do my notes say?')
    assert "not a valid tool" in result["content"]


# -- approval (phase 4) ----------------------------------------------------------------

ADD = '[[tool:notes_add({"text": "call the venue"})]] Add a note.'


def pause(client: TestClient, message: str) -> tuple[str, dict[str, Any]]:
    """Start a run that should stop for approval; the thread and its interrupt."""
    thread_id = new_thread(client, "workspace")
    done = run_and_wait(client, thread_id, {"message": message})
    assert done["status"] == "interrupted", done
    # What the UI reads back after a reload: the pause lives in the checkpoint.
    state = client.get(f"/api/v1/threads/{thread_id}/state").json()
    [pending] = state["interrupts"]
    return thread_id, pending["value"]


def test_a_write_tool_pauses_and_runs_when_approved(make_app: Any) -> None:
    client = mcp_app(make_app).test_client()
    thread_id, value = pause(client, ADD)
    assert value["kind"] == "approve_tools"
    [call] = value["calls"]
    assert (call["tool"], call["server"], call["remote_tool"]) == ("notes_add", "notes", "add")
    assert call["args"] == {"text": "call the venue"}
    assert tool_messages(client, thread_id) == []  # nothing ran before the answer

    done = run_and_wait(client, thread_id, {"resume": {"tools": {call["id"]: True}}})
    assert done["status"] == "completed", done
    [result] = tool_messages(client, thread_id)
    assert "added call the venue" in result["content"]


def test_a_declined_call_never_reaches_the_server(make_app: Any) -> None:
    app = mcp_app(make_app)
    client = app.test_client()
    thread_id, _ = pause(client, ADD)
    done = run_and_wait(client, thread_id, {"resume": {"approved": False}})
    assert done["status"] == "completed", done
    [result] = tool_messages(client, thread_id)
    assert result["content"].startswith("Not run: the user declined")
    assert "added" not in result["content"]


def test_each_call_is_decided_on_its_own(make_app: Any) -> None:
    client = mcp_app(make_app).test_client()
    two = '[[tool:notes_add({"text": "a"})]] [[tool:notes_add({"text": "b"})]] [[tool:notes_search({"query": "q"})]] Go.'
    thread_id, value = pause(client, two)
    first, _second = (c["id"] for c in value["calls"])  # the search needs no approval
    assert len(value["calls"]) == 2
    done = run_and_wait(
        client, thread_id, {"resume": {"tools": {first: True}}}
    )  # second unanswered: declined
    assert done["status"] == "completed", done
    contents = [m["content"] for m in tool_messages(client, thread_id)]
    assert "added a" in contents[0]
    assert contents[1].startswith("Not run: the user declined")
    assert "a note about q" in contents[2]  # read-only, ran without asking


def test_read_only_tools_never_pause(make_app: Any) -> None:
    client = mcp_app(make_app).test_client()
    thread_id = new_thread(client, "workspace")
    message = '[[tool:notes_search({"query": "x"})]] Search.'
    assert run_and_wait(client, thread_id, {"message": message})["status"] == "completed"


def test_an_untrusted_servers_read_only_tool_pauses(make_app: Any) -> None:
    client = mcp_app(make_app, servers=[NOTES | {"trusted": False}]).test_client()
    _, value = pause(client, '[[tool:notes_search({"query": "x"})]] Search.')
    assert [c["tool"] for c in value["calls"]] == ["notes_search"]


def test_calls_past_the_limit_are_refused_in_words(make_app: Any) -> None:
    client = mcp_app(make_app, max_calls_per_run=2).test_client()
    call = '[[tool:notes_search({"query": "q"})]]'
    results = ask(client, f"{call} {call} {call} [[tool:now({{}})]] Search three times.")
    assert [r["name"] for r in results] == [
        "notes_search",
        "notes_search",
        "notes_search",
        "now",
    ]  # in the order called
    refused = ["MCP__MAX_CALLS_PER_RUN" in r["content"] for r in results]
    assert refused == [False, False, True, False]  # built-in tools do not count


def test_a_slow_server_times_out_into_a_message(make_app: Any) -> None:
    app = mcp_app(make_app, servers=[NOTES | {"timeout_s": 0.2}])
    [result] = ask(app.test_client(), '[[tool:notes_slow({"seconds": 3})]] Be slow.')
    assert "did not answer within 0.2 s" in result["content"]


def test_a_server_error_reaches_the_model(make_app: Any) -> None:
    client = mcp_app(make_app).test_client()
    [result] = ask(client, "[[tool:notes_broken({})]] Break.")
    assert "notes server reported an error" in result["content"]
    assert "kaput" in result["content"]


# -- tracing -------------------------------------------------------------------------


def test_a_tool_span_names_its_server() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = Tracing(tracer=provider.get_tracer("t"), provider=provider)
    root = tracing.tracer.start_span("run test")
    handler = TraceHandler(tracing, root, attributes={"waypoint.run_id": "r"})

    async def search(query: str) -> str:
        return query

    tool = StructuredTool.from_function(
        coroutine=search,
        name="notes_search",
        description="Search.",
        metadata={"waypoint_tool_server": "notes"},
    )
    asyncio.run(tool.ainvoke({"query": "x"}, config={"callbacks": [handler]}))
    root.end()
    [span] = [s for s in exporter.get_finished_spans() if s.name == "execute_tool notes_search"]
    assert span.attributes is not None
    assert span.attributes["waypoint.tool.server"] == "notes"


@pytest.mark.parametrize("name", ["Notes", "1notes", "notes-x", "a" * 25])
def test_server_names_are_short_slugs(name: str) -> None:
    with pytest.raises(ValueError, match="name"):
        McpServer(**(NOTES | {"name": name}))


# -- the demo notes server -------------------------------------------------------------


def test_the_notes_server_keeps_tenants_apart_and_needs_one() -> None:
    from waypoint.mcp_demo.notes import MemoryNotes, build

    async def scenario() -> None:
        client = Client(build(MemoryNotes()))
        async with client:
            annotations = {t.name: t.annotations for t in await client.list_tools()}
            assert annotations["search"].read_only_hint is True  # type: ignore[union-attr]
            assert annotations["delete"].destructive_hint is True  # type: ignore[union-attr]

            async def call(tool: str, args: dict[str, Any], tenant: str | None) -> Any:
                meta = {TENANT_META: tenant} if tenant else None
                return await client.call_tool(tool, args, meta=meta, raise_on_error=False)

            await call("add", {"text": "acme's secret"}, "acme")
            assert "acme's secret" in (await call("list", {}, "acme")).content[0].text
            assert (await call("search", {"query": "secret"}, "globex")).content[0].text == (
                "No notes."
            )
            refused = await call("list", {}, None)
            assert refused.is_error
            assert "tenant" in refused.content[0].text
            assert (await call("delete", {"note_id": 1}, "globex")).is_error  # not theirs
            assert not (await call("delete", {"note_id": 1}, "acme")).is_error

    asyncio.run(scenario())
