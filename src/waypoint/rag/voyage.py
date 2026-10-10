"""Voyage endpoints that langchain-voyageai does not wrap: contextualized chunk
embeddings (``voyage-context-4``) and reranking (``rerank-3``).

Both are phase 4 ablations of the RAG design (docs/design/rag.md). Prices and
limits are from docs.voyageai.com, checked 2026-10-10.
"""

from collections.abc import Sequence

from langchain_core.embeddings import Embeddings
from voyageai.client import Client
from voyageai.client_async import AsyncClient

from waypoint.rag.chunk import estimate_tokens
from waypoint.rag.retrieve import Reranked

CONTEXT_REQUEST_TOKENS = 24_000
"""Budget per contextualized-embedding request. Voyage allows 32K tokens per
request without auto-chunking; chunk sizes here are estimated (characters / 4),
so the budget leaves room for the estimate being low."""


def context_groups(texts: Sequence[str], budget: int = CONTEXT_REQUEST_TOKENS) -> list[list[str]]:
    """Consecutive runs of one document's chunks, each within ``budget`` tokens.

    Chunks in a group are embedded with each other as context. A document
    longer than the budget is split into several groups, each its own request:
    its chunks then see a window of the document, not all of it.
    """
    groups: list[list[str]] = []
    size = 0
    for text in texts:
        tokens = estimate_tokens(text)
        if groups and size + tokens <= budget:
            groups[-1].append(text)
            size += tokens
        else:
            groups.append([text])
            size = tokens
    return groups


class VoyageContextEmbeddings(Embeddings):
    """``voyage-context-4``: each chunk embedded with its document's other chunks.

    ``embed_documents`` takes **one document's chunks**, in order, which is how
    ``rag/index.py`` calls it (once per document). Queries are embedded alone
    with ``input_type="query"``, as Voyage's docs show.
    """

    def __init__(self, model: str, dims: int, api_key: str) -> None:
        self.model, self.dims = model, dims
        self._sync = Client(api_key=api_key, max_retries=3)
        self._async = AsyncClient(api_key=api_key, max_retries=3)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for group in context_groups(texts):
            result = self._sync.contextualized_embed(
                inputs=[group], model=self.model, input_type="document", output_dimension=self.dims
            )
            out.extend([list(map(float, e)) for e in result.results[0].embeddings])
        return out

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for group in context_groups(texts):
            result = await self._async.contextualized_embed(
                inputs=[group], model=self.model, input_type="document", output_dimension=self.dims
            )
            out.extend([list(map(float, e)) for e in result.results[0].embeddings])
        return out

    def embed_query(self, text: str) -> list[float]:
        result = self._sync.contextualized_embed(
            inputs=[[text]], model=self.model, input_type="query", output_dimension=self.dims
        )
        return list(map(float, result.results[0].embeddings[0]))

    async def aembed_query(self, text: str) -> list[float]:
        result = await self._async.contextualized_embed(
            inputs=[[text]], model=self.model, input_type="query", output_dimension=self.dims
        )
        return list(map(float, result.results[0].embeddings[0]))


class VoyageReranker:
    """A cross-encoder pass over the first stage's candidates."""

    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._client = AsyncClient(api_key=api_key, max_retries=3)

    async def rerank(self, query: str, documents: Sequence[str]) -> Reranked:
        if not documents:
            return Reranked([], 0)
        result = await self._client.rerank(query, list(documents), model=self.model)
        return Reranked(
            [(r.index, float(r.relevance_score)) for r in result.results], result.total_tokens
        )
