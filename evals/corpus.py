"""A pinned corpus and its index, shared by the retrieval eval and the research eval.

``corpora/<name>.toml`` names files at a git commit; ``build_index`` indexes
them with ``rag/index.py``, the ingest agent's own code, under tenant
``evals``, in a corpus named after a fingerprint of everything that shapes
the vectors. A store that already holds that corpus is reused.
"""

import asyncio
import hashlib
import json
import subprocess
import time
import tomllib
from dataclasses import dataclass
from typing import Any

from langchain_core.embeddings import Embeddings

from evals.cases import ROOT
from waypoint.rag.index import index_document
from waypoint.rag.parse import media_type_for
from waypoint.rag.store import CorpusStore, NewDocument
from waypoint.settings import Settings

CORPORA = ROOT / "corpora"
REPO = ROOT.parent
TENANT = "evals"
INDEX_SOURCES = ("parse.py", "chunk.py", "index.py")
"""Code whose change invalidates the index, by file content."""


@dataclass(frozen=True, slots=True)
class Manifest:
    name: str
    rev: str
    paths: tuple[str, ...]
    suffixes: tuple[str, ...]


def load_manifest(name: str) -> Manifest:
    data = tomllib.loads((CORPORA / f"{name}.toml").read_text(encoding="utf-8"))
    return Manifest(name, data["rev"], tuple(data["paths"]), tuple(data["suffixes"]))


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, check=True, timeout=30
    ).stdout


def read_corpus(manifest: Manifest) -> dict[str, bytes]:
    """Path -> bytes at the pinned commit. ``git cat-file`` applies no eol filters."""
    names = _git("ls-tree", "-r", "--name-only", manifest.rev, "--", *manifest.paths)
    paths = [p for p in names.decode().splitlines() if p.endswith(manifest.suffixes)]
    return {p: _git("cat-file", "blob", f"{manifest.rev}:{p}") for p in sorted(paths)}


def index_fingerprint(settings: Settings, manifest: Manifest, files: dict[str, bytes]) -> str:
    parts = {
        "rev": manifest.rev,
        "files": {p: hashlib.sha256(b).hexdigest() for p, b in files.items()},
        "model": settings.rag.embeddings_model,
        "dims": settings.rag.embeddings_dims,
        "chunk_tokens": settings.rag.chunk_tokens,
        "code": {
            name: hashlib.sha256(
                (REPO / "src" / "waypoint" / "rag" / name).read_bytes()
            ).hexdigest()
            for name in INDEX_SOURCES
        },
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


@dataclass(slots=True)
class Index:
    corpus_id: str
    titles: dict[str, str]
    """document_id -> path."""
    chunks: int
    reused: bool
    tokens: int
    """Estimated tokens embedded to build it (chars / 4), 0 when reused."""
    seconds: float


async def build_index(
    settings: Settings,
    store: CorpusStore,
    embeddings: Embeddings,
    name: str,
    files: dict[str, bytes],
) -> Index:
    """Reuse a complete index of the same fingerprint, else build one."""
    started = time.perf_counter()
    embed_model = settings.rag.embeddings_model or "unset"
    existing = next((c for c in await store.list_corpora(TENANT) if c.name == name), None)
    if existing is not None:
        docs = await store.list_documents(TENANT, existing.corpus_id)
        if len(docs) == len(files) and all(d.status == "ready" for d in docs):
            return Index(
                existing.corpus_id,
                {d.document_id: d.title for d in docs},
                existing.chunks,
                reused=True,
                tokens=0,
                seconds=time.perf_counter() - started,
            )
        await store.delete_corpus(TENANT, existing.corpus_id)  # partial: start over

    corpus = await store.create_corpus(TENANT, name, embed_model)
    new = [
        NewDocument(path, media_type_for(path) or "text/plain", content, source_uri=path)
        for path, content in files.items()
    ]
    added = await store.add_documents(TENANT, corpus.corpus_id, new)
    gate = asyncio.Semaphore(4)

    async def one(document_id: str) -> Any:
        async with gate:
            return await index_document(
                store,
                embeddings,
                tenant_id=TENANT,
                document_id=document_id,
                embed_model=embed_model,
                chunk_tokens=settings.rag.chunk_tokens,
            )

    results = await asyncio.gather(*(one(a.document.document_id) for a in added))
    failed = [f"{r['title']}: {r['error']}" for r in results if r["status"] != "ready"]
    if failed:
        raise RuntimeError("indexing failed:\n" + "\n".join(failed))
    chunks = await store.list_chunks(TENANT, corpus.corpus_id)
    tokens = sum(len("\n\n".join((c.heading, *c.paragraphs))) for c in chunks) // 4
    return Index(
        corpus.corpus_id,
        {a.document.document_id: a.document.title for a in added},
        len(chunks),
        reused=False,
        tokens=tokens,
        seconds=time.perf_counter() - started,
    )
