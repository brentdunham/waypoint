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


class CorpusStore(Protocol):
    async def create_corpus(self, tenant_id: str, name: str, embed_model: str) -> Corpus: ...

    async def list_corpora(self, tenant_id: str) -> list[Corpus]: ...

    async def get_corpus(self, tenant_id: str, corpus_id: str) -> Corpus | None: ...

    async def delete_corpus(self, tenant_id: str, corpus_id: str) -> bool: ...

    async def add_documents(
        self, tenant_id: str, corpus_id: str, documents: Sequence[NewDocument]
    ) -> list[Added]: ...

    async def list_documents(self, tenant_id: str, corpus_id: str) -> list[DocumentInfo]: ...

    async def load_document(self, tenant_id: str, document_id: str) -> StoredDocument | None: ...

    async def write_chunks(
        self,
        tenant_id: str,
        document_id: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
    ) -> int: ...

    async def mark_failed(self, tenant_id: str, document_id: str, error: str) -> None: ...

    async def list_chunks(self, tenant_id: str, corpus_id: str) -> list[StoredChunk]: ...


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
    def __init__(self, db: Database) -> None:
        self._db = db

    def _as_tenant(self, tenant_id: str) -> AbstractAsyncContextManager[AsyncConnection[Any]]:
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

    async def create_corpus(self, tenant_id: str, name: str, embed_model: str) -> Corpus:
        corpus_id = str(uuid.uuid4())
        try:
            async with self._as_tenant(tenant_id) as conn:
                await conn.execute(
                    """insert into corpora (corpus_id, tenant_id, name, embed_model)
                       values (%s, %s, %s, %s)""",
                    (corpus_id, tenant_id, name, embed_model),
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
               and visibility = 'private' returning corpus_id""",
            (corpus_id, tenant_id),
        )
        return row is not None

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
                   and visibility = 'private'""",
                (corpus_id, tenant_id),
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
                          embedding)
                       values (%s, %s, %s, %s, %s, %s, %s::vector)""",
                    [
                        (
                            document_id,
                            corpus_id,
                            tenant_id,
                            c.ordinal,
                            c.heading,
                            list(c.paragraphs),
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
            """select k.document_id::text as document_id, k.ordinal, k.heading, k.paragraphs
               from chunks k join corpora c on c.corpus_id = k.corpus_id
               where k.corpus_id = %s and (c.tenant_id = %s or c.visibility = 'public')
               order by k.document_id, k.ordinal""",
            (corpus_id, tenant_id),
        )
        return [StoredChunk(**(r | {"paragraphs": tuple(r["paragraphs"])})) for r in rows]


# -- in memory ------------------------------------------------------------------


@dataclass(slots=True)
class _Doc:
    info: DocumentInfo
    tenant_id: str
    content: bytes


class MemoryCorpusStore:
    """Same contract, in a dict. Visibility rules mirror the RLS policies."""

    def __init__(self) -> None:
        self._corpora: dict[str, Corpus] = {}
        self._docs: dict[str, _Doc] = {}
        self._chunks: dict[str, list[StoredChunk]] = {}

    def _visible(self, tenant_id: str, corpus_id: str) -> Corpus | None:
        corpus = self._corpora.get(corpus_id)
        if corpus and (corpus.tenant_id == tenant_id or corpus.visibility == "public"):
            return corpus
        return None

    def _owned(self, tenant_id: str, corpus_id: str) -> Corpus | None:
        corpus = self._corpora.get(corpus_id)
        if corpus and corpus.tenant_id == tenant_id and corpus.visibility == "private":
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
        corpus = Corpus(str(uuid.uuid4()), tenant_id, name, visibility, embed_model, _now())
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
                document_id=str(uuid.uuid4()),
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
            StoredChunk(document_id, c.ordinal, c.heading, c.paragraphs, tuple(e))
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
