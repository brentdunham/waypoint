"""One document into chunks with embeddings: parse -> chunk -> embed -> write.

Shared by the ``ingest`` agent, which runs it once per uploaded document, and
the retrieval eval, which indexes its pinned corpus with exactly the same code
so that what it measures is what the service builds.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from langchain_core.embeddings import Embeddings

from waypoint.errors import NotFound, OverQuota, http_status
from waypoint.infra.quota import UploadQuota
from waypoint.rag.chunk import chunk_sections, estimate_tokens
from waypoint.rag.contextualize import Contextualizer, document_text
from waypoint.rag.parse import ParseError, parse
from waypoint.rag.store import CorpusStore
from waypoint.usage import report_embedding

log = logging.getLogger(__name__)

EMBED_ATTEMPTS = 3
EMBED_BACKOFF_S = 1.0

type Emit = Callable[[dict[str, Any]], None]


class IndexResult(TypedDict):
    document_id: str
    title: str
    status: str
    """``ready`` or ``failed``."""
    chunks: int
    error: str | None


class DocumentError(Exception):
    """This document cannot be indexed; the reason is shown to the person."""


class EmbeddingError(RuntimeError):
    """The provider kept failing, or refused outright."""


def _ignore(_event: dict[str, Any]) -> None:
    return None


async def index_document(
    corpora: CorpusStore | None,
    embeddings: Embeddings | None,
    *,
    tenant_id: str,
    document_id: str,
    embed_model: str | None,
    chunk_tokens: int,
    contextualizer: Contextualizer | None = None,
    quota: UploadQuota | None = None,
    emit: Emit = _ignore,
) -> IndexResult:
    """Index one stored document. Failures come back as a ``failed`` result.

    ``embed_model`` is the service's configured model; a corpus created with
    another one is refused, because vectors from two models cannot be searched
    together. With a ``contextualizer`` (``RAG__CONTEXTUAL``) each chunk gets a
    preamble before it is embedded. With a ``quota`` the chunks are counted
    against the tenant's daily limit once the document is chunked, before any
    model or embedding call: a document that would pass it fails with the
    reason and costs nothing (docs/design/tools.md, T13). Chunks counted for a
    document that then fails are given back. Progress goes to ``emit`` as
    ``{"stage": ...}`` events.
    """
    title = document_id
    counted = 0
    try:
        if corpora is None or embeddings is None:
            raise DocumentError("Document retrieval is off: RAG__EMBEDDINGS_MODEL is unset.")
        doc = await corpora.load_document(tenant_id, document_id)
        if doc is None:
            raise DocumentError("No such document.")
        title = doc.title
        if doc.embed_model != embed_model:
            raise DocumentError(
                f"The corpus is embedded with {doc.embed_model} but the service is "
                f"configured for {embed_model}; vectors from different models cannot "
                "be searched together."
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
        chunks = chunk_sections(parsed.sections, doc.title, target_tokens=chunk_tokens)
        if not chunks:
            raise DocumentError("The document has no text to index.")
        emit(progress | {"stage": "chunked", "chunks": len(chunks)})
        counted = await _count_chunks(quota, tenant_id, len(chunks))
        if contextualizer is not None:
            try:
                chunks = await contextualizer(document_text(chunks), chunks)
            except Exception as exc:
                raise DocumentError(
                    f"Contextual retrieval failed: {type(exc).__name__}: {exc}"
                ) from exc
            emit(progress | {"stage": "contextualized"})
        vectors = await embed_with_retry(embeddings, [c.text for c in chunks])
        # Estimated: langchain's embeddings interface returns vectors, not counts.
        report_embedding(embed_model, sum(estimate_tokens(c.text) for c in chunks))
        emit(progress | {"stage": "embedded"})
        try:
            stored = await corpora.write_chunks(tenant_id, document_id, chunks, vectors)
        except NotFound as exc:
            raise DocumentError("The document was deleted during ingestion.") from exc
    except (DocumentError, EmbeddingError) as exc:
        error = f"Embedding failed: {exc}" if isinstance(exc, EmbeddingError) else str(exc)
        if quota is not None:
            await quota.give_back_chunks(tenant_id, counted)
        if corpora is not None:
            await corpora.mark_failed(tenant_id, document_id, error)
        emit({"document_id": document_id, "title": title, "stage": "failed", "error": error})
        return IndexResult(
            document_id=document_id, title=title, status="failed", chunks=0, error=error
        )
    emit({"document_id": document_id, "title": title, "stage": "stored", "chunks": stored})
    return IndexResult(
        document_id=document_id, title=title, status="ready", chunks=stored, error=None
    )


async def _count_chunks(quota: UploadQuota | None, tenant_id: str, chunks: int) -> int:
    """Take ``chunks`` from the tenant's daily limit; how many were taken."""
    if quota is None:
        return 0
    try:
        await quota.take_chunks(tenant_id, chunks)
    except OverQuota as exc:
        raise DocumentError(exc.detail) from exc
    return chunks


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
