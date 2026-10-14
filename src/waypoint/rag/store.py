"""Corpora, documents and chunks: persistence, with tenant isolation twice over.

Design decision R5. Every query here names the tenant in its ``where``
clause, and every Postgres query also runs inside :meth:`PgCorpusStore._as_tenant`:
a transaction that switches to the ``waypoint_tenant`` role and sets
``app.tenant_id``, so row-level security filters the rows again even if a
``where`` clause is wrong. Both settings are transaction-local, which is what
makes them safe on a pooled connection.

``MemoryCorpusStore`` is the unit-test tier's stand-in (no Postgres), with the
same visibility rules written in Python.
"""

import datetime as dt
import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal, Protocol, cast

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation

from waypoint.errors import Conflict, NotFound
from waypoint.infra.db import TENANT_ROLE, Database
from waypoint.rag.chunk import Chunk

type Visibility = Literal["private", "public"]
type Status = Literal["pending", "ready", "failed"]


@dataclass(frozen=True, slots=True)
class Corpus:
    corpus_id: str
    tenant_id: str
    name: str
    visibility: Visibility
    embed_model: str
    created_at: str
    documents: int = 0
    chunks: int = 0

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    document_id: str
    corpus_id: str
    title: str
    source_uri: str | None
    media_type: str
    sha256: str
    bytes: int
    status: Status
    error: str | None
    created_at: str
    chunks: int = 0

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NewDocument:
    title: str
    media_type: str
    content: bytes
    source_uri: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class Added:
    document: DocumentInfo
    queued: bool
    """True when this upload needs an ingest run: new, or a retry of a failed one.
    False for an exact duplicate that is already pending or ready."""


@dataclass(frozen=True, slots=True)
class StoredDocument:
    document_id: str
    corpus_id: str
    title: str
    media_type: str
    content: bytes
    embed_model: str


@dataclass(frozen=True, slots=True)
class StoredChunk:
    document_id: str
    ordinal: int
    heading: str
    paragraphs: tuple[str, ...]
    embedding: tuple[float, ...] = field(repr=False, default=())
    context: str = ""
    """The contextual-retrieval preamble, empty when it was not generated."""

    @property
    def text(self) -> str:
        """What was embedded, and what a reranker reads: as ``Chunk.text``."""
        return Chunk(self.ordinal, self.heading, self.paragraphs, self.context).text


@dataclass(frozen=True, slots=True)
class ChunkHit:
    """A chunk one search method returned, with that method's score (higher is better)."""

    chunk: StoredChunk
    score: float


