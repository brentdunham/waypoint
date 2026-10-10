"""The research agent with a corpus: retrieval into search_result blocks,
document citations, the approval switch, and tenant checks. Unit tier.

The scripted model cites the first search_result block it is given, the way
the API returns a search_result_location citation, so the whole path from
retrieval to the briefing's source list runs offline.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from starlette.testclient import TestClient
from tests.conftest import TestApp, build_settings, new_thread, read_sse, run_and_wait

from waypoint.agents.citations import answer_and_sources, number_sources, strip_typed_markers
from waypoint.agents.context import AgentContext
from waypoint.agents.graphs import research
from waypoint.agents.models import ScriptedChatModel
from waypoint.rag.retrieve import Hit
from waypoint.rag.store import MemoryCorpusStore, NewDocument, StoredChunk

RAG = {"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 64}
DOC = b"""# Streams

## Reconnection

A client that loses the stream reconnects with Last-Event-ID and resumes.

## Storage

Events live in a Redis Stream with a one hour TTL after the run ends.
"""


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


def start(client: TestClient, corpus_id: str | None) -> tuple[str, dict[str, Any]]:
    thread_id = new_thread(client, "research")
    body: dict[str, Any] = {"message": "how stream recovery works"}
    if corpus_id:
        body["input"] = {"corpus_id": corpus_id}
    return thread_id, run_and_wait(client, thread_id, body)


def test_a_briefing_cites_the_tenants_documents(rag: TestClient) -> None:
    corpus_id = corpus_with_doc(rag)
    thread_id, paused = start(rag, corpus_id)
    assert paused["status"] == "interrupted"
    card = paused["interrupts"][0]["value"]
    assert card["corpus"] == {"corpus_id": corpus_id, "name": "docs"}

    done = run_and_wait(rag, thread_id, {"resume": {"approved": True}})
    assert done["status"] == "completed"
    findings = done["result"]["findings"]
    documents = [s for f in findings for s in f["sources"] if s.get("kind") == "document"]
    assert documents, "every scripted worker cites its first passage"
    assert all(s["url"].startswith("doc://") for s in documents)
    assert all(s["title"].startswith("streams.md") for s in documents)
    assert all(s["quotes"] for s in documents)  # the cited_text, kept for the verifier
    report = done["result"]["report"]
    assert "(document)" in report.split("\n\nSources\n", 1)[1]
    assert done["result"]["metadata"]["corpus"]["name"] == "docs"
    assert done["result"]["metadata"]["document_source_count"] >= 1


def test_approval_can_switch_documents_off(rag: TestClient) -> None:
    corpus_id = corpus_with_doc(rag)
    thread_id, _ = start(rag, corpus_id)
    done = run_and_wait(rag, thread_id, {"resume": {"approved": True, "documents": False}})
    sources = [s for f in done["result"]["findings"] for s in f["sources"]]
    assert sources
    assert not any(s.get("kind") == "document" for s in sources)
    assert "corpus" not in done["result"]["metadata"]


def test_web_only_runs_are_unchanged(rag: TestClient) -> None:
    thread_id, paused = start(rag, None)
    assert paused["interrupts"][0]["value"]["corpus"] is None
    done = run_and_wait(rag, thread_id, {"resume": {"approved": True}})
    sources = [s for f in done["result"]["findings"] for s in f["sources"]]
    assert sources
    assert all("kind" not in s for s in sources)  # findings keep their old shape


def test_an_unknown_corpus_fails_the_run(rag: TestClient) -> None:
    _, result = start(rag, "00000000-0000-0000-0000-000000000000")
    assert result["status"] == "failed"
    assert "Unknown corpus" in result["error"]


def test_documents_need_rag_on(client: TestClient) -> None:
    _, result = start(client, "00000000-0000-0000-0000-000000000000")
    assert result["status"] == "failed"
    assert "RAG__EMBEDDINGS_MODEL" in result["error"]


def test_another_tenants_corpus_is_refused_before_any_search() -> None:
    async def scenario() -> None:
        store = MemoryCorpusStore()
        corpus = await store.create_corpus("acme", "private", "voyageai:voyage-4")
        await store.add_documents(
            "acme", corpus.corpus_id, [NewDocument("a.md", "text/markdown", b"# A\n\nsecret\n")]
        )
        graph = research.build(
            build_settings(rag=RAG),
            corpora=store,
            checkpointer=InMemorySaver(),
            store=InMemoryStore(),
        )
        ctx = AgentContext(
            tenant_id="evil", user_id="u", request_id="r", run_id="x", agent="research"
        )
        config: Any = {"configurable": {"thread_id": "t"}}
        with pytest.raises(research.CorpusUnavailableError, match="Unknown corpus"):
            await graph.ainvoke({"topic": "t", "corpus_id": corpus.corpus_id}, config, context=ctx)
        assert not (await graph.aget_state(config)).values.get("plan")

    asyncio.run(scenario())


# -- the pieces -----------------------------------------------------------------------


def test_a_retrieved_chunk_becomes_a_search_result_block() -> None:
    hit = Hit(StoredChunk("d1", 3, "DEPLOY.md > 4. Caddy", ("first para", "second para")), 0.9)
    block = research.search_result(hit)
    assert block == {
        "type": "search_result",
        "source": "doc://d1#3",
        "title": "DEPLOY.md > 4. Caddy",
        "content": [
            {"type": "text", "text": "first para"},
            {"type": "text", "text": "second para"},
        ],
        "citations": {"enabled": True},
    }


def test_document_and_web_citations_share_one_numbering() -> None:
    reply = AIMessage(
        content=[
            {
                "type": "text",
                "text": "Caddy terminates TLS.",
                "citations": [
                    {
                        "type": "search_result_location",
                        "source": "doc://d1#3",
                        "title": "DEPLOY.md > 4. Caddy",
                        "cited_text": "Caddy terminates TLS for the demo.",
                        "search_result_index": 0,
                        "start_block_index": 0,
                        "end_block_index": 0,
                    }
                ],
            },
            {
                "type": "text",
                "text": " So do most reverse proxies.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://example.com/proxies",
                        "title": "Proxies",
                        "cited_text": "Most reverse proxies terminate TLS.",
                    }
                ],
            },
        ]
    )
    answer, sources = answer_and_sources(reply)
    assert answer == "Caddy terminates TLS. So do most reverse proxies."
    assert sources[0] == {
        "url": "doc://d1#3",
        "title": "DEPLOY.md > 4. Caddy",
        "kind": "document",
        "quotes": ["Caddy terminates TLS for the demo."],
    }
    assert "kind" not in sources[1]
    finding: Any = {"sources": sources}
    assert number_sources([finding]) == {"doc://d1#3": 1, "https://example.com/proxies": 2}


def test_uncited_answers_fall_back_to_the_passages() -> None:
    hit = Hit(StoredChunk("d1", 0, "a.md", ("p",)), 0.5)
    assert research.passage_source(hit) == {
        "url": "doc://d1#0",
        "title": "a.md",
        "kind": "document",
    }


def test_critic_and_writer_see_documents_marked_and_web_sources_unchanged() -> None:
    doc: Any = {"url": "doc://d1#3", "title": "DEPLOY.md > 4. Caddy", "kind": "document"}
    web: Any = {"url": "https://example.com/proxies", "title": "Proxies"}
    finding: Any = {
        "question": "q",
        "answer": "a",
        "sources": [doc, web],
        "confidence": 0.9,
        "issue": "",
    }
    numbers = number_sources([finding])
    assert research._critic_source(doc) == "document: DEPLOY.md > 4. Caddy"
    assert research._critic_source(web) == "https://example.com/proxies"
    assert "[1] DEPLOY.md > 4. Caddy (document); [2] Proxies\n" in research._render_finding(
        finding, numbers
    )


def test_document_rules_only_reach_runs_with_a_corpus() -> None:
    # Web-only prompts must stay byte-identical so recorded calls still replay.
    for template in (research.WORKER, research.CRITIC, research.SYNTH):
        assert "{document_rule}" in template
    for rule in (research.DOCUMENT_RULE, research.CRITIC_DOCUMENTS, research.SYNTH_DOCUMENTS):
        assert rule.startswith("\n")


def _uncited_first(monkeypatch: pytest.MonkeyPatch, *, searched: bool) -> list[str]:
    """Worker replies carry no citations unless the prompt is the retry."""
    original = ScriptedChatModel._next
    prompts: list[str] = []

    def scripted(self: ScriptedChatModel, messages: Any) -> AIMessage:
        content = messages[-1].content
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        text = "\n".join(
            str(b["text"]) for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        )
        if not text.startswith("Answer this research question"):
            return original(self, messages)
        prompts.append(text)
        if "attached no citations" in text:
            return original(self, messages)
        reply: list[dict[str, Any]] = [{"type": "text", "text": "An answer with no citations."}]
        if searched:
            reply.insert(
                0,
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "s1",
                    "content": [
                        {"type": "web_search_result", "url": "https://a.example", "title": "A"}
                    ],
                },
            )
        return AIMessage(content=reply)

    monkeypatch.setattr(ScriptedChatModel, "_next", scripted)
    return prompts


def test_an_uncited_answer_over_passages_is_asked_again(
    rag: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus_id = corpus_with_doc(rag)
    prompts = _uncited_first(monkeypatch, searched=False)
    thread_id, _ = start(rag, corpus_id)
    done = run_and_wait(rag, thread_id, {"resume": {"approved": True}})
    findings = done["result"]["findings"]
    assert findings
    assert all(f.get("retried") for f in findings)
    assert all(f["cited"] for f in findings)
    assert all(any(s.get("kind") == "document" for s in f["sources"]) for f in findings)
    retries = [p for p in prompts if "attached no citations" in p]
    assert len(retries) == len(findings) == len(prompts) - len(retries)


def test_an_answer_with_nothing_to_cite_is_not_asked_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = _uncited_first(monkeypatch, searched=False)
    thread_id, _ = start(client, None)
    done = run_and_wait(client, thread_id, {"resume": {"approved": True}})
    findings = done["result"]["findings"]
    assert findings
    assert not any(f.get("retried") for f in findings)
    assert not any(f["cited"] for f in findings)
    assert not any("attached no citations" in p for p in prompts)


def test_an_uncited_answer_after_web_searches_is_asked_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _uncited_first(monkeypatch, searched=True)
    thread_id, _ = start(client, None)
    done = run_and_wait(client, thread_id, {"resume": {"approved": True}})
    findings = done["result"]["findings"]
    assert findings
    assert all(f.get("retried") for f in findings)
    assert all(f["cited"] for f in findings)


def test_the_retry_adds_the_rule_after_the_prompt_and_keeps_the_passages() -> None:
    assert research._with_recite("prompt") == "prompt" + research.RECITE_RULE
    passage = {"type": "search_result", "source": "doc://d#0"}
    out = research._with_recite([passage, {"type": "text", "text": "prompt"}])
    assert out == [passage, {"type": "text", "text": "prompt" + research.RECITE_RULE}]


def test_typed_markers_are_stripped_from_worker_answers() -> None:
    typed = "EventSource has no auth header ([3-1]). It cannot abort [2][4-2]. Fine."
    assert strip_typed_markers(typed) == "EventSource has no auth header. It cannot abort. Fine."
    assert (
        strip_typed_markers("No marks, and a list [a] stays.") == "No marks, and a list [a] stays."
    )
