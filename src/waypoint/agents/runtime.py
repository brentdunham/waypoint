"""Process-wide runtime: everything expensive, built once, on the shared loop.

Order matters. The Postgres pool has to exist before the checkpointer and store
can bind to it; the checkpointer and store have to exist before graphs compile;
graphs have to exist before the run manager can drive them. All of it is built
in the app's lifespan, on the server's event loop, which every request handler
and every run task shares.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from waypoint.agents.models import (
    build_contextualizer,
    build_embeddings,
    build_rag_embeddings,
    build_reranker,
    check_embedding_dims,
    check_model_config,
    check_reranker,
)
from waypoint.agents.registry import build_all, roles_by_agent
from waypoint.agents.types import Graph
from waypoint.infra import redis as redis_infra
from waypoint.infra.bus import EventBus, MemoryBus, RedisBus
from waypoint.infra.cache import RedisCache
from waypoint.infra.db import Database
from waypoint.infra.ratelimit import Limiter
from waypoint.infra.runs import RunManager
from waypoint.infra.security import KeyStore, parse_key_id
from waypoint.infra.threads import ThreadStore
from waypoint.rag.seed import describe, seed_public_corpus
from waypoint.rag.store import CorpusStore, MemoryCorpusStore, PgCorpusStore
from waypoint.settings import Settings

log = logging.getLogger(__name__)

STARTUP_TIMEOUT_S = 90
SHUTDOWN_TIMEOUT_S = 20


@dataclass(slots=True)
class AgentRuntime:
    settings: Settings
    graphs: dict[str, Graph]
    runs: RunManager
    threads: ThreadStore
    keys: KeyStore | None
    db: Database | None
    limiter: Limiter | None
    bus: EventBus
    corpora: CorpusStore = field(default_factory=MemoryCorpusStore)
    redis: Any = None
    store: Any = None
    checkpointer: Any = None
    _closers: list[Any] = field(default_factory=list)

    def graph(self, name: str) -> Graph:
        from waypoint.errors import NotFound

        try:
            return self.graphs[name]
        except KeyError:
            raise NotFound(f"No agent named {name!r}.", available=sorted(self.graphs)) from None

    async def health(self) -> dict[str, str]:
        checks = {"graphs": "ok" if self.graphs else "empty"}
        checks["postgres"] = (
            "ok" if self.db and await self.db.ping() else ("off" if not self.db else "down")
        )
        if self.redis is None:
            checks["redis"] = "off"
        else:
            checks["redis"] = "ok" if await redis_infra.ping(self.redis) else "down"
        return checks

    async def aclose(self) -> None:
        await self.runs.aclose()
        for closer in reversed(self._closers):
            with contextlib.suppress(Exception):
                await closer()
        self._closers.clear()


async def create_runtime(settings: Settings) -> AgentRuntime:
    # Before any connection opens: a typo in MODELS__* is a config error, and
    # finding it at the first run of that agent would be far from the cause.
    check_model_config(settings, roles_by_agent())
    closers: list[Any] = []
    db: Database | None = None
    redis_client: Any = None
    checkpointer: Any
    store: Any
    cache: Any = None
    limiter: Limiter | None = None

    # -- Postgres: checkpointer + long-term store + our own tables ---------
    if settings.pg_dsn:
        db = await Database(
            settings.pg_dsn, min_size=settings.db_pool_min, max_size=settings.db_pool_max
        ).open()
        closers.append(db.close)

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore

        index = await _memory_index(settings)
        checkpointer = AsyncPostgresSaver(db.pool)  # type: ignore[arg-type]
        store = AsyncPostgresStore(db.pool, index=index)  # type: ignore[arg-type]

        # All schema work behind one advisory lock. Workers boot in parallel,
        # and LangGraph's own migrations are not safe to run concurrently.
        async with db.advisory_lock():
            await db.migrate(embedding_dims=settings.rag.embeddings_dims)
            await checkpointer.setup()
            await store.setup()

        log.info("using postgres checkpointer and store")
    else:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        checkpointer = InMemorySaver()
        store = InMemoryStore(index=await _memory_index(settings))  # type: ignore[arg-type]
        log.warning("DATABASE_URL unset: state is in memory and dies with this process")

    # -- Redis: event bus, limits, node cache ------------------------------
    if settings.redis_dsn:
        redis_client = redis_infra.create_client(
            settings.redis_dsn, socket_timeout_s=settings.redis_socket_timeout_s
        )
        await redis_client.ping()
        bus: EventBus = RedisBus(
            redis_client, maxlen=settings.stream.maxlen, ttl_s=settings.stream.ttl_s
        )
        limiter = Limiter(
            redis_client,
            rpm=settings.limits.rpm,
            concurrent=settings.limits.concurrent_runs,
            lease_s=settings.limits.run_timeout_s + 30,
        )
        if settings.node_cache_ttl_s:
            cache = RedisCache(redis_client, default_ttl=settings.node_cache_ttl_s)
        closers.append(redis_client.aclose)
        log.info("using redis event bus, limiter and node cache")
    else:
        bus = MemoryBus(maxlen=settings.stream.maxlen)
        log.warning("REDIS_URL unset: single-process event bus, no rate limits")

    # -- graphs -------------------------------------------------------------
    compile_kwargs: dict[str, Any] = {"checkpointer": checkpointer, "store": store}
    if cache is not None:
        compile_kwargs["cache"] = cache
    corpora = await _corpus_store(settings, db)
    graphs = build_all(settings, corpora=corpora, **compile_kwargs)

    runs = RunManager(bus=bus, db=db, limiter=limiter, settings=settings, graphs=graphs)
    await runs.start_listener()

    keys = KeyStore(db) if db is not None else None
    if keys is not None:
        await _seed_bootstrap_key(keys, db, settings)

    _start_seeding(settings, db, closers)

    return AgentRuntime(
        settings=settings,
        graphs=graphs,
        runs=runs,
        threads=ThreadStore(db),
        keys=keys,
        db=db,
        limiter=limiter,
        bus=bus,
        corpora=corpora,
        redis=redis_client,
        store=store,
        checkpointer=checkpointer,
        _closers=closers,
    )


def _start_seeding(settings: Settings, db: Database | None, closers: list[Any]) -> None:
    """Index ``RAG__SEED_DIR`` in the background, cancelled at shutdown.

    The API serves while the corpus is indexed; a seeding failure is logged,
    never fatal.
    """
    if not (settings.rag.seed_dir and settings.rag.enabled):
        return
    if db is None:
        log.warning("RAG__SEED_DIR is set but DATABASE_URL is not: nothing seeded")
        return
    seeding = asyncio.create_task(_seed_corpus(settings, db), name="rag-seed")

    async def stop_seeding() -> None:
        seeding.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await seeding

    closers.append(stop_seeding)


async def _seed_corpus(settings: Settings, db: Database) -> None:
    """``RAG__SEED_DIR`` into the public ``RAG__SEED_CORPUS`` (``rag/seed.py``)."""
    embeddings = build_rag_embeddings(settings)
    if embeddings is None:
        return
    try:
        result = await seed_public_corpus(db, settings, embeddings, build_contextualizer(settings))
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("seeding the public corpus failed")
        return
    if result is not None:
        level = logging.WARNING if result.failed else logging.INFO
        log.log(
            level,
            describe(result, settings.rag.seed_corpus),
            extra={"indexed": result.indexed, "removed": result.removed, "failed": result.failed},
        )


async def _memory_index(settings: Settings) -> dict[str, Any] | None:
    """The store's vector index config, or None to run without semantic search."""
    embeddings = build_embeddings(settings)
    if embeddings is None:
        log.warning(
            "EMBEDDINGS_MODEL unset: memory search ignores the query and returns arbitrary facts"
        )
        return None
    if not settings.fake_model:
        await check_embedding_dims(embeddings, settings.embeddings_dims)
    log.info(
        "memory vector search on",
        extra={"embeddings": settings.embeddings_model, "dims": settings.embeddings_dims},
    )
    return {"dims": settings.embeddings_dims, "embed": embeddings, "fields": ["fact"]}


