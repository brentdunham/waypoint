"""Process-wide runtime: everything expensive, built once, on the shared loop.

Two roles share it (docs/design/queue.md). The API (``role="api"``) admits
runs and puts them on the queue; with Redis it never drives one. The worker
(``role="worker"``, ``python -m waypoint.worker``) takes runs off the queue
and drives them, so it alone discovers MCP tools and listens for cancels.
Without Redis there is no shared queue, so the API process runs a worker of
its own.

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
from typing import Any, Literal

from fastapi import Request

from waypoint.agents.mcp import McpCatalog
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
from waypoint.infra.queue import MemoryRunQueue, RedisRunQueue, RunQueue
from waypoint.infra.quota import MemoryUploadQuota, RedisUploadQuota, UploadQuota
from waypoint.infra.ratelimit import Limiter
from waypoint.infra.runs import ALIVE_FILE, RunManager, RunWorker
from waypoint.infra.security import KeyStore, parse_key_id
from waypoint.infra.threads import ThreadStore
from waypoint.rag.seed import describe, seed_public_corpus
from waypoint.rag.store import CorpusStore, MemoryCorpusStore, PgCorpusStore
from waypoint.settings import Settings
from waypoint.tracing import NOOP, Tracing

log = logging.getLogger(__name__)

STARTUP_TIMEOUT_S = 90
SHUTDOWN_TIMEOUT_S = 20

type Role = Literal["api", "worker"]


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
    quota: UploadQuota | None = None
    """Daily upload limits (``UPLOADS__*``); ``None`` only in hand-built runtimes."""
    mcp: McpCatalog | None = None
    """Tools from ``MCP__SERVERS``, discovered in the background by the
    process that drives runs."""
    queue: RunQueue = field(default_factory=MemoryRunQueue)
    worker: RunWorker | None = None
    """This process's worker: always in ``role="worker"``, and in the API
    when there is no Redis to share a queue through."""
    role: Role = "api"
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
        if self.worker is not None:
            # Shown, never "down": a tool server is optional and must not take
            # the service out of rotation (docs/design/tools.md, T7).
            for name, state in (self.mcp.status() if self.mcp else {}).items():
                checks[f"mcp:{name}"] = "unavailable" if state["error"] else "ok"
        else:
            checks |= await self._worker_checks()
        return checks

    async def _worker_checks(self) -> dict[str, str]:
        """What the workers report: how many are alive, the queue, MCP servers.

        Never "down": with no worker the API still answers and accepts runs,
        which wait in the queue; ``unavailable`` says why they wait.
        """
        try:
            workers = await self.queue.workers()
            stats = await self.queue.stats()
        except Exception:
            return {"workers": "unknown"}
        checks = {
            "workers": f"ok ({len(workers)})" if workers else "unavailable",
            "queue": f"{stats['queued']} queued, {stats['running']} running",
        }
        servers: dict[str, bool] = {}
        for worker in workers:
            for name, state in (worker.get("mcp") or {}).items():
                servers[name] = servers.get(name, False) or not state.get("error")
        if workers:
            for server in self.settings.mcp.servers:
                checks[f"mcp:{server.name}"] = "ok" if servers.get(server.name) else "unavailable"
        return checks

    async def aclose(self) -> None:
        if self.worker is not None:
            await self.worker.aclose()
        await self.runs.aclose()
        for closer in reversed(self._closers):
            with contextlib.suppress(Exception):
                await closer()
        self._closers.clear()


async def create_runtime(
    settings: Settings, *, tracing: Tracing = NOOP, role: Role = "api"
) -> AgentRuntime:
    # Before any connection opens: a typo in MODELS__* is a config error, and
    # finding it at the first run of that agent would be far from the cause.
    check_model_config(settings, roles_by_agent())
    _check_role(settings, role)
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
            budget=settings.budget,
        )
        if settings.node_cache_ttl_s:
            cache = RedisCache(redis_client, default_ttl=settings.node_cache_ttl_s)
        closers.append(redis_client.aclose)
        log.info("using redis event bus, limiter and node cache")
    else:
        bus = MemoryBus(maxlen=settings.stream.maxlen)
        log.warning("REDIS_URL unset: single-process event bus, no rate limits")

    # -- graphs -------------------------------------------------------------
    compile_kwargs: dict[str, Any] = {"checkpointer": checkpointer, "store": store} | (
        {"cache": cache} if cache is not None else {}
    )
    drives_runs = role == "worker" or redis_client is None
    corpora = await _corpus_store(settings, db)
    quota = _upload_quota(settings, redis_client)
    mcp = _mcp_catalog(settings, closers, discover=drives_runs)
    graphs = build_all(settings, corpora=corpora, quota=quota, mcp=mcp, **compile_kwargs)

    queue, runs, worker = await _runs(
        settings,
        role,
        redis_client=redis_client,
        bus=bus,
        db=db,
        limiter=limiter,
        graphs=graphs,
        mcp=mcp,
        tracing=tracing,
        drives_runs=drives_runs,
    )

    keys = KeyStore(db) if db is not None else None
    if role == "api":
        await _api_only(settings, db, keys, closers)

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
        quota=quota,
        mcp=mcp,
        queue=queue,
        worker=worker,
        role=role,
        redis=redis_client,
        store=store,
        checkpointer=checkpointer,
        _closers=closers,
    )


def _check_role(settings: Settings, role: Role) -> None:
    if role == "worker" and not settings.redis_dsn:
        raise RuntimeError(
            "The worker needs REDIS_URL: it takes runs from the queue the API "
            "writes to. Without Redis, the API runs its own runs."
        )


async def _api_only(
    settings: Settings, db: Database | None, keys: KeyStore | None, closers: list[Any]
) -> None:
    """The bootstrap key and the seeded corpus: once, by the API, not per worker."""
    if keys is not None:
        await _seed_bootstrap_key(keys, db, settings)
    _start_seeding(settings, db, closers)


async def _runs(
    settings: Settings,
    role: Role,
    *,
    redis_client: Any,
    bus: EventBus,
    db: Database | None,
    limiter: Limiter | None,
    graphs: dict[str, Graph],
    mcp: McpCatalog,
    tracing: Tracing,
    drives_runs: bool,
) -> tuple[RunQueue, RunManager, RunWorker | None]:
    """The queue, the API's run manager and, if this process drives runs, its worker."""
    queue: RunQueue = RedisRunQueue(redis_client) if redis_client else MemoryRunQueue()
    await queue.ensure()  # the group exists before the first run is queued
    runs = RunManager(
        bus=bus,
        db=db,
        limiter=limiter,
        settings=settings,
        graphs=graphs,
        queue=queue,
        tracing=tracing,
    )
    worker = None
    if drives_runs:
        worker = RunWorker(
            queue=queue,
            bus=bus,
            db=db,
            limiter=limiter,
            settings=settings,
            graphs=graphs,
            tracing=tracing,
            status=lambda: {"mcp": mcp.status()},
            alive_file=ALIVE_FILE if role == "worker" else None,
            drain=role == "worker",
        )
        await worker.start()

    return queue, runs, worker


