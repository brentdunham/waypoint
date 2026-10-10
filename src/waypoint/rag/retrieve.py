"""Search a corpus: vector, keyword, or both fused (design decision R3).

``retrieve`` returns more than the ranked chunks: for each hit, its rank and
score under every method that found it, and timings. The research worker
(phase 3) only needs the chunks; the retrieval eval needs the rest to say
which method found what.

Hybrid search uses reciprocal rank fusion: a chunk's fused score is the sum,
over the methods that ranked it, of ``1 / (rrf_k + rank)``. Only ranks are
used, so vector cosine similarities and ``ts_rank_cd`` values, which are on
unrelated scales, never have to be made comparable.

With a reranker (``RAG__RERANK``), any mode's top ``candidates`` go through a
second stage that reads the query and each chunk together, and its order
decides the final ``k``.
"""

import asyncio
import re
import time
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from langchain_core.embeddings import Embeddings

from waypoint.rag.store import ChunkHit, CorpusStore, StoredChunk
from waypoint.settings import RagSettings

type Mode = Literal["vector", "keyword", "hybrid"]
MODES: tuple[Mode, ...] = ("vector", "keyword", "hybrid")


@dataclass(frozen=True, slots=True)
class Hit:
    chunk: StoredChunk
    score: float
    """Fused RRF score in hybrid mode; the method's own score otherwise."""
    ranks: dict[str, int] = field(default_factory=dict)
    """Method -> 1-based rank, for each method that returned this chunk."""
    scores: dict[str, float] = field(default_factory=dict)
    """Method -> that method's own score."""

    @property
    def source(self) -> str:
        """The ``search_result`` source id: stable for a given chunking."""
        return f"doc://{self.chunk.document_id}#{self.chunk.ordinal}"


@dataclass(frozen=True, slots=True)
class Retrieval:
    hits: list[Hit]
    mode: Mode
    embed_s: float = 0.0
    """Time spent embedding the query (zero for keyword mode)."""
    search_s: float = 0.0
    """Time spent in the database after the query was embedded."""
    rerank_s: float = 0.0
    rerank_tokens: int = 0
    """Tokens the reranker billed for this query (0 without one)."""

    @property
    def elapsed_s(self) -> float:
        return self.embed_s + self.search_s + self.rerank_s


@dataclass(frozen=True, slots=True)
class Reranked:
    order: list[tuple[int, float]]
    """(index into the documents passed, relevance score), best first."""
    tokens: int
    """Billed tokens: query tokens x documents + document tokens."""


class Reranker(Protocol):
    async def rerank(self, query: str, documents: Sequence[str]) -> Reranked: ...


class WordOverlapReranker:
    """Offline stand-in for tests: more shared words ranks higher. Never measure
    retrieval with it."""

    async def rerank(self, query: str, documents: Sequence[str]) -> Reranked:
        terms = set(_WORD.findall(query.lower()))
        scores = [len(terms & set(_WORD.findall(d.lower()))) for d in documents]
        order = sorted(range(len(documents)), key=lambda i: (-scores[i], i))
        return Reranked([(i, float(scores[i])) for i in order], 0)


_WORD = re.compile(r"\w{3,}")


def rrf[K: Hashable](rankings: Mapping[str, Sequence[K]], *, k: int) -> dict[K, float]:
    """Reciprocal rank fusion of several best-first rankings of the same items."""
    fused: dict[K, float] = {}
    for ranking in rankings.values():
        for rank, item in enumerate(ranking, start=1):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank)
    return fused


def _key(chunk: StoredChunk) -> tuple[str, int]:
    return (chunk.document_id, chunk.ordinal)