async def _corpus_store(settings: Settings, db: Database | None) -> CorpusStore:
    """The corpus store, after failing the boot on a bad RAG key or width.

    Checks, as for the memory index: the provider returns the configured width
    (one embedding call), the existing ``chunks.embedding`` column has that
    width too, and with ``RAG__RERANK`` the reranker accepts the key.
    """
    corpora: CorpusStore = PgCorpusStore(db) if db is not None else MemoryCorpusStore()
    embeddings = build_rag_embeddings(settings)
    if embeddings is None:
        log.info("RAG__EMBEDDINGS_MODEL unset: document retrieval is off")
        return corpora
    if db is not None:
        await db.check_embedding_dims(settings.rag.embeddings_dims)
    reranker = build_reranker(settings)  # no VOYAGE_API_KEY with RAG__RERANK on: stops here
    if not settings.fake_model:
        await check_embedding_dims(
            embeddings, settings.rag.embeddings_dims, setting="RAG__EMBEDDINGS_DIMS"
        )
        if reranker is not None:
            await check_reranker(reranker)
    log.info(
        "document retrieval on",
        extra={
            "embeddings": settings.rag.embeddings_model,
            "dims": settings.rag.embeddings_dims,
            "rerank": settings.rag.rerank_model if reranker is not None else None,
        },
    )
    return corpora


