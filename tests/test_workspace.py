"""The workspace agent (docs/design/tools.md, phase 1). Unit tier.

Chat's tool loop and memory, plus a ``search_documents`` tool over the
thread's corpus whose results come back cited, numbered, listed and checked by
the research verifier. The scripted model cites the first passage of a
``search_documents`` result the way the API cites search results in a tool
result, so the path from retrieval to the reply's source list runs offline.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from starlette.testclient import TestClient
from tests.conftest import TestApp, build_settings, new_thread, read_sse, run_and_wait

from waypoint.agents.context import AgentContext
from waypoint.agents.graphs import workspace
from waypoint.agents.graphs.workspace import Evidence
from waypoint.agents.models import ScriptedChatModel
from waypoint.agents.registry import get_spec, list_specs
from waypoint.rag.store import MemoryCorpusStore, NewDocument

RAG = {"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 64}
DOC = b"""# Streams

## Reconnection

A client that loses the stream reconnects with Last-Event-ID and resumes.

## Storage

Events live in a Redis Stream with a one hour TTL after the run ends.
"""
SEARCH = '[[tool:search_documents({"query": "reconnect"})]] How does a client reconnect?'


@pytest.fixture
def rag(make_app: Any) -> TestClient:
    app: TestApp = make_app(rag=RAG)
    return app.test_client()


def corpus_with_doc(client: TestClient) -> str:
    corpus_id = str(client.post("/api/v1/corpora", json={"name": "docs"}).json()["corpus_id"])
    files = [("files", ("streams.md", DOC, "text/markdown"))]
    upload = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files).json()
    assert read_sse(client, upload["events_url"])[-1] == "run.completed"
    return corpus_id


def ask(
    client: TestClient, thread_id: str, message: str, corpus_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"message": message}
    if corpus_id is not None:
        body["input"] = {"corpus_id": corpus_id}
    return run_and_wait(client, thread_id, body)


def last_reply(client: TestClient, thread_id: str) -> str:
    state = client.get(f"/api/v1/threads/{thread_id}/state").json()
    return str(state["values"]["messages"][-1]["content"])


# -- the agent ---------------------------------------------------------------------


def test_workspace_is_listed_and_chat_is_unchanged() -> None:
    names = [a["name"] for a in list_specs()]
    assert "workspace" in names
    assert "chat" in names
    assert get_spec("workspace").model_roles == ("assistant", "verifier")
    assert get_spec("chat").model_roles == ("assistant",)


def test_without_documents_it_is_a_tool_using_chat(client: TestClient) -> None:
    thread_id = new_thread(client, "workspace")
    done = ask(client, thread_id, '[[tool:calculate({"expression": "6*7"})]]')
    assert done["status"] == "completed"
    state = client.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    assert any(m.get("type") == "tool" and m["content"] == "42" for m in state["messages"])
    assert not state.get("corpus")
    assert "Sources" not in state["report"]


def test_it_shares_memory_with_chat(client: TestClient) -> None:
    client.post("/api/v1/memories", json={"fact": "works in aerospace"})
    thread_id = new_thread(client, "workspace")
    ask(client, thread_id, "hello")
    state = client.get(f"/api/v1/threads/{thread_id}/state").json()
    assert "works in aerospace" in state["values"]["recalled"]


def test_a_reply_cites_the_threads_documents(rag: TestClient) -> None:
    corpus_id = corpus_with_doc(rag)
    thread_id = new_thread(rag, "workspace")
    done = ask(rag, thread_id, SEARCH, corpus_id)
    assert done["status"] == "completed", done
    values = rag.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    report = values["report"]
    body, _, listing = report.partition("\n\nSources\n")
    assert "[1]" in body
    assert listing.startswith("[1] streams.md")
    assert listing.endswith("(document)")
    assert last_reply(rag, thread_id) == report  # the reply shown is the cited one
    [finding] = values["findings"]
    assert finding["question"] == "reconnect"
    assert all(s["url"].startswith("doc://") for s in finding["sources"])
    assert any(s.get("quotes") for s in finding["sources"])
    assert values["metadata"]["corpora"] == [{"corpus_id": corpus_id, "name": "docs"}]
    assert values["metadata"]["document_sources_cited"] == 1
    assert values["metadata"]["verification"]["sentences"] >= 1


def test_the_corpus_holds_for_later_turns(rag: TestClient) -> None:
    corpus_id = corpus_with_doc(rag)
    thread_id = new_thread(rag, "workspace")
    ask(rag, thread_id, "hello", corpus_id)
    ask(rag, thread_id, SEARCH)  # no input: the thread keeps its corpus
    values = rag.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    assert values["corpus_ids"] == [corpus_id]  # the single corpus_id input, migrated
    assert values["corpora"] == [{"corpus_id": corpus_id, "name": "docs"}]
    assert not values.get("corpus_id")
    assert "[1]" in values["report"]


def test_a_thread_can_stop_searching_documents(rag: TestClient) -> None:
    corpus_id = corpus_with_doc(rag)
    thread_id = new_thread(rag, "workspace")
    ask(rag, thread_id, "hello", corpus_id)
    rag.post(
        f"/api/v1/threads/{thread_id}/runs?wait=true",
        json={"message": "and now?", "input": {"corpus_ids": []}},
    )
    values = rag.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    assert values["corpora"] == []


def test_an_unknown_corpus_fails_the_run(rag: TestClient) -> None:
    thread_id = new_thread(rag, "workspace")
    done = ask(rag, thread_id, "hi", "00000000-0000-0000-0000-000000000000")
    assert done["status"] == "failed"
    assert "Unknown corpus" in done["error"]


def test_documents_need_rag_on(client: TestClient) -> None:
    thread_id = new_thread(client, "workspace")
    done = ask(client, thread_id, "hi", "00000000-0000-0000-0000-000000000000")
    assert done["status"] == "failed"
    assert "RAG__EMBEDDINGS_MODEL" in done["error"]


def test_another_tenants_corpus_is_refused() -> None:
    async def scenario() -> None:
        store = MemoryCorpusStore()
        corpus = await store.create_corpus("acme", "private", "voyageai:voyage-4")
        await store.add_documents(
            "acme", corpus.corpus_id, [NewDocument("a.md", "text/markdown", b"# A\n\nsecret\n")]
        )
        graph = workspace.build(
            build_settings(rag=RAG),
            corpora=store,
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
        )
        ctx = AgentContext(
            tenant_id="evil", user_id="u", request_id="r", run_id="x", agent="workspace"
        )
        config: Any = {"configurable": {"thread_id": "t"}}
        with pytest.raises(workspace.CorpusUnavailableError, match="Unknown corpus"):
            await graph.ainvoke(
                {"messages": [HumanMessage(SEARCH)], "corpus_id": corpus.corpus_id},
                config,
                context=ctx,
            )
        messages = (await graph.aget_state(config)).values.get("messages", [])
        assert not any(isinstance(m, ToolMessage) for m in messages)

    asyncio.run(scenario())


def test_an_unsupported_sentence_is_rewritten(
    rag: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        ScriptedChatModel.structured_scripts,
        "Verification",
        lambda _t: {"issues": [{"index": 0, "verdict": "unsupported", "note": "not in passages"}]},
    )
    monkeypatch.setitem(
        ScriptedChatModel.structured_scripts,
        "Revision",
        lambda _t: {
            "rewrites": [{"index": 0, "text": "Clients reconnect with Last-Event-ID [1]."}]
        },
    )
    corpus_id = corpus_with_doc(rag)
    thread_id = new_thread(rag, "workspace")
    ask(rag, thread_id, SEARCH, corpus_id)
    values = rag.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    assert values["report"].startswith("Clients reconnect with Last-Event-ID [1].")
    assert values["metadata"]["verification"]["revised"] == 1


# -- the pieces ---------------------------------------------------------------------


def _search(call_id: str, query: str, *sources: tuple[str, str]) -> list[Any]:
    return [
        AIMessage(
            content="",
            tool_calls=[{"id": call_id, "name": "search_documents", "args": {"query": query}}],
        ),
        ToolMessage(
            content=[
                {
                    "type": "search_result",
                    "source": url,
                    "title": title,
                    "content": [{"type": "text", "text": f"text of {title}"}],
                    "citations": {"enabled": True},
                }
                for url, title in sources
            ],
            tool_call_id=call_id,
        ),
    ]


def _cites(url: str, text: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": text,
        "citations": [{"type": "search_result_location", "source": url, "cited_text": text}],
    }


def test_numbers_follow_the_order_the_reply_cites() -> None:
    messages = [
        HumanMessage("q"),
        *_search("c1", "streams", ("doc://a#0", "A"), ("doc://b#1", "B")),
    ]
    reply = AIMessage(
        content=[
            {"type": "thinking", "thinking": "hmm"},
            _cites("doc://b#1", "B says one thing"),
            {"type": "text", "text": ", and "},
            _cites("doc://a#0", "A says another"),
            {"type": "text", "text": "."},
        ]
    )
    evidence = Evidence.from_thread([*messages, reply])
    body, cited = evidence.render(reply)
    assert body == "B says one thing [1], and A says another [2]."
    assert cited == ["doc://b#1", "doc://a#0"]
    findings, numbers = evidence.findings(cited, current_turn_from=0)
    assert numbers == {"doc://b#1": 1, "doc://a#0": 2}
    assert [f["question"] for f in findings] == ["streams"]


def test_typed_marks_are_not_citations() -> None:
    reply = AIMessage(content=[{"type": "text", "text": "Streams resume [3] on reconnect."}])
    body, cited = Evidence.from_thread([HumanMessage("q"), reply]).render(reply)
    assert body == "Streams resume on reconnect."
    assert cited == []


def test_earlier_searches_count_only_when_cited() -> None:
    first = [HumanMessage("q1"), *_search("c1", "old", ("doc://a#0", "A"))]
    second = [HumanMessage("q2"), *_search("c2", "new", ("doc://b#0", "B"))]
    messages = [*first, AIMessage("earlier answer"), *second]
    turn = len(first) + 1
    reply = AIMessage(content=[_cites("doc://b#0", "B")])
    evidence = Evidence.from_thread([*messages, reply])
    _, cited = evidence.render(reply)
    findings, _ = evidence.findings(cited, current_turn_from=turn)
    assert [f["question"] for f in findings] == ["new"]

    reply = AIMessage(content=[_cites("doc://a#0", "A")])
    evidence = Evidence.from_thread([*messages, reply])
    _, cited = evidence.render(reply)
    findings, _ = evidence.findings(cited, current_turn_from=turn)
    assert [f["question"] for f in findings] == ["old", "new"]


def test_passages_that_share_a_heading_are_told_apart() -> None:
    from waypoint.agents.graphs.workspace import _source_list

    sources = {
        "doc://d#7": {
            "url": "doc://d#7",
            "title": "02-runs.md > 2.4 Reconnection",
            "kind": "document",
        },
        "doc://d#6": {
            "url": "doc://d#6",
            "title": "02-runs.md > 2.4 Reconnection",
            "kind": "document",
        },
        "doc://e#0": {"url": "doc://e#0", "title": "ARCHITECTURE.md > 2. Runs", "kind": "document"},
    }
    numbers = {"doc://e#0": 1, "doc://d#7": 2, "doc://d#6": 3}
    listing = _source_list("A [1]. B [2]. C [3].", numbers, sources)  # type: ignore[arg-type]
    assert listing.split("\n")[3:] == [
        "[1] ARCHITECTURE.md > 2. Runs (document)",
        "[2] 02-runs.md > 2.4 Reconnection (part 2) (document)",
        "[3] 02-runs.md > 2.4 Reconnection (part 1) (document)",
    ]


# -- several corpora (T10, T11) and global corpora (T12) ------------------------------

OTHER = b"""# Budgets