async def retrieve(
    store: CorpusStore,
    embeddings: Embeddings | None,
    tenant_id: str,
    corpus_id: str,
    query: str,
    *,
    k: int,
    mode: Mode = "hybrid",
    candidates: int = 50,
    rrf_k: int = 60,
    reranker: Reranker | None = None,
) -> Retrieval:
    """The ``k`` best chunks for ``query`` in a corpus the tenant can read."""
    if mode != "keyword" and embeddings is None:
        raise ValueError(f"mode {mode!r} needs an embeddings model")

    pool = max(k, candidates) if mode == "hybrid" or reranker is not None else k
    first_k = max(k, candidates) if reranker is not None else k
    embed_s = 0.0
    vector: list[float] = []
    if mode != "keyword":
        assert embeddings is not None
        started = time.perf_counter()
        vector = await embeddings.aembed_query(query)
        embed_s = time.perf_counter() - started

    started = time.perf_counter()
    by_method: dict[str, list[ChunkHit]] = {}
    if mode == "vector":
        by_method["vector"] = await store.vector_search(tenant_id, corpus_id, vector, pool)
    elif mode == "keyword":
        by_method["keyword"] = await store.keyword_search(tenant_id, corpus_id, query, pool)
    else:
        found_v, found_k = await asyncio.gather(
            store.vector_search(tenant_id, corpus_id, vector, pool),
            store.keyword_search(tenant_id, corpus_id, query, pool),
        )
        by_method = {"vector": found_v, "keyword": found_k}
    search_s = time.perf_counter() - started

    chunks: dict[tuple[str, int], StoredChunk] = {}
    ranks: dict[tuple[str, int], dict[str, int]] = {}
    scores: dict[tuple[str, int], dict[str, float]] = {}
    for method, found in by_method.items():
        for rank, hit in enumerate(found, start=1):
            key = _key(hit.chunk)
            chunks.setdefault(key, hit.chunk)
            ranks.setdefault(key, {})[method] = rank
            scores.setdefault(key, {})[method] = hit.score

    if mode == "hybrid":
        fused = rrf({m: [_key(h.chunk) for h in f] for m, f in by_method.items()}, k=rrf_k)
        order = sorted(fused, key=lambda key: (-fused[key], key))
        final = {key: fused[key] for key in order[:first_k]}
    else:
        (method,) = by_method
        final = {key: scores[key][method] for key in [_key(h.chunk) for h in by_method[method]]}

    rerank_s, rerank_tokens = 0.0, 0
    if reranker is not None and final:
        keys = list(final)[:first_k]
        started = time.perf_counter()
        reranked = await reranker.rerank(query, [chunks[key].text for key in keys])
        rerank_s = time.perf_counter() - started
        rerank_tokens = reranked.tokens
        final = {}
        for rank, (index, relevance) in enumerate(reranked.order, start=1):
            key = keys[index]
            ranks[key]["rerank"] = rank
            scores[key]["rerank"] = relevance
            final[key] = relevance

    ranked = [Hit(chunks[key], score, ranks[key], scores[key]) for key, score in final.items()]
    return Retrieval(
        ranked[:k],
        mode,
        embed_s=embed_s,
        search_s=search_s,
        rerank_s=rerank_s,
        rerank_tokens=rerank_tokens,
    )


class Retriever:
    """``retrieve`` with the service's ``RAG__*`` defaults bound."""

    def __init__(
        self,
        store: CorpusStore,
        embeddings: Embeddings | None,
        settings: RagSettings,
        reranker: Reranker | None = None,
    ) -> None:
        self.store, self.embeddings, self.settings = store, embeddings, settings
        self.reranker = reranker

    async def search(
        self,
        tenant_id: str,
        corpus_id: str,
        query: str,
        *,
        k: int | None = None,
        mode: Mode | None = None,
    ) -> Retrieval:
        return await retrieve(
            self.store,
            self.embeddings,
            tenant_id,
            corpus_id,
            query,
            k=k or self.settings.top_k,
            mode=mode or self.settings.mode,
            candidates=self.settings.candidates,
            rrf_k=self.settings.rrf_k,
            reranker=self.reranker,
        )