class CorpusStore(Protocol):
    async def create_corpus(
        self, tenant_id: str, name: str, embed_model: str, *, visibility: Visibility = "private"
    ) -> Corpus: ...

    async def list_corpora(self, tenant_id: str) -> list[Corpus]: ...

    async def get_corpus(self, tenant_id: str, corpus_id: str) -> Corpus | None: ...

    async def delete_corpus(self, tenant_id: str, corpus_id: str) -> bool: ...

    async def set_visibility(
        self, tenant_id: str, corpus_id: str, visibility: Visibility
    ) -> Corpus | None: ...

    async def add_documents(
        self, tenant_id: str, corpus_id: str, documents: Sequence[NewDocument]
    ) -> list[Added]: ...

    async def list_documents(self, tenant_id: str, corpus_id: str) -> list[DocumentInfo]: ...

    async def load_document(self, tenant_id: str, document_id: str) -> StoredDocument | None: ...

    async def delete_document(self, tenant_id: str, document_id: str) -> bool: ...

    async def write_chunks(
        self,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
    ) -> int: ...

    async def mark_failed(self, tenant_id: str, document_id: str, error: str) -> None: ...

    async def list_chunks(self, tenant_id: str, corpus_id: str) -> list[StoredChunk]: ...

    async def vector_search(
        self, tenant_id: str, corpus_id: str, embedding: Sequence[float], limit: int
    ) -> list[ChunkHit]: ...

    async def keyword_search(
        self, tenant_id: str, corpus_id: str, query: str, limit: int
    ) -> list[ChunkHit]: ...


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _check_lengths(chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> None:
    if len(chunks) != len(embeddings):
        raise ValueError(f"{len(chunks)} chunks but {len(embeddings)} embeddings")


# -- Postgres -------------------------------------------------------------------


def _vector(values: Sequence[float]) -> str:
    """pgvector's text form. Avoids a client-side adapter dependency."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


_PIN_SEARCH_PATH = """
    select set_config('search_path',
                      (select string_agg(quote_ident(s), ',')
                         from unnest(current_schemas(false)) as s),
                      true)
"""


@asynccontextmanager
async def tenant_transaction(
    db: Database, tenant_id: str | None
) -> AsyncIterator[AsyncConnection[Any]]:
    """A transaction in which row-level security applies, scoped to ``tenant_id``.

    ``None`` leaves ``app.tenant_id`` unset, which the policies treat as no
    tenant at all; only tests want that.
    """
    async with db.pool.connection() as conn, conn.transaction():
        # Pin the schemas first: a search_path of "$user", public resolves
        # "$user" per role, and after the switch it would name the tenant
        # role's (nonexistent) schema instead of the one the tables are in.
        await conn.execute(_PIN_SEARCH_PATH)
        await conn.execute(f"set local role {TENANT_ROLE}")
        if tenant_id is not None:
            await conn.execute("select set_config('app.tenant_id', %s, true)", (tenant_id,))
        yield conn


@asynccontextmanager
async def owner_transaction(db: Database) -> AsyncIterator[AsyncConnection[Any]]:
    """A transaction as the tables' owner, which row-level security does not
    filter. Only for writing public corpora (``rag/seed.py``); never for tenant
    input."""
    async with db.pool.connection() as conn, conn.transaction():
        yield conn


_CORPUS_COLUMNS = """
    c.corpus_id::text as corpus_id, c.tenant_id, c.name, c.visibility, c.embed_model,
    c.created_at,
    (select count(*) from documents d where d.corpus_id = c.corpus_id) as documents,
    (select count(*) from chunks k where k.corpus_id = c.corpus_id) as chunks
"""

_DOCUMENT_COLUMNS = """
    d.document_id::text as document_id, d.corpus_id::text as corpus_id, d.title,
    d.source_uri, d.media_type, d.sha256, octet_length(d.content) as bytes, d.status,
    d.error, d.created_at,
    (select count(*) from chunks k where k.document_id = d.document_id) as chunks
"""


def _corpus(row: dict[str, Any]) -> Corpus:
    return Corpus(**(row | {"created_at": _iso(row["created_at"])}))


def _document(row: dict[str, Any]) -> DocumentInfo:
    return DocumentInfo(**(row | {"created_at": _iso(row["created_at"])}))


class PgCorpusStore:
    """``owner=True`` runs every query as the tables' owner instead of the tenant
    role, so it can write public corpora. Only the seeder makes one; an admin
    publishes through ``set_visibility``, which runs only its one update as owner."""

    def __init__(self, db: Database, *, owner: bool = False) -> None:
        self._db = db
        self._owner = owner

    def _as_tenant(self, tenant_id: str) -> AbstractAsyncContextManager[AsyncConnection[Any]]:
        if self._owner:
            return owner_transaction(self._db)
        return tenant_transaction(self._db, tenant_id)

    async def _one(
        self, tenant_id: str, sql: str, params: Sequence[Any] = ()
    ) -> dict[str, Any] | None:
        async with self._as_tenant(tenant_id) as conn:
            cur = await conn.execute(sql, params)
            return cast("dict[str, Any] | None", await cur.fetchone())

    async def _all(
        self, tenant_id: str, sql: str, params: Sequence[Any] = ()
    ) -> list[dict[str, Any]]:
        async with self._as_tenant(tenant_id) as conn:
            cur = await conn.execute(sql, params)
            return cast("list[dict[str, Any]]", await cur.fetchall())

    # -- corpora --------------------------------------------------------------

    async def create_corpus(
        self, tenant_id: str, name: str, embed_model: str, *, visibility: Visibility = "private"
    ) -> Corpus:
        """A public corpus needs an owner store; the tenant role may not insert one."""
        corpus_id = str(uuid.uuid4())
        try:
            async with self._as_tenant(tenant_id) as conn:
                await conn.execute(
                    """insert into corpora (corpus_id, tenant_id, name, visibility, embed_model)
                       values (%s, %s, %s, %s, %s)""",
                    (corpus_id, tenant_id, name, visibility, embed_model),
                )
        except UniqueViolation:
            raise Conflict(f"A corpus named {name!r} already exists.") from None
        created = await self.get_corpus(tenant_id, corpus_id)
        assert created is not None
        return created

    async def list_corpora(self, tenant_id: str) -> list[Corpus]:
        rows = await self._all(
            tenant_id,
            f"""select {_CORPUS_COLUMNS} from corpora c
                where c.tenant_id = %s or c.visibility = 'public'
                order by c.visibility, c.name""",
            (tenant_id,),
        )
        return [_corpus(r) for r in rows]

    async def get_corpus(self, tenant_id: str, corpus_id: str) -> Corpus | None:
        if not is_uuid(corpus_id):
            return None
        row = await self._one(
            tenant_id,
            f"""select {_CORPUS_COLUMNS} from corpora c
                where c.corpus_id = %s and (c.tenant_id = %s or c.visibility = 'public')""",
            (corpus_id, tenant_id),
        )
        return _corpus(row) if row else None

    async def delete_corpus(self, tenant_id: str, corpus_id: str) -> bool:
        if not is_uuid(corpus_id):
            return False
        row = await self._one(
            tenant_id,
            """delete from corpora where corpus_id = %s and tenant_id = %s
               and (visibility = 'private' or %s) returning corpus_id""",
            (corpus_id, tenant_id, self._owner),
        )
        return row is not None

    async def set_visibility(
        self, tenant_id: str, corpus_id: str, visibility: Visibility
    ) -> Corpus | None:
        """Publish or unpublish one of ``tenant_id``'s own corpora.

        Runs as the tables' owner: the tenant role may not write a public row,
        by design. The caller must have checked that the principal is an admin
        (``POST /corpora/{id}/visibility`` requires ``keys:admin``). Only the
        tenant's own corpora: an admin cannot publish another tenant's.
        """
        if not is_uuid(corpus_id):
            return None
        async with owner_transaction(self._db) as conn:
            cur = await conn.execute(
                """update corpora set visibility = %s
                   where corpus_id = %s and tenant_id = %s returning corpus_id""",
                (visibility, corpus_id, tenant_id),
            )
            if await cur.fetchone() is None:
                return None
        return await self.get_corpus(tenant_id, corpus_id)

    # -- documents ------------------------------------------------------------

    async def add_documents(
        self, tenant_id: str, corpus_id: str, documents: Sequence[NewDocument]
    ) -> list[Added]:
        if not is_uuid(corpus_id):
            raise NotFound(f"Unknown corpus {corpus_id!r}.")
        added: list[Added] = []
        async with self._as_tenant(tenant_id) as conn:
            cur = await conn.execute(
                """select 1 from corpora where corpus_id = %s and tenant_id = %s
                   and (visibility = 'private' or %s)""",
                (corpus_id, tenant_id, self._owner),
            )
            if await cur.fetchone() is None:
                raise NotFound(f"Unknown corpus {corpus_id!r}.")
            for doc in documents:
                cur = await conn.execute(
                    """insert into documents
                         (document_id, corpus_id, tenant_id, title, source_uri, media_type,
                          content, sha256)
                       values (%s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict (corpus_id, sha256) do nothing
                       returning document_id""",
                    (
                        str(uuid.uuid4()),
                        corpus_id,
                        tenant_id,
                        doc.title,
                        doc.source_uri,
                        doc.media_type,
                        doc.content,
                        doc.sha256,
                    ),
                )
                queued = await cur.fetchone() is not None
                if not queued:
                    # Same bytes already here. A failed one is retried; anything
                    # else is left alone, so re-uploading a folder is cheap.
                    cur = await conn.execute(
                        """update documents set status = 'pending', error = null
                           where corpus_id = %s and sha256 = %s and tenant_id = %s
                             and status = 'failed'
                           returning document_id""",
                        (corpus_id, doc.sha256, tenant_id),
                    )
                    queued = await cur.fetchone() is not None
                cur = await conn.execute(
                    f"""select {_DOCUMENT_COLUMNS} from documents d
                        where d.corpus_id = %s and d.sha256 = %s and d.tenant_id = %s""",
                    (corpus_id, doc.sha256, tenant_id),
                )
                row = cast("dict[str, Any]", await cur.fetchone())
                added.append(Added(_document(row), queued))
        return added

    async def list_documents(self, tenant_id: str, corpus_id: str) -> list[DocumentInfo]:
        if not is_uuid(corpus_id):
            return []
        rows = await self._all(
            tenant_id,
            f"""select {_DOCUMENT_COLUMNS} from documents d
                join corpora c on c.corpus_id = d.corpus_id
                where d.corpus_id = %s and (c.tenant_id = %s or c.visibility = 'public')
                order by d.created_at, d.title""",
            (corpus_id, tenant_id),
        )
        return [_document(r) for r in rows]

    async def load_document(self, tenant_id: str, document_id: str) -> StoredDocument | None:
        if not is_uuid(document_id):
            return None
        row = await self._one(
            tenant_id,
            """select d.document_id::text as document_id, d.corpus_id::text as corpus_id,
                      d.title, d.media_type, d.content, c.embed_model
               from documents d join corpora c on c.corpus_id = d.corpus_id
               where d.document_id = %s and d.tenant_id = %s""",
            (document_id, tenant_id),
        )
        if row is None:
            return None
        return StoredDocument(**(row | {"content": bytes(row["content"])}))

    async def delete_document(self, tenant_id: str, document_id: str) -> bool:
        """With its chunks. Only in the tenant's own corpora (all, for the owner)."""
        if not is_uuid(document_id):
            return False
        row = await self._one(
            tenant_id,
            """delete from documents d using corpora c
               where d.document_id = %s and d.tenant_id = %s and c.corpus_id = d.corpus_id
                 and (c.visibility = 'private' or %s)
               returning d.document_id""",
            (document_id, tenant_id, self._owner),
        )
        return row is not None

    # -- chunks ---------------------------------------------------------------

    async def write_chunks(
        self,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
    ) -> int:
        """Replace the document's chunks and mark it ready, in one transaction."""
        _check_lengths(chunks, embeddings)
        async with self._as_tenant(tenant_id) as conn:
            cur = await conn.execute(
                "select corpus_id from documents where document_id = %s and tenant_id = %s",
                (document_id, tenant_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise NotFound(f"Unknown document {document_id!r}.")
            corpus_id = row["corpus_id"]
            await conn.execute("delete from chunks where document_id = %s", (document_id,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    """insert into chunks
                         (document_id, corpus_id, tenant_id, ordinal, heading, paragraphs,
                          context, embedding)
                       values (%s, %s, %s, %s, %s, %s, nullif(%s, ''), %s::vector)""",
                    [
                        (
                            document_id,
                            corpus_id,
                            tenant_id,
                            c.ordinal,
                            c.heading,
                            list(c.paragraphs),
                            c.context,
                            _vector(e),
                        )
                        for c, e in zip(chunks, embeddings, strict=True)
                    ],
                )
            await conn.execute(
                "update documents set status = 'ready', error = null where document_id = %s",
                (document_id,),
            )
        return len(chunks)

    async def mark_failed(self, tenant_id: str, document_id: str, error: str) -> None:
        async with self._as_tenant(tenant_id) as conn:
            await conn.execute(
                """update documents set status = 'failed', error = %s
                   where document_id = %s and tenant_id = %s""",
                (error[:2000], document_id, tenant_id),
            )

    async def list_chunks(self, tenant_id: str, corpus_id: str) -> list[StoredChunk]:
        if not is_uuid(corpus_id):
            return []
        rows = await self._all(
            tenant_id,
            """select k.document_id::text as document_id, k.ordinal, k.heading, k.paragraphs,
                      coalesce(k.context, '') as context
               from chunks k join corpora c on c.corpus_id = k.corpus_id
               where k.corpus_id = %s and (c.tenant_id = %s or c.visibility = 'public')
               order by k.document_id, k.ordinal""",
            (corpus_id, tenant_id),
        )
        return [StoredChunk(**(r | {"paragraphs": tuple(r["paragraphs"])})) for r in rows]

    # -- search ---------------------------------------------------------------

    async def vector_search(
        self, tenant_id: str, corpus_id: str, embedding: Sequence[float], limit: int
    ) -> list[ChunkHit]:
        """Nearest chunks by cosine similarity, through the HNSW index.

        Two index settings, both transaction-local. ``ef_search`` (default 40)
        caps how many candidates one HNSW scan yields, so it must be at least
        ``limit``. ``iterative_scan`` (pgvector 0.8) keeps scanning when the
        ``corpus_id`` filter discards candidates; without it a small corpus
        among large ones could come back short. ``relaxed_order`` may return
        neighbours slightly out of order, so the rows are re-sorted here.
        """
        if not is_uuid(corpus_id):
            return []
        vector = _vector(embedding)
        async with self._as_tenant(tenant_id) as conn:
            await conn.execute(
                """select set_config('hnsw.ef_search', %s, true),
                          set_config('hnsw.iterative_scan', 'relaxed_order', true)""",
                (str(max(40, limit)),),
            )
            cur = await conn.execute(
                f"""select {_CHUNK_COLUMNS}, 1 - (k.embedding <=> %s::vector) as score
                    from chunks k
                    where k.corpus_id = %s and {_VISIBLE_CORPUS}
                    order by k.embedding <=> %s::vector
                    limit %s""",
                (vector, corpus_id, corpus_id, tenant_id, vector, limit),
            )
            rows = cast("list[dict[str, Any]]", await cur.fetchall())
        hits = [_hit(r) for r in rows]
        return sorted(hits, key=lambda h: h.score, reverse=True)

    async def keyword_search(
        self, tenant_id: str, corpus_id: str, query: str, limit: int
    ) -> list[ChunkHit]:
        """Postgres full-text search over heading, paragraphs and context.

        The query's lexemes are OR-ed, not AND-ed: ``plainto_tsquery`` would
        require every word of a natural-language question in one chunk, which
        almost nothing satisfies. Ranked by ``ts_rank_cd`` (cover density),
        which rewards matched terms that sit close together. This is not BM25:
        there is no corpus-wide term weighting.
        """
        if not is_uuid(corpus_id):
            return []
        rows = await self._all(
            tenant_id,
            f"""with q as (
                    select nullif(replace(plainto_tsquery('english', %s)::text, ' & ', ' | '),
                                  '')::tsquery as query
                )
                select {_CHUNK_COLUMNS}, ts_rank_cd(k.tsv, q.query) as score
                from chunks k, q
                where k.corpus_id = %s and {_VISIBLE_CORPUS} and k.tsv @@ q.query
                order by score desc, k.chunk_id
                limit %s""",
            (query, corpus_id, corpus_id, tenant_id, limit),
        )
        return [_hit(r) for r in rows]


_CHUNK_COLUMNS = (
    "k.document_id::text as document_id, k.ordinal, k.heading, k.paragraphs, "
    "coalesce(k.context, '') as context"
)

_VISIBLE_CORPUS = """exists (select 1 from corpora c where c.corpus_id = %s
                             and (c.tenant_id = %s or c.visibility = 'public'))"""
"""Uncorrelated, so Postgres evaluates it once and the index scan stays a plain
scan of one corpus."""


def _hit(row: dict[str, Any]) -> ChunkHit:
    chunk = StoredChunk(
        document_id=row["document_id"],
        ordinal=row["ordinal"],
        heading=row["heading"],
        paragraphs=tuple(row["paragraphs"]),
        context=row["context"],
    )
    return ChunkHit(chunk, float(row["score"]))


# -- in memory ------------------------------------------------------------------


@dataclass(slots=True)
class _Doc:
    info: DocumentInfo
    tenant_id: str
    content: bytes


class MemoryCorpusStore:
    """Same contract, in a dict. Visibility rules mirror the RLS policies.

    ``stable_ids`` derives corpus and document ids from tenant, corpus name and
    document title instead of drawing random ones. The research eval needs it:
    a passage's ``doc://<document_id>`` is part of the worker's input, so a
    random id per process made every corpus-case call miss the replay store.
    """

    def __init__(self, *, stable_ids: bool = False, owner: bool = False) -> None:
        self._stable_ids = stable_ids
        self._owner = owner
        self._corpora: dict[str, Corpus] = {}
        self._docs: dict[str, _Doc] = {}
        self._chunks: dict[str, list[StoredChunk]] = {}

    def _new_id(self, *parts: str) -> str:
        if self._stable_ids:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, "waypoint-memory:" + "/".join(parts)))
        return str(uuid.uuid4())

    def _visible(self, tenant_id: str, corpus_id: str) -> Corpus | None:
        corpus = self._corpora.get(corpus_id)
        if corpus and (corpus.tenant_id == tenant_id or corpus.visibility == "public"):
            return corpus
        return None

    def _owned(self, tenant_id: str, corpus_id: str) -> Corpus | None:
        corpus = self._corpora.get(corpus_id)
        if (
            corpus
            and corpus.tenant_id == tenant_id
            and (corpus.visibility == "private" or self._owner)
        ):
            return corpus
        return None

    def _counted(self, corpus: Corpus) -> Corpus:
        docs = [d for d in self._docs.values() if d.info.corpus_id == corpus.corpus_id]
        chunks = sum(len(self._chunks.get(d.info.document_id, [])) for d in docs)
        return replace(corpus, documents=len(docs), chunks=chunks)

    def _doc_info(self, doc: _Doc) -> DocumentInfo:
        return replace(doc.info, chunks=len(self._chunks.get(doc.info.document_id, [])))

    async def create_corpus(
        self,
        tenant_id: str,
        name: str,
        embed_model: str,
        *,
        visibility: Visibility = "private",
    ) -> Corpus:
        """``visibility`` exists for tests; the API only ever creates private corpora."""
        if any(c.tenant_id == tenant_id and c.name == name for c in self._corpora.values()):
            raise Conflict(f"A corpus named {name!r} already exists.")
        corpus = Corpus(
            self._new_id(tenant_id, name), tenant_id, name, visibility, embed_model, _now()
        )
        self._corpora[corpus.corpus_id] = corpus
        return corpus

    async def list_corpora(self, tenant_id: str) -> list[Corpus]:
        visible = [c for c in self._corpora.values() if self._visible(tenant_id, c.corpus_id)]
        return [self._counted(c) for c in sorted(visible, key=lambda c: (c.visibility, c.name))]

    async def get_corpus(self, tenant_id: str, corpus_id: str) -> Corpus | None:
        corpus = self._visible(tenant_id, corpus_id)
        return self._counted(corpus) if corpus else None

    async def delete_corpus(self, tenant_id: str, corpus_id: str) -> bool:
        if self._owned(tenant_id, corpus_id) is None:
            return False
        del self._corpora[corpus_id]
        for doc_id in [k for k, d in self._docs.items() if d.info.corpus_id == corpus_id]:
            del self._docs[doc_id]
            self._chunks.pop(doc_id, None)
        return True

    async def set_visibility(
        self, tenant_id: str, corpus_id: str, visibility: Visibility
    ) -> Corpus | None:
        """As the Postgres store: the tenant's own corpora only; admin checked by the caller."""
        corpus = self._corpora.get(corpus_id)
        if corpus is None or corpus.tenant_id != tenant_id:
            return None
        self._corpora[corpus_id] = replace(corpus, visibility=visibility)
        return self._counted(self._corpora[corpus_id])

    async def add_documents(
        self, tenant_id: str, corpus_id: str, documents: Sequence[NewDocument]
    ) -> list[Added]:
        if self._owned(tenant_id, corpus_id) is None:
            raise NotFound(f"Unknown corpus {corpus_id!r}.")
        added: list[Added] = []
        for new in documents:
            existing = next(
                (
                    d
                    for d in self._docs.values()
                    if d.info.corpus_id == corpus_id and d.info.sha256 == new.sha256
                ),
                None,
            )
            if existing is not None:
                queued = existing.info.status == "failed"
                if queued:
                    existing.info = replace(existing.info, status="pending", error=None)
                added.append(Added(self._doc_info(existing), queued))
                continue
            info = DocumentInfo(
                document_id=self._new_id(tenant_id, self._corpora[corpus_id].name, new.title),
                corpus_id=corpus_id,
                title=new.title,
                source_uri=new.source_uri,
                media_type=new.media_type,
                sha256=new.sha256,
                bytes=len(new.content),
                status="pending",
                error=None,
                created_at=_now(),
            )
            self._docs[info.document_id] = _Doc(info, tenant_id, new.content)
            added.append(Added(info, True))
        return added

    async def list_documents(self, tenant_id: str, corpus_id: str) -> list[DocumentInfo]:
        if self._visible(tenant_id, corpus_id) is None:
            return []
        return [self._doc_info(d) for d in self._docs.values() if d.info.corpus_id == corpus_id]

    async def load_document(self, tenant_id: str, document_id: str) -> StoredDocument | None:
        doc = self._docs.get(document_id)
        if doc is None or doc.tenant_id != tenant_id:
            return None
        corpus = self._corpora[doc.info.corpus_id]
        return StoredDocument(
            document_id,
            doc.info.corpus_id,
            doc.info.title,
            doc.info.media_type,
            doc.content,
            corpus.embed_model,
        )

    async def delete_document(self, tenant_id: str, document_id: str) -> bool:
        doc = self._docs.get(document_id)
        if doc is None or self._owned(tenant_id, doc.info.corpus_id) is None:
            return False
        del self._docs[document_id]
        self._chunks.pop(document_id, None)
        return True

    async def write_chunks(
        self,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
    ) -> int:
        _check_lengths(chunks, embeddings)
        doc = self._docs.get(document_id)
        if doc is None or self._owned(tenant_id, doc.info.corpus_id) is None:
            raise NotFound(f"Unknown document {document_id!r}.")
        self._chunks[document_id] = [
            StoredChunk(document_id, c.ordinal, c.heading, c.paragraphs, tuple(e), c.context)
            for c, e in zip(chunks, embeddings, strict=True)
        ]
        doc.info = replace(doc.info, status="ready", error=None)
        return len(chunks)

    async def mark_failed(self, tenant_id: str, document_id: str, error: str) -> None:
        doc = self._docs.get(document_id)
        if doc is not None and doc.tenant_id == tenant_id:
            doc.info = replace(doc.info, status="failed", error=error[:2000])

    async def list_chunks(self, tenant_id: str, corpus_id: str) -> list[StoredChunk]:
        if self._visible(tenant_id, corpus_id) is None:
            return []
        docs = [d for d in self._docs.values() if d.info.corpus_id == corpus_id]
        return [c for d in docs for c in self._chunks.get(d.info.document_id, [])]

    async def vector_search(
        self, tenant_id: str, corpus_id: str, embedding: Sequence[float], limit: int
    ) -> list[ChunkHit]:
        """Exact cosine similarity. Embeddings here are unit vectors (HashEmbeddings)."""
        chunks = await self.list_chunks(tenant_id, corpus_id)
        hits = [
            ChunkHit(c, sum(a * b for a, b in zip(c.embedding, embedding, strict=False)))
            for c in chunks
        ]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:limit]

    async def keyword_search(
        self, tenant_id: str, corpus_id: str, query: str, limit: int
    ) -> list[ChunkHit]:
        """Shared lower-case words, counted. A stand-in for the unit tier, not a
        model of Postgres full-text search: never measure retrieval with it."""
        terms = set(_WORD.findall(query.lower()))
        hits = []
        for chunk in await self.list_chunks(tenant_id, corpus_id):
            words = set(_WORD.findall(chunk.text.lower()))
            if score := len(terms & words):
                hits.append(ChunkHit(chunk, float(score)))
        return sorted(hits, key=lambda h: h.score, reverse=True)[:limit]


_WORD = re.compile(r"\w{3,}")