## Daily cap

A tenant over its daily dollar budget gets a 429 with Retry-After.
"""


def corpus_with(client: TestClient, name: str, filename: str, body: bytes) -> str:
    corpus_id = str(client.post("/api/v1/corpora", json={"name": name}).json()["corpus_id"])
    files = [("files", (filename, body, "text/markdown"))]
    upload = client.post(f"/api/v1/corpora/{corpus_id}/documents", files=files).json()
    assert read_sse(client, upload["events_url"])[-1] == "run.completed"
    return corpus_id


def test_one_search_covers_every_selected_corpus(rag: TestClient) -> None:
    streams = corpus_with(rag, "streams", "streams.md", DOC)
    budgets = corpus_with(rag, "budgets", "budgets.md", OTHER)
    thread_id = new_thread(rag, "workspace")
    body = {"message": SEARCH, "input": {"corpus_ids": [streams, budgets]}}
    assert run_and_wait(rag, thread_id, body)["status"] == "completed"
    values = rag.get(f"/api/v1/threads/{thread_id}/state").json()["values"]
    assert [c["name"] for c in values["corpora"]] == ["streams", "budgets"]
    tool = next(m for m in values["messages"] if m.get("type") == "tool")
    assert "streams.md" in tool["content"]
    assert "budgets.md" in tool["content"]  # passages from both corpora, one search
    assert values["metadata"]["searches"] == 1


def test_a_thread_selects_at_most_the_configured_number(make_app: Any) -> None:
    client = make_app(rag=RAG, workspace={"max_corpora": 1}).test_client()
    a = corpus_with(client, "a", "a.md", DOC)
    b = corpus_with(client, "b", "b.md", OTHER)
    thread_id = new_thread(client, "workspace")
    done = run_and_wait(client, thread_id, {"message": "hi", "input": {"corpus_ids": [a, b]}})
    assert done["status"] == "failed"
    assert "WORKSPACE__MAX_CORPORA" in done["error"]


def test_an_admin_publishes_a_corpus_to_every_tenant() -> None:
    async def scenario() -> None:
        store = MemoryCorpusStore()
        mine = await store.create_corpus("admin", "handbook", "voyageai:voyage-4")
        assert await store.get_corpus("acme", mine.corpus_id) is None  # private: invisible
        published = await store.set_visibility("admin", mine.corpus_id, "public")
        assert published is not None
        assert published.visibility == "public"
        assert await store.get_corpus("acme", mine.corpus_id) is not None
        assert await store.set_visibility("acme", mine.corpus_id, "private") is None  # not theirs
        assert (await store.set_visibility("admin", mine.corpus_id, "private")) is not None
        assert await store.get_corpus("acme", mine.corpus_id) is None

    asyncio.run(scenario())


def test_the_visibility_endpoint(rag: TestClient) -> None:
    corpus_id = str(rag.post("/api/v1/corpora", json={"name": "handbook"}).json()["corpus_id"])
    response = rag.post(f"/api/v1/corpora/{corpus_id}/visibility", json={"visibility": "public"})
    assert response.status_code == 200, response.json()
    assert response.json()["visibility"] == "public"
    unknown = "00000000-0000-0000-0000-000000000000"
    response = rag.post(f"/api/v1/corpora/{unknown}/visibility", json={"visibility": "public"})
    assert response.status_code == 404


def test_cited_passages_are_never_cut_from_the_evidence() -> None:
    """A cited passage past the evidence budget stays, whole (docs-eval-costs, 2026-10-14)."""
    from waypoint.agents.graphs.workspace import MAX_EVIDENCE_CHARS, _evidence
    from waypoint.agents.state import Source

    long = Source(url="doc://a#1", title="a", kind="document")
    cited = Source(url="doc://b#1", title="b", kind="document")
    other = Source(url="doc://c#1", title="c", kind="document")
    texts = ["a: " + "x" * MAX_EVIDENCE_CHARS, "b: the verifier runs on Haiku", "c: unrelated"]
    evidence = _evidence([long, cited, other], texts, {cited["url"]})
    assert "b: the verifier runs on Haiku" in evidence
    assert "c: unrelated" not in evidence  # no room left for uncited passages
    assert evidence.splitlines()[0].endswith("…")  # the uncited one was cut, not dropped
