"""A Redis-backed LangGraph node cache.

Nodes marked with a ``CachePolicy`` skip re-execution when their input is
unchanged. Backing that with Redis instead of process memory means the cache
survives restarts and is shared by every worker -- which is the difference
between a cache and a coincidence.
"""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast, override

from langgraph.cache.base import BaseCache, FullKey, Namespace
from redis.asyncio import Redis

log = logging.getLogger(__name__)

PREFIX = "lgcache"


def _redis_key(full_key: FullKey) -> str:
    namespace, key = full_key
    return f"{PREFIX}:{'/'.join(namespace)}:{key}"


class RedisCache(BaseCache[Any]):
    """Async-only: this service never runs a graph on the calling thread."""

    def __init__(self, client: Redis, *, default_ttl: int | None = None) -> None:
        super().__init__()
        self._redis = client
        self._default_ttl = default_ttl

    @override
    async def aget(self, keys: Sequence[FullKey]) -> dict[FullKey, Any]:
        if not keys:
            return {}
        raw = await self._redis.mget([_redis_key(k) for k in keys])
        out: dict[FullKey, Any] = {}
        for full_key, blob in zip(keys, raw, strict=True):
            if blob is None:
                continue
            try:
                enc, _, payload = cast("bytes", blob).partition(b"\0")
                out[full_key] = self.serde.loads_typed((enc.decode(), payload))
            except Exception:
                log.warning("dropping undecodable cache entry", extra={"key": _redis_key(full_key)})
        return out

    @override
    async def aset(self, pairs: Mapping[FullKey, tuple[Any, int | None]]) -> None:
        if not pairs:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for full_key, (value, ttl) in pairs.items():
                enc, payload = self.serde.dumps_typed(value)
                blob = enc.encode() + b"\0" + payload
                pipe.set(_redis_key(full_key), blob, ex=ttl or self._default_ttl or None)
            await pipe.execute()

    @override
    async def aclear(self, namespaces: Sequence[Namespace] | None = None) -> None:
        patterns = (
            [f"{PREFIX}:{'/'.join(ns)}:*" for ns in namespaces] if namespaces else [f"{PREFIX}:*"]
        )
        for pattern in patterns:
            async for key in self._redis.scan_iter(match=pattern, count=500):
                await self._redis.delete(key)

    # -- sync surface -------------------------------------------------------
    # LangGraph calls these only when a graph is invoked synchronously, which
    # this service never does: every graph runs on the server's event loop.
    # Blocking that loop on Redis would stall every request, so refuse loudly.

    @override
    def get(self, keys: Sequence[FullKey]) -> dict[FullKey, Any]:
        raise NotImplementedError(_SYNC)

    @override
    def set(self, pairs: Mapping[FullKey, tuple[Any, int | None]]) -> None:
        raise NotImplementedError(_SYNC)

    @override
    def clear(self, namespaces: Sequence[Namespace] | None = None) -> None:
        raise NotImplementedError(_SYNC)


_SYNC = "RedisCache is async-only; invoke graphs with ainvoke/astream."
