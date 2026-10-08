"""Process-wide runtime: everything expensive, built once, on the shared loop.

Order matters. The Postgres pool has to exist before the checkpointer and store
can bind to it; the checkpointer and store have to exist before graphs compile;
graphs have to exist before the run manager can drive them. All of it happens
on ``app.aio.loop`` so that every loop-bound object agrees about which loop it
lives on.
"""

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from flask import Flask, current_app

from waypoint.agents.models import build_embeddings, check_embedding_dims, check_model_config
from waypoint.agents.registry import build_all, roles_by_agent
from waypoint.agents.types import Graph
from waypoint.aio import loop
from waypoint.infra import redis as redis_infra
from waypoint.infra.bus import EventBus, MemoryBus, RedisBus
from waypoint.infra.cache import RedisCache
from waypoint.infra.db import Database
from waypoint.infra.ratelimit import Limiter
from waypoint.infra.runs import RunManager
from waypoint.infra.security import KeyStore, parse_key_id
from waypoint.infra.threads import ThreadStore
from waypoint.settings import Settings

log = logging.getLogger(__name__)

EXTENSION_KEY = "agents.runtime"


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


async def _create(settings: Settings) -> AgentRuntime:
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
            await db.migrate()
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
    graphs = build_all(settings, **compile_kwargs)

    runs = RunManager(bus=bus, db=db, limiter=limiter, settings=settings, graphs=graphs)
    await runs.start_listener()

    keys = KeyStore(db) if db is not None else None
    if keys is not None:
        await _seed_bootstrap_key(keys, db, settings)

    return AgentRuntime(
        settings=settings,
        graphs=graphs,
        runs=runs,
        threads=ThreadStore(db),
        keys=keys,
        db=db,
        limiter=limiter,
        bus=bus,
        redis=redis_client,
        store=store,
        checkpointer=checkpointer,
        _closers=closers,
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


def init_runtime(app: Flask, settings: Settings) -> AgentRuntime:
    loop.start()
    runtime = loop.run_sync(_create(settings), timeout=90)
    app.extensions[EXTENSION_KEY] = runtime
    app.extensions["keystore"] = runtime.keys
    log.info(
        "agent runtime ready",
        extra={"agents": sorted(runtime.graphs), "model": settings.model},
    )
    return runtime


def shutdown_runtime(app: Flask) -> None:
    runtime: AgentRuntime | None = app.extensions.pop(EXTENSION_KEY, None)
    if runtime is not None:
        with contextlib.suppress(Exception):
            loop.run_sync(runtime.aclose(), timeout=20)


def get_runtime() -> AgentRuntime:
    runtime = current_app.extensions.get(EXTENSION_KEY)
    if runtime is None:  # pragma: no cover - only during startup races
        from waypoint.errors import ServiceUnavailable

        raise ServiceUnavailable("Agent runtime is still starting.")
    return runtime  # type: ignore[no-any-return]
