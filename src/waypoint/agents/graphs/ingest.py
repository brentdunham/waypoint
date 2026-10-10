"""The `ingest` agent: documents into a corpus (design decision R6).

Ingestion is a run, not a request, for the same reasons research is: a big PDF
takes a while, and a run already has durability, an event stream, cancellation,
audit and per-tenant limits. ``POST /corpora/{id}/documents`` stores the files
and starts this graph with their ids; nothing large travels through the graph
state or its checkpoints.

    START -> ingest_document (one per document, in parallel) -> summarize -> END

Each document goes parse -> chunk -> embed -> store and reports every stage as
a ``custom`` event. A document that fails is marked ``failed`` with the reason
and the others carry on; the run fails only on a bug or a cancellation. A
document's chunks are written in one transaction, so a cancelled run leaves
each document either fully indexed or still ``pending``.

No node here is cached: every node has side effects.
"""

import asyncio
import logging
import operator
from collections.abc import Sequence
from typing import Annotated, Any, TypedDict

from langchain_core.embeddings import Embeddings
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import get_runtime
from langgraph.types import Send

from waypoint.agents.context import AgentContext
from waypoint.agents.models import build_rag_embeddings, http_status
from waypoint.agents.types import Graph
from waypoint.errors import NotFound
from waypoint.rag.chunk import chunk_sections
from waypoint.rag.parse import ParseError, parse
from waypoint.rag.store import CorpusStore
from waypoint.settings import Settings

log = logging.getLogger(__name__)

ROLES: tuple[str, ...] = ()
"""No model calls yet. Contextual retrieval (phase 4) adds a ``contextualizer``."""

EMBED_ATTEMPTS = 3
EMBED_BACKOFF_S = 1.0


class IngestResult(TypedDict):
    document_id: str
    title: str
    status: str
    """``ready`` or ``failed``."""
    chunks: int
    error: str | None


class IngestState(TypedDict, total=False):
    corpus_id: str
    document_ids: list[str]
    results: Annotated[list[IngestResult], operator.add]
    metadata: dict[str, Any]
    """The summary. Named ``metadata`` so ``run.completed`` carries it."""


class DocumentTask(TypedDict):
    document_id: str


class DocumentError(Exception):
    """This document cannot be indexed; the reason is shown to the person."""


def build(
    settings: Settings, *, corpora: CorpusStore | None = None, **compile_kwargs: Any
) -> Graph:
    embeddings = build_rag_embeddings(settings)

    def fan_out(state: IngestState) -> list[Send] | str:
        ids = list(dict.fromkeys(state.get("document_ids") or []))
        return [Send("ingest_document", DocumentTask(document_id=i)) for i in ids] or "summarize"

    async def ingest_document(task: DocumentTask) -> dict[str, Any]:
        runtime = get_runtime(AgentContext)
        tenant = runtime.context.tenant_id
        document_id = task["document_id"]
        emit = runtime.stream_writer
        title = document_id
        try:
            if corpora is None or embeddings is None:
                raise DocumentError("Document retrieval is off: RAG__EMBEDDINGS_MODEL is unset.")
            doc = await corpora.load_document(tenant, document_id)
            if doc is None:
                raise DocumentError("No such document.")
            title = doc.title
            if doc.embed_model != settings.rag.embeddings_model:
                raise DocumentError(
                    f"The corpus is embedded with {doc.embed_model} but the service is "
                    f"configured for {settings.rag.embeddings_model}; vectors from different "
                    "models cannot be searched together."
                )
            try:
                parsed = parse(doc.content, doc.media_type, doc.title)
            except ParseError as exc:
                raise DocumentError(str(exc)) from exc
            progress: dict[str, Any] = {"document_id": document_id, "title": title}
            emit(
                progress
                | {
                    "stage": "parsed",
                    "sections": len(parsed.sections),
                    "paragraphs": parsed.paragraph_count,
                }
            )
            chunks = chunk_sections(
                parsed.sections, doc.title, target_tokens=settings.rag.chunk_tokens
            )
            if not chunks:
                raise DocumentError("The document has no text to index.")
            emit(progress | {"stage": "chunked", "chunks": len(chunks)})
            vectors = await embed_with_retry(embeddings, [c.text for c in chunks])
            emit(progress | {"stage": "embedded"})
            try:
                stored = await corpora.write_chunks(tenant, document_id, chunks, vectors)
            except NotFound as exc:
                raise DocumentError("The document was deleted during ingestion.") from exc
        except DocumentError as exc:
            failed = await _failed(corpora, tenant, document_id, title, str(exc), emit)
            return {"results": [failed]}
        except EmbeddingError as exc:
            message = f"Embedding failed: {exc}"
            failed = await _failed(corpora, tenant, document_id, title, message, emit)
            return {"results": [failed]}
        emit({"document_id": document_id, "title": title, "stage": "stored", "chunks": stored})
        return {
            "results": [
                IngestResult(
                    document_id=document_id, title=title, status="ready", chunks=stored, error=None
                )
            ]
        }

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
    # mypy matches no add_node overload for this Send() target, with or without
    # input_schema; at runtime langgraph accepts it (and the tests run it).
    builder.add_node("ingest_document", ingest_document, input_schema=DocumentTask)  # type: ignore[call-overload]
    builder.add_node("summarize", summarize)
    builder.add_conditional_edges(START, fan_out, ["ingest_document", "summarize"])
    builder.add_edge("ingest_document", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(name="ingest", **compile_kwargs)


class EmbeddingError(RuntimeError):
    """The provider kept failing, or refused outright."""


async def embed_with_retry(
    embeddings: Embeddings,
    texts: Sequence[str],
    *,
    attempts: int = EMBED_ATTEMPTS,
    backoff_s: float = EMBED_BACKOFF_S,
) -> list[list[float]]:
    """``aembed_documents`` with a short exponential backoff on provider errors.

    A rejected key (401/403) or a bad request (400) is not retried: waiting
    does not fix it. Retrying here, not with a node ``RetryPolicy``, keeps the
    parse and chunk work from being redone and lets the last error be recorded
    on the document instead of failing the whole run.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await embeddings.aembed_documents(list(texts))
        except Exception as exc:
            status = http_status(exc)
            if status in {400, 401, 403} or attempt == attempts:
                raise EmbeddingError(f"{type(exc).__name__}: {exc}") from exc
            log.warning("embedding attempt %d failed; retrying", attempt, exc_info=True)
            await asyncio.sleep(backoff_s * 2 ** (attempt - 1))
    raise AssertionError("unreachable")  # pragma: no cover


async def _failed(  # noqa: PLR0917
    corpora: CorpusStore | None,
    tenant: str,
    document_id: str,
    title: str,
    error: str,
    emit: Any,
) -> IngestResult:
    if corpora is not None:
        await corpora.mark_failed(tenant, document_id, error)
    emit({"document_id": document_id, "title": title, "stage": "failed", "error": error})
    return IngestResult(
        document_id=document_id, title=title, status="failed", chunks=0, error=error
    )
