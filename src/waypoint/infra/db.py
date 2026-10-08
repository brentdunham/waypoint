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
"""


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

    async def migrate(self) -> None:
        """Create this service's own tables. LangGraph creates its own separately."""
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)
        log.info("database schema ready")

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
