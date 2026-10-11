"""Keep a directory of documents indexed as a public corpus (RAG phase 5).

The demo's reviewers ask questions of this repository's own ``docs/``. The
corpus is public (every tenant can search it, none can change it) and owned by
the system tenant ``waypoint``. Row-level security lets tenants write only
private corpora, so seeding writes as the tables' owner
(``PgCorpusStore(owner=True)``); it never touches tenant input.

``seed_corpus`` reconciles rather than rebuilds: files whose bytes are already
indexed are left alone, changed or new ones are indexed with the ingest
agent's own code (``rag/index.py``), and removed ones are deleted. A deploy
that changes one page re-embeds one page. The service runs it in the
background at every boot (``RAG__SEED_DIR``), under an advisory lock so two
workers do not seed at once; ``python -m waypoint.seed`` runs it by hand.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.embeddings import Embeddings

from waypoint.infra.db import Database
from waypoint.rag.contextualize import Contextualizer
from waypoint.rag.index import index_document
from waypoint.rag.parse import media_type_for
from waypoint.rag.store import CorpusStore, DocumentInfo, NewDocument, PgCorpusStore
from waypoint.settings import Settings

log = logging.getLogger(__name__)

SYSTEM_TENANT = "waypoint"
"""Owns public corpora. No API key is minted for it."""
SEED_LOCK_KEY = int.from_bytes(b"RAGSEED", "big")
CONCURRENCY = 4


@dataclass(slots=True)
class SeedResult:
    corpus_id: str
    indexed: list[str] = field(default_factory=list)
    """New or changed files, indexed now."""
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    failed: list[str] = field(default_factory=list)
    """``title: reason`` for files that could not be indexed."""
    seconds: float = 0.0


def collect(directory: Path, root: Path) -> dict[str, bytes]:
    """Every file under ``directory`` that ``rag/parse.py`` reads, by path
    relative to ``root`` (``docs/DEPLOY.md``), which becomes its title."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and media_type_for(path.name) is not None
    }


async def seed_corpus(
    store: CorpusStore,
    embeddings: Embeddings,
    *,
    name: str,
    files: dict[str, bytes],
    embed_model: str,
    chunk_tokens: int,
    contextualizer: Contextualizer | None = None,
    tenant_id: str = SYSTEM_TENANT,
) -> SeedResult:
    """Make the public corpus ``name`` hold exactly ``files``, indexed.

    ``store`` must be able to write public corpora (an owner store). A corpus
    embedded with another model is rebuilt from scratch: vectors from two
    models cannot be searched together.
    """
    started = time.perf_counter()
    corpus = next(
        (
            c
            for c in await store.list_corpora(tenant_id)
            if c.tenant_id == tenant_id and c.name == name
        ),
        None,
    )
    if corpus is not None and corpus.embed_model != embed_model:
        log.info("seed: %s was embedded with %s; rebuilding", name, corpus.embed_model)
        await store.delete_corpus(tenant_id, corpus.corpus_id)
        corpus = None
    if corpus is None:
        corpus = await store.create_corpus(tenant_id, name, embed_model, visibility="public")
    result = SeedResult(corpus.corpus_id)

    wanted = {title: hashlib.sha256(content).hexdigest() for title, content in files.items()}
    current: dict[str, DocumentInfo] = {
        d.title: d for d in await store.list_documents(tenant_id, corpus.corpus_id)
    }
    for title, doc in current.items():
        # Gone, changed, or never finished (a crash mid-index leaves "pending"):
        # delete, and index the current bytes afresh below.
        if wanted.get(title) != doc.sha256 or doc.status != "ready":
            await store.delete_document(tenant_id, doc.document_id)
            if title not in wanted:
                result.removed.append(title)
    keep = {t for t, d in current.items() if wanted.get(t) == d.sha256 and d.status == "ready"}
    result.unchanged = len(keep)

    new = [
        NewDocument(title, media_type_for(title) or "text/plain", content, source_uri=title)
        for title, content in files.items()
        if title not in keep
    ]
    added = await store.add_documents(tenant_id, corpus.corpus_id, new) if new else []
    gate = asyncio.Semaphore(CONCURRENCY)

    async def index(document_id: str) -> None:
        async with gate:
            outcome = await index_document(
                store,
                embeddings,
                tenant_id=tenant_id,
                document_id=document_id,
                embed_model=embed_model,
                chunk_tokens=chunk_tokens,
                contextualizer=contextualizer,
            )
        if outcome["status"] == "ready":
            result.indexed.append(outcome["title"])
        else:
            result.failed.append(f"{outcome['title']}: {outcome['error']}")

    await asyncio.gather(*(index(a.document.document_id) for a in added if a.queued))
    result.indexed.sort()
    result.failed.sort()
    result.seconds = time.perf_counter() - started
    return result


async def seed_public_corpus(
    db: Database,
    settings: Settings,
    embeddings: Embeddings,
    contextualizer: Contextualizer | None = None,
    *,
    root: Path | None = None,
) -> SeedResult | None:
    """``RAG__SEED_DIR`` into ``RAG__SEED_CORPUS``, under the seed lock."""
    rag = settings.rag
    if not rag.seed_dir or not rag.embeddings_model:
        return None
    root = root or Path.cwd()
    directory = (root / rag.seed_dir).resolve()
    if not directory.is_dir():
        log.warning("RAG__SEED_DIR=%s is not a directory; nothing seeded", rag.seed_dir)
        return None
    files = collect(directory, root.resolve())
    if not files:
        log.warning("RAG__SEED_DIR=%s holds no documents; nothing seeded", rag.seed_dir)
        return None
    async with db.advisory_lock(SEED_LOCK_KEY, timeout_s=600):
        return await seed_corpus(
            PgCorpusStore(db, owner=True),
            embeddings,
            name=rag.seed_corpus,
            files=files,
            embed_model=rag.embeddings_model,
            chunk_tokens=rag.chunk_tokens,
            contextualizer=contextualizer,
        )


def describe(result: SeedResult, name: str) -> str:
    return (
        f"seeded {name}: {len(result.indexed)} indexed, {result.unchanged} unchanged, "
        f"{len(result.removed)} removed, {len(result.failed)} failed "
        f"in {result.seconds:.1f}s"
    )
