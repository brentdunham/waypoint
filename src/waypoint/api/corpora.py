"""Corpora: named sets of a tenant's documents that retrieval searches.

Uploading is two steps on the server and one for the client: the files are
stored (``documents``, status ``pending``) and an ``ingest`` run is started on
them. The response is ``202`` with that run's id; follow it on its events URL
like any other run. A file whose exact bytes are already in the corpus is not
ingested again.

Creating, deleting and uploading need the ``corpora:write`` scope; listing and
searching do not. The demo's reviewer keys lack it: they can ask the public
corpus, and add nothing to the server.
"""

import uuid
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile

from waypoint.agents.runtime import AgentRuntime, get_runtime
from waypoint.api.deps import CorpusWriter, Reader
from waypoint.errors import BadRequest, NotFound, PayloadTooLarge, ServiceUnavailable
from waypoint.rag.parse import MEDIA_TYPES, media_type_for
from waypoint.rag.store import NewDocument
from waypoint.schemas import (
    SCOPE_RUN,
    SCOPE_WRITE,
    CorpusList,
    CorpusOut,
    CreateCorpus,
    DocumentList,
    DocumentOut,
    UploadAccepted,
    UploadedDocument,
)

router = APIRouter(prefix="/corpora", tags=["corpora"])

MAX_FILES = 100
INGEST_AGENT = "ingest"


def _rag_runtime(request: Request) -> AgentRuntime:
    runtime = get_runtime(request)
    if not runtime.settings.rag.enabled:
        raise ServiceUnavailable(
            "Document retrieval is off on this server (RAG__EMBEDDINGS_MODEL is unset)."
        )
    return runtime


Rag = Annotated[AgentRuntime, Depends(_rag_runtime)]


@router.post("", status_code=201)
async def create(payload: CreateCorpus, runtime: Rag, principal: CorpusWriter) -> CorpusOut:
    embed_model = runtime.settings.rag.embeddings_model
    assert embed_model is not None  # _rag_runtime checked
    corpus = await runtime.corpora.create_corpus(principal.tenant_id, payload.name, embed_model)
    return CorpusOut.model_validate(corpus.public())


@router.get("")
async def index(runtime: Rag, principal: Reader) -> CorpusList:
    """Your corpora, then public ones."""
    corpora = await runtime.corpora.list_corpora(principal.tenant_id)
    return CorpusList(corpora=[CorpusOut.model_validate(c.public()) for c in corpora])


@router.get("/{corpus_id}")
async def show(corpus_id: str, runtime: Rag, principal: Reader) -> CorpusOut:
    corpus = await runtime.corpora.get_corpus(principal.tenant_id, corpus_id)
    if corpus is None:
        raise NotFound(f"Unknown corpus {corpus_id!r}.")
    return CorpusOut.model_validate(corpus.public())


@router.delete("/{corpus_id}", status_code=204)
async def destroy(corpus_id: str, runtime: Rag, principal: CorpusWriter) -> Response:
    """Deletes its documents and chunks too. Public corpora cannot be deleted here."""
    if not await runtime.corpora.delete_corpus(principal.tenant_id, corpus_id):
        raise NotFound(f"Unknown corpus {corpus_id!r}.")
    return Response(status_code=204)


@router.get("/{corpus_id}/documents")
async def documents(corpus_id: str, runtime: Rag, principal: Reader) -> DocumentList:
    if await runtime.corpora.get_corpus(principal.tenant_id, corpus_id) is None:
        raise NotFound(f"Unknown corpus {corpus_id!r}.")
    docs = await runtime.corpora.list_documents(principal.tenant_id, corpus_id)
    return DocumentList(documents=[DocumentOut.model_validate(d.public()) for d in docs])


@router.post("/{corpus_id}/documents", status_code=202)
async def upload(
    corpus_id: str,
    request: Request,
    runtime: Rag,
    principal: CorpusWriter,
    files: Annotated[list[UploadFile], File(description="Markdown, HTML, PDF or text.")],
) -> UploadAccepted:
    """Store the files and start an ``ingest`` run on them.

    The filename as sent (``docs/DEPLOY.md``) is the document's title and the
    first part of every chunk's heading, so send paths you want to see in a
    source list. Limits: ``RAG__MAX_UPLOAD_MB`` per file, 8 MB per request,
    100 files per request.
    """
    principal.require(SCOPE_RUN)  # it starts an ingest run, on a new thread
    principal.require(SCOPE_WRITE)
    if not files:
        raise BadRequest("Send at least one file in the 'files' field.")
    if len(files) > MAX_FILES:
        raise BadRequest(f"At most {MAX_FILES} files per request; send the rest separately.")
    limit = int(runtime.settings.rag.max_upload_mb * 1024 * 1024)
    new_docs = [await _read(f, limit) for f in files]

    added = await runtime.corpora.add_documents(principal.tenant_id, corpus_id, new_docs)
    queued = [a.document.document_id for a in added if a.queued]
    body = UploadAccepted(
        corpus_id=corpus_id,
        documents=[
            UploadedDocument.model_validate(a.document.public() | {"queued": a.queued})
            for a in added
        ],
    )
    if not queued:
        return body

    if runtime.limiter is not None:
        await runtime.limiter.enforce(principal.tenant_id)
    thread = await runtime.threads.create(
        principal, agent=INGEST_AGENT, title=f"Ingest into corpus {corpus_id}"
    )
    record = await runtime.runs.start(
        agent=INGEST_AGENT,
        thread_id=thread["thread_id"],
        principal=principal,
        request_id=str(getattr(request.state, "request_id", "-")),
        payload={"corpus_id": corpus_id, "document_ids": queued},
    )
    body.run_id = record.run_id
    body.thread_id = record.thread_id
    body.events_url = f"/api/v1/runs/{record.run_id}/events"
    return body


async def _read(upload: UploadFile, limit: int) -> NewDocument:
    name = _clean_name(upload.filename or "")
    media_type = media_type_for(name)
    if media_type is None:
        accepted = ", ".join(sorted(MEDIA_TYPES))
        raise BadRequest(f"{name or 'A file'}: unsupported type. Accepted: {accepted}.")
    content = await upload.read(limit + 1)
    if len(content) > limit:
        raise PayloadTooLarge(f"{name} is larger than RAG__MAX_UPLOAD_MB ({limit} bytes).")
    if not content.strip():
        raise BadRequest(f"{name} is empty.")
    return NewDocument(title=name, media_type=media_type, content=content, source_uri=name)


def _clean_name(raw: str) -> str:
    """A display path: forward slashes, no leading ``./`` or ``/``, no ``..``."""
    parts = [p for p in PurePosixPath(raw.replace("\\", "/")).parts if p not in {"/", ".", ".."}]
    return "/".join(parts)[-200:] or f"document-{uuid.uuid4().hex[:8]}"
