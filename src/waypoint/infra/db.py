"""Postgres access.

One ``AsyncConnectionPool`` is shared by three consumers: LangGraph's
checkpointer, LangGraph's long-term store, and this service's own tables
(API keys and thread ownership). Sharing the pool keeps connection count
predictable and means a single place to tune.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Self, cast

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

# An arbitrary but stable 64-bit key: "AGENTS" as bytes. Every process that
# migrates this database must agree on it, and nothing else may use it.
MIGRATION_LOCK_KEY = int.from_bytes(b"AGENTS", "big")

SCHEMA = """
create table if not exists api_keys (
    key_id      text primary key,
    key_hash    text not null unique,
    tenant_id   text not null,
    user_id     text not null,
    scopes      text[] not null default '{}',
    label       text,
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz
);
create index if not exists api_keys_tenant_idx on api_keys (tenant_id);

create table if not exists threads (
    thread_id   uuid primary key,
    tenant_id   text not null,
    user_id     text not null,
    agent       text not null,
    title       text,
    metadata    jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists threads_tenant_idx on threads (tenant_id, updated_at desc);

create table if not exists run_audit (
    run_id      uuid primary key,
    thread_id   uuid not null,
    tenant_id   text not null,
    user_id     text not null,
    agent       text not null,
    status      text not null,
    error       text,
    started_at  timestamptz not null default now(),
    ended_at    timestamptz
);
create index if not exists run_audit_thread_idx on run_audit (thread_id, started_at desc);
-- What each run cost (observability design, O5). Totals here, per-call detail
-- in traces. Rows from before this existed keep nulls: "not recorded", not zero.
alter table run_audit
    add column if not exists cost_usd      numeric(12, 6),
    add column if not exists input_tokens  bigint,
    add column if not exists output_tokens bigint,
    add column if not exists searches      int,
    add column if not exists usage         jsonb,
    add column if not exists trace_id      text;
create index if not exists run_audit_tenant_idx on run_audit (tenant_id, started_at desc);
"""

TENANT_ROLE = "waypoint_tenant"
"""The role every tenant-facing RAG query runs as (``set local role``).

The service connects as the tables' owner, and in the compose images that
user is a superuser: both bypass row-level security, so policies alone would
guard nothing. This role owns nothing, cannot log in and does not bypass RLS,
so queries made as it are filtered by the policies below whatever the SQL says.
"""

# Formatted with the embedding width (RAG__EMBEDDINGS_DIMS). Everything here is
# idempotent and runs on every boot under the migration lock. Policies are
# dropped and recreated so a changed policy reaches existing databases.
RAG_SCHEMA = """
create extension if not exists vector;

create table if not exists corpora (
    corpus_id   uuid primary key,
    tenant_id   text not null,
    name        text not null,
    visibility  text not null default 'private' check (visibility in ('private', 'public')),
    embed_model text not null,
    created_at  timestamptz not null default now(),
    unique (tenant_id, name)
);

create table if not exists documents (
    document_id uuid primary key,
    corpus_id   uuid not null references corpora on delete cascade,
    tenant_id   text not null,
    title       text not null,
    source_uri  text,
    media_type  text not null,
    content     bytea not null,
    sha256      text not null,
    status      text not null default 'pending' check (status in ('pending', 'ready', 'failed')),
    error       text,
    created_at  timestamptz not null default now(),
    unique (corpus_id, sha256)
);
create index if not exists documents_corpus_idx on documents (corpus_id, created_at);

-- A generated column needs an immutable expression, and array_to_string is
-- only marked stable (for arrays of types whose output can vary). For text[]
-- it cannot vary, so this wrapper declares what is true for its one use.
create or replace function chunk_search_text(context text, heading text, paragraphs text[])
    returns text language sql immutable parallel safe
    as $f$ select coalesce(context, '') || ' ' || heading || ' '
                  || array_to_string(paragraphs, ' ') $f$;

create table if not exists chunks (
    chunk_id    bigserial primary key,
    document_id uuid not null references documents on delete cascade,
    corpus_id   uuid not null,
    tenant_id   text not null,
    ordinal     int not null,
    heading     text not null,
    paragraphs  text[] not null,
    context     text,
    embedding   vector({dims}),
    tsv         tsvector generated always as (
                    to_tsvector('english'::regconfig,
                                chunk_search_text(context, heading, paragraphs))
                ) stored,
    unique (document_id, ordinal)
);
create index if not exists chunks_embedding_idx on chunks using hnsw (embedding vector_cosine_ops);
create index if not exists chunks_tsv_idx on chunks using gin (tsv);
create index if not exists chunks_corpus_idx on chunks (corpus_id);

do $$ begin
    if not exists (select from pg_roles where rolname = '{role}') then
        create role {role} nologin nobypassrls;
    end if;
exception when duplicate_object then null;  -- another database in the cluster won the race
end $$;
grant {role} to current_user;
do $$ begin
    execute format('grant usage on schema %I to {role}', current_schema());
end $$;
grant select, insert, update, delete on corpora, documents, chunks to {role};
grant usage on sequence chunks_chunk_id_seq to {role};

alter table corpora enable row level security;
alter table documents enable row level security;
alter table chunks enable row level security;

-- Read: your own rows, and anything in a public corpus.
drop policy if exists corpora_read on corpora;
create policy corpora_read on corpora for select to {role}
    using (tenant_id = current_setting('app.tenant_id', true) or visibility = 'public');
drop policy if exists documents_read on documents;
create policy documents_read on documents for select to {role}
    using (tenant_id = current_setting('app.tenant_id', true)
           or corpus_id in (select corpus_id from corpora where visibility = 'public'));
drop policy if exists chunks_read on chunks;
create policy chunks_read on chunks for select to {role}
    using (tenant_id = current_setting('app.tenant_id', true)
           or corpus_id in (select corpus_id from corpora where visibility = 'public'));

-- Write: only your own private corpora, and only rows that hang off them.
-- Public corpora are written by the owner role (seeding), never by a tenant.
-- Foreign keys are checked without RLS, so a document naming another tenant's
-- corpus would otherwise pass; the exists() clauses close that.
drop policy if exists corpora_write on corpora;
create policy corpora_write on corpora for all to {role}
    using ({own_corpus})
    with check ({own_corpus});
drop policy if exists documents_write on documents;
create policy documents_write on documents for all to {role}
    using ({own_document})
    with check ({own_document});
drop policy if exists chunks_write on chunks;
create policy chunks_write on chunks for all to {role}
    using ({own_chunk})
    with check ({own_chunk});
"""

_TENANT = "current_setting('app.tenant_id', true)"
_PRIVATE_CORPUS_OF = (
    "exists (select 1 from corpora c where c.corpus_id = {table}.corpus_id"
    f" and c.tenant_id = {_TENANT} and c.visibility = 'private')"
)
RAG_POLICIES = {
    "own_corpus": f"tenant_id = {_TENANT} and visibility = 'private'",
    "own_document": f"tenant_id = {_TENANT} and " + _PRIVATE_CORPUS_OF.format(table="documents"),
    "own_chunk": (
        f"tenant_id = {_TENANT} and "
        + _PRIVATE_CORPUS_OF.format(table="chunks")
        + " and exists (select 1 from documents d where d.document_id = chunks.document_id"
        f" and d.corpus_id = chunks.corpus_id and d.tenant_id = {_TENANT})"
    ),
}
"""The write predicates, spelled once: each serves as both USING and WITH CHECK."""


class SchemaMismatchError(RuntimeError):
    """The database disagrees with configuration in a way only a person can fix."""


class Database:
    """Thin wrapper over the pool. Every method is async and pool-scoped."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    @property
    def pool(self) -> AsyncConnectionPool:
        return self._pool

    async def open(self) -> Self:
        await self._pool.open(wait=True, timeout=30)
        return self

    async def close(self) -> None:
        await self._pool.close()

    async def migrate(self, *, embedding_dims: int = 1024) -> None:
        """Create this service's own tables. LangGraph creates its own separately.

        ``embedding_dims`` sizes ``chunks.embedding`` when the table is first
        created; an existing table keeps its width (see :meth:`embedding_dims`).
        """
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
            await conn.execute(
                RAG_SCHEMA.format(dims=int(embedding_dims), role=TENANT_ROLE, **RAG_POLICIES)
            )
        log.info("database schema ready")

    async def embedding_dims(self) -> int | None:
        """The declared width of ``chunks.embedding`` (pgvector keeps it in atttypmod)."""
        row = await self.fetch_one(
            """select atttypmod as dims from pg_attribute
               where attrelid = 'chunks'::regclass and attname = 'embedding'"""
        )
        return int(row["dims"]) if row and row["dims"] > 0 else None

    async def check_embedding_dims(self, expected: int) -> None:
        """Fail the boot if RAG__EMBEDDINGS_DIMS no longer matches the column."""
        actual = await self.embedding_dims()
        if actual is not None and actual != expected:
            raise SchemaMismatchError(
                f"chunks.embedding is vector({actual}) but RAG__EMBEDDINGS_DIMS={expected}. "
                f"Set RAG__EMBEDDINGS_DIMS={actual}, or re-embed: drop the chunks table "
                "(documents keep their content) and re-ingest each corpus."
            )

    @asynccontextmanager
    async def advisory_lock(
        self, key: int = MIGRATION_LOCK_KEY, *, poll_s: float = 0.25, timeout_s: float = 120.0
    ) -> AsyncIterator[None]:
        """Serialise schema work across every worker and replica.

        Two gunicorn workers booting together both call ``setup()`` on the
        checkpointer and the store, and LangGraph's migration step is a plain
        ``INSERT`` into its own version table -- so the loser dies on a
        duplicate key.

        This *polls* with ``pg_try_advisory_lock`` rather than blocking in
        ``pg_advisory_lock``, and the reason is subtle and load-bearing:
        LangGraph's setup runs ``CREATE INDEX CONCURRENTLY``, which waits for
        every transaction that is currently in flight. A session parked inside
        a blocking ``pg_advisory_lock`` *is* an in-flight transaction holding a
        virtualxid -- so the lock holder would wait forever on the very worker
        waiting for it. Postgres cannot see that as a deadlock and neither
        side ever times out. Polling leaves the waiter idle between attempts,
        holding nothing.

        The lock is session-scoped, so the explicit unlock matters: returning a
        connection to the pool does not release it.
        """
        deadline = time.monotonic() + timeout_s
        async with self._pool.connection() as conn:
            while True:
                cur = await conn.execute("select pg_try_advisory_lock(%s) as ok", (key,))
                row = cast("dict[str, Any] | None", await cur.fetchone())
                if row is not None and row["ok"]:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Timed out after {timeout_s}s waiting for the migration lock. "
                        "Another process may be stuck mid-migration."
                    )
                log.info("waiting for the migration lock")
                await asyncio.sleep(poll_s)
            try:
                yield
            finally:
                await conn.execute("select pg_advisory_unlock(%s)", (key,))

    async def ping(self) -> bool:
        try:
            async with self._pool.connection(timeout=2) as conn:
                await conn.execute("select 1")
        except Exception:
            return False
        return True

    # -- queries ------------------------------------------------------------

    async def fetch_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cast("dict[str, Any] | None", await cur.fetchone())

    async def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cast("list[dict[str, Any]]", await cur.fetchall())

    async def execute(self, sql: str, params: Any = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(sql, params)

    async def connection(self) -> AsyncConnection[Any]:  # pragma: no cover - convenience
        return await self._pool.getconn()