async def _seed_bootstrap_key(keys: KeyStore, db: Database | None, settings: Settings) -> None:
    """Register the one key that exists before any key exists.

    Minting a key needs the ``keys:admin`` scope, so something has to break the
    cycle. This does -- but never with a value published in the repository.
    """
    if not settings.bootstrap_api_key:
        row = await db.fetch_one("select count(*) as n from api_keys") if db else None
        if row is not None and not row["n"]:
            log.warning(
                "no API keys exist and BOOTSTRAP_API_KEY is unset: every request will 401. "
                "Set BOOTSTRAP_API_KEY once, then mint real keys with POST /api/v1/keys."
            )
        return

    if settings.bootstrap_is_placeholder:
        # Refusing beats seeding: the placeholder is printed in .env.example, so
        # anyone who has read the repository would hold a full-scope key.
        log.error(
            "BOOTSTRAP_API_KEY is still the example placeholder and was NOT seeded. "
            'Generate a real one:  python -c "import secrets; '
            "print(f'ak_{secrets.token_hex(6)}.{secrets.token_urlsafe(32)}')\"  "
            "then put it in .env and restart."
        )
        return

    await keys.upsert_raw(
        settings.bootstrap_api_key,
        tenant_id="bootstrap",
        user_id="bootstrap",
        scopes=["*"],
        label="BOOTSTRAP_API_KEY",
    )
    # key_id is the public half of the key. Logging it is how you confirm which
    # key is live without going digging in the database.
    log.warning(
        "bootstrap API key active (key_id=%s) -- unset BOOTSTRAP_API_KEY in production",
        parse_key_id(settings.bootstrap_api_key) or "?",
    )


async def start_runtime(settings: Settings) -> AgentRuntime:
    async with asyncio.timeout(STARTUP_TIMEOUT_S):
        runtime = await create_runtime(settings)
    log.info(
        "agent runtime ready",
        extra={"agents": sorted(runtime.graphs), "model": settings.model},
    )
    return runtime


async def stop_runtime(runtime: AgentRuntime) -> None:
    """Cancel runs in flight and close pools. Bounded, so shutdown cannot hang."""
    with contextlib.suppress(Exception):
        async with asyncio.timeout(SHUTDOWN_TIMEOUT_S):
            await runtime.aclose()


def get_runtime(request: Request) -> AgentRuntime:
    """FastAPI dependency: the runtime the lifespan built."""
    runtime: AgentRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only during startup races
        from waypoint.errors import ServiceUnavailable

        raise ServiceUnavailable("Agent runtime is still starting.")
    return runtime