def _upload_quota(settings: Settings, redis_client: Any) -> UploadQuota:
    if redis_client is not None:
        return RedisUploadQuota(redis_client, settings.uploads)
    return MemoryUploadQuota(settings.uploads)


def _mcp_catalog(settings: Settings, closers: list[Any], *, discover: bool) -> McpCatalog:
    """Started at once and discovering in the background: a server that is down
    never holds up boot (T7). The graphs read it per run, so tools found later
    are offered from then on. Only a process that drives runs discovers; the
    API's catalog stays empty and ``/readyz`` reports what the workers see."""
    mcp = McpCatalog(settings.mcp)
    if discover:
        mcp.start()
        closers.append(mcp.aclose)
    return mcp


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


async def start_runtime(
    settings: Settings, *, tracing: Tracing = NOOP, role: Role = "api"
) -> AgentRuntime:
    async with asyncio.timeout(STARTUP_TIMEOUT_S):
        runtime = await create_runtime(settings, tracing=tracing, role=role)
    log.info(
        "agent runtime ready",
        extra={
            "agents": sorted(runtime.graphs),
            "model": settings.model,
            "role": role,
            "drives_runs": runtime.worker is not None,
        },
    )
    return runtime


async def stop_runtime(runtime: AgentRuntime) -> None:
    """Drain this process's runs, if it drives any, and close pools. Bounded,
    so shutdown cannot hang."""
    # A worker first gives its runs QUEUE__DRAIN_S to end.
    drain = runtime.worker.drain_s if runtime.worker is not None else 0
    with contextlib.suppress(Exception):
        async with asyncio.timeout(SHUTDOWN_TIMEOUT_S + drain):
            await runtime.aclose()


def get_runtime(request: Request) -> AgentRuntime:
    """FastAPI dependency: the runtime the lifespan built."""
    runtime: AgentRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only during startup races
        from waypoint.errors import ServiceUnavailable

        raise ServiceUnavailable("Agent runtime is still starting.")
    return runtime
