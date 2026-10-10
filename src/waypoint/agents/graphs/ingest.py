"""The `ingest` agent: documents into a corpus (design decision R6).

Ingestion is a run, not a request, for the same reasons research is: a big PDF
takes a while, and a run already has durability, an event stream, cancellation,
audit and per-tenant limits. ``POST /corpora/{id}/documents`` stores the files
and starts this graph with their ids; nothing large travels through the graph
state or its checkpoints.

    START -> ingest_document (one per document, in parallel) -> summarize -> END

Each document goes parse -> chunk -> embed -> store (``rag/index.py``) and
reports every stage as a ``custom`` event. A document that fails is marked
``failed`` with the reason and the others carry on; the run fails only on a
bug or a cancellation. A document's chunks are written in one transaction, so
a cancelled run leaves each document either fully indexed or still ``pending``.

No node here is cached: every node has side effects.
"""

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import get_runtime
from langgraph.types import Send

from waypoint.agents.context import AgentContext
from waypoint.agents.models import build_rag_embeddings
from waypoint.agents.types import Graph
from waypoint.rag.index import IndexResult, index_document
from waypoint.rag.store import CorpusStore
from waypoint.settings import Settings

ROLES: tuple[str, ...] = ()
"""No model calls yet. Contextual retrieval (phase 4) adds a ``contextualizer``."""


class IngestState(TypedDict, total=False):
    corpus_id: str
    document_ids: list[str]
    results: Annotated[list[IndexResult], operator.add]
    metadata: dict[str, Any]
    """The summary. Named ``metadata`` so ``run.completed`` carries it."""


class DocumentTask(TypedDict):
    document_id: str


def build(
    settings: Settings, *, corpora: CorpusStore | None = None, **compile_kwargs: Any
) -> Graph:
    embeddings = build_rag_embeddings(settings)

    def fan_out(state: IngestState) -> list[Send] | str:
        ids = list(dict.fromkeys(state.get("document_ids") or []))
        return [Send("ingest_document", DocumentTask(document_id=i)) for i in ids] or "summarize"

    async def ingest_document(state: DocumentTask) -> dict[str, Any]:
        runtime = get_runtime(AgentContext)
        result = await index_document(
            corpora,
            embeddings,
            tenant_id=runtime.context.tenant_id,
            document_id=state["document_id"],
            embed_model=settings.rag.embeddings_model,
            chunk_tokens=settings.rag.chunk_tokens,
            emit=runtime.stream_writer,
        )
        return {"results": [result]}

    def summarize(state: IngestState) -> dict[str, Any]:
        results = state.get("results") or []
        ready = [r for r in results if r["status"] == "ready"]
        return {
            "metadata": {
                "corpus_id": state.get("corpus_id"),
                "documents": len(results),
                "ready": len(ready),
                "failed": len(results) - len(ready),
                "chunks": sum(r["chunks"] for r in ready),
                "results": results,
            }
        }

    builder = StateGraph(IngestState, context_schema=AgentContext)
    builder.add_node("ingest_document", ingest_document, input_schema=DocumentTask)
    builder.add_node("summarize", summarize)
    builder.add_conditional_edges(START, fan_out, ["ingest_document", "summarize"])
    builder.add_edge("ingest_document", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(name="ingest", **compile_kwargs)
