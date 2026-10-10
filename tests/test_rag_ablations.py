"""Phase 4 of the RAG design: contextual retrieval, contextualized embeddings
and reranking. Unit tier: the scripted model, hash vectors and a word-overlap
reranker stand in for the providers, so these test plumbing and arithmetic.
How much each technique helps is the retrieval eval's question.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from tests.conftest import build_settings

from evals import retrieval as rv
from evals.scorecard import fingerprint, rag_settings
from waypoint.agents.models import (
    EmbeddingsConfigError,
    HashEmbeddings,
    ScriptedChatModel,
    build_contextualizer,
    build_reranker,
    check_reranker,
)
from waypoint.rag.chunk import Chunk
from waypoint.rag.contextualize import Contextualizer, Usage, document_text
from waypoint.rag.index import index_document
from waypoint.rag.retrieve import WordOverlapReranker, retrieve
from waypoint.rag.store import MemoryCorpusStore, NewDocument, StoredChunk
from waypoint.rag.voyage import context_groups

RAG = {"embeddings_model": "voyageai:voyage-4", "embeddings_dims": 64}
DOC = b"""# Streams

## Reconnection

A client that loses the stream reconnects with Last-Event-ID and resumes.

## Storage

Events live in a Redis Stream with a one hour TTL after the run ends.
"""


def test_the_preamble_is_embedded_first_and_kept_out_when_empty() -> None:
    plain = Chunk(0, "a.md > Storage", ("Events live in Redis.",))
    assert plain.text == "a.md > Storage\n\nEvents live in Redis."
    framed = Chunk(0, "a.md > Storage", ("Events live in Redis.",), context="About the event log.")
    assert framed.text == "About the event log.\n\na.md > Storage\n\nEvents live in Redis."
    stored = StoredChunk("d", 0, "a.md > Storage", ("Events live in Redis.",), context="About it.")
    assert stored.text.startswith("About it.\n\na.md > Storage")


def test_the_document_shown_to_the_contextualizer_has_no_repeated_overlap() -> None:
    chunks = [
        Chunk(0, "a.md > S", ("one", "two")),
        Chunk(1, "a.md > S", ("two", "three")),  # starts with the carried-over paragraph
        Chunk(2, "a.md > T", ("four",)),
    ]
    assert document_text(chunks) == "a.md > S\n\none\n\ntwo\n\nthree\n\na.md > T\n\nfour"


def test_the_contextualizer_writes_one_preamble_per_chunk() -> None:
    chunks = [Chunk(0, "a.md > S", ("one",)), Chunk(1, "a.md > T", ("two",))]
    contextualizer = Contextualizer(ScriptedChatModel(), "scripted")
    out = asyncio.run(contextualizer(document_text(chunks), chunks))
    assert [c.context for c in out] == [
        "Scripted context: this chunk is from a.md > S.",
        "Scripted context: this chunk is from a.md > T.",
    ]
    assert [c.paragraphs for c in out] == [c.paragraphs for c in chunks]
    assert contextualizer.usage.calls == 2


def test_the_document_is_marked_for_caching_and_sent_first() -> None:
    seen: list[Any] = []

    class Recorder:
        async def ainvoke(self, messages: Any, /) -> AIMessage:
            seen.append(messages)
            return AIMessage(content="ctx")

    chunks = [Chunk(0, "a.md > S", ("one",))]
    asyncio.run(Contextualizer(Recorder(), "m")("the whole document", chunks))
    [[message]] = seen
    first, second = message["content"]
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["text"] == "<document>\nthe whole document\n</document>"
    assert "<chunk>\na.md > S\n\none\n</chunk>" in second["text"]


def test_usage_separates_cached_from_uncached_input() -> None:
    usage = Usage()
    usage.add(
        {
            "input_tokens": 1_100,  # langchain's total, cache included
            "output_tokens": 40,
            "input_token_details": {"cache_read": 1_000, "cache_creation": 0},
        }
    )
    usage.add({"input_tokens": 1_100, "output_tokens": 30, "input_token_details": {}})
    usage.add(  # the first call of a document writes the cache, reported per TTL
        {
            "input_tokens": 1_100,
            "output_tokens": 30,
            "input_token_details": {
                "cache_read": 0,
                "cache_creation": 0,
                "ephemeral_5m_input_tokens": 1_000,
            },
        }
    )
    assert usage.calls == 3
    assert usage.input_tokens == 100 + 1_100 + 100
    assert usage.cache_read_tokens == 1_000
    assert usage.cache_write_tokens == 1_000
    assert usage.output_tokens == 100


def test_context_cost_prices_cache_reads_and_writes() -> None:
    usage = {
        "calls": 2,
        "input_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000,
        "cache_write_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    }
    price = {"input": 0.10, "output": 0.50}
    # 1M plain + 1M x 0.1 read + 1M x 1.25 write, at $0.10; 1M out at $0.50
    assert rv._context_cost(usage, price) == pytest.approx(0.10 * 2.35 + 0.50)
    assert rv._context_cost(None, price) is None


def test_ingesting_with_context_stores_and_searches_the_preamble() -> None:
    async def scenario() -> None:
        store, embeddings = MemoryCorpusStore(), HashEmbeddings(dims=16)
        corpus = await store.create_corpus("acme", "c", "voyageai:voyage-4")
        [added] = await store.add_documents(
            "acme", corpus.corpus_id, [NewDocument("streams.md", "text/markdown", DOC)]
        )
        events: list[dict[str, Any]] = []
        result = await index_document(
            store,
            embeddings,
            tenant_id="acme",
            document_id=added.document.document_id,
            embed_model="voyageai:voyage-4",
            chunk_tokens=400,
            contextualizer=Contextualizer(ScriptedChatModel(), "scripted"),
            emit=events.append,
        )
        assert result["status"] == "ready"
        assert "contextualized" in [e["stage"] for e in events]
        chunks = await store.list_chunks("acme", corpus.corpus_id)
        assert all(c.context.startswith("Scripted context:") for c in chunks)
        # "scripted" appears only in the preambles, and keyword search finds it.
        found = await retrieve(
            store, None, "acme", corpus.corpus_id, "scripted", k=5, mode="keyword"
        )
        assert len(found.hits) == len(chunks)

    asyncio.run(scenario())


def test_a_failing_contextualizer_fails_the_document_not_the_run() -> None:
    class Broken:
        async def ainvoke(self, messages: Any, /) -> AIMessage:
            raise RuntimeError("overloaded")

    async def scenario() -> None:
        store = MemoryCorpusStore()
        corpus = await store.create_corpus("acme", "c", "m")
        [added] = await store.add_documents(
            "acme", corpus.corpus_id, [NewDocument("a.md", "text/markdown", DOC)]
        )
        result = await index_document(
            store,
            HashEmbeddings(dims=8),
            tenant_id="acme",
            document_id=added.document.document_id,
            embed_model="m",
            chunk_tokens=400,
            contextualizer=Contextualizer(Broken(), "m"),
        )
        assert result["status"] == "failed"
        assert result["error"] == "Contextual retrieval failed: RuntimeError: overloaded"

    asyncio.run(scenario())


def test_the_reranker_reorders_the_first_stage_and_records_its_rank() -> None:
    async def scenario() -> None:
        store, embeddings = MemoryCorpusStore(), HashEmbeddings(dims=16)
        corpus = await store.create_corpus("acme", "c", "m")
        [added] = await store.add_documents(
            "acme", corpus.corpus_id, [NewDocument("a.md", "text/markdown", b"x")]
        )
        texts = [
            "Caddy terminates TLS.",
            "Redis Streams hold events.",
            "Resume with Last-Event-ID.",
        ]
        chunks = [Chunk(i, f"a.md > {i}", (t,)) for i, t in enumerate(texts)]
        await store.write_chunks(
            "acme",
            added.document.document_id,
            chunks,
            embeddings.embed_documents([c.text for c in chunks]),
        )
        result = await retrieve(
            store,
            embeddings,
            "acme",
            corpus.corpus_id,
            "how do Redis Streams hold events",
            k=2,
            mode="vector",
            candidates=3,
            reranker=WordOverlapReranker(),
        )
        assert len(result.hits) == 2
        top = result.hits[0]
        assert top.chunk.ordinal == 1
        assert top.ranks["rerank"] == 1
        assert "vector" in top.ranks  # the first stage's rank is kept beside it
        assert result.rerank_s >= 0

    asyncio.run(scenario())


def test_contextualized_requests_stay_within_the_token_budget() -> None:
    texts = ["x" * 400] * 10  # 100 estimated tokens each
    groups = context_groups(texts, budget=350)
    assert [len(g) for g in groups] == [3, 3, 3, 1]
    assert [t for g in groups for t in g] == texts
    assert context_groups(["y" * 4_000], budget=100) == [["y" * 4_000]]  # never dropped


def test_reranker_and_contextualizer_follow_the_settings() -> None:
    off = build_settings(rag=RAG | {"rerank": False})
    assert build_reranker(off) is None
    assert build_contextualizer(off) is None
    assert build_reranker(build_settings()) is None  # RAG itself off
    on = build_settings(rag=RAG | {"contextual": True})  # rerank is on by default
    assert isinstance(build_reranker(on), WordOverlapReranker)
    contextualizer = build_contextualizer(on)
    assert contextualizer is not None
    assert contextualizer.model_name == "claude-haiku-5-5"
    live = build_settings(fake_model=False, rag=RAG | {"rerank": True})
    with pytest.raises(EmbeddingsConfigError, match="VOYAGE_API_KEY"):
        build_reranker(live)


def test_a_rejected_rerank_key_stops_the_boot() -> None:
    class RejectedError(Exception):
        status_code = 401

    class Refusing:
        async def rerank(self, query: str, documents: Any) -> Any:
            raise RejectedError("bad key")

    class Flaky:
        async def rerank(self, query: str, documents: Any) -> Any:
            raise ConnectionError("timeout")

    with pytest.raises(EmbeddingsConfigError, match="401"):
        asyncio.run(check_reranker(Refusing()))
    asyncio.run(check_reranker(Flaky()))  # a network blip is only logged


def test_new_settings_enter_the_research_fingerprint_only_when_they_change_behaviour() -> None:
    plain = build_settings(rag=RAG | {"rerank": False})  # as before phase 4
    assert "rerank" not in (rag_settings(plain, "research") or {})
    models = {"planner": "m"}
    reranked = build_settings(rag=RAG)  # the default now reranks
    assert rag_settings(reranked, "research")["rerank"] is True  # type: ignore[index]
    assert fingerprint(plain, "research", models) != fingerprint(reranked, "research", models)
    assert fingerprint(plain, "research", models) == fingerprint(
        build_settings(rag=RAG | {"rerank": False, "rerank_model": "rerank-3"}),
        "research",
        models,
    )


def test_stable_ids_repeat_across_stores_so_eval_prompts_replay() -> None:
    async def ids(stable: bool) -> tuple[str, str]:
        store = MemoryCorpusStore(stable_ids=stable)
        corpus = await store.create_corpus("evals", "waypoint-docs-abc", "m")
        [added] = await store.add_documents(
            "evals", corpus.corpus_id, [NewDocument("docs/API.md", "text/markdown", b"x")]
        )
        return corpus.corpus_id, added.document.document_id

    assert asyncio.run(ids(True)) == asyncio.run(ids(True))
    assert asyncio.run(ids(False)) != asyncio.run(ids(False))
