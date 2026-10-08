"""Per-tenant rate limiting and concurrency slots, enforced in Redis.

Both are Lua so they are atomic across workers -- a check-then-set in Python
would let two workers hand out the same last slot.

* :meth:`Limiter.take` is a token bucket: smooth, allows a burst up to the
  bucket size, refills continuously. Fairer than a fixed window, which lets a
  caller spend a whole minute's budget in the last second and the next
  minute's in the first.
* :meth:`Limiter.slot` is a counting semaphore with a lease TTL, so a worker
  that dies mid-run cannot leak a slot forever.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis

from waypoint.errors import RateLimited

log = logging.getLogger(__name__)

# KEYS[1]=bucket  ARGV=[rate_per_sec, capacity, now, cost]
_TOKEN_BUCKET = """
local bucket = KEYS[1]
local rate, capacity = tonumber(ARGV[1]), tonumber(ARGV[2])
local now, cost = tonumber(ARGV[3]), tonumber(ARGV[4])
local state = redis.call('HMGET', bucket, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 end
redis.call('HSET', bucket, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', bucket, math.ceil(capacity / rate) + 60)
local retry = 0
if allowed == 0 then retry = (cost - tokens) / rate end
return {allowed, tostring(retry)}
"""

# KEYS[1]=slots(zset)  ARGV=[limit, now, ttl, member]
_ACQUIRE_SLOT = """
local key = KEYS[1]
local limit, now, ttl, member = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
if redis.call('ZCARD', key) >= limit then return 0 end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, ttl + 60)
return 1
"""


@dataclass(slots=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0


class Limiter:
    def __init__(self, client: Redis, *, rpm: int, concurrent: int, lease_s: float) -> None:
        self._redis = client
        self._rpm = rpm
        self._concurrent = concurrent
        self._lease_s = lease_s
        self._take = client.register_script(_TOKEN_BUCKET)
        self._acquire = client.register_script(_ACQUIRE_SLOT)

    async def take(self, tenant_id: str, *, cost: float = 1.0) -> Decision:
        now = await self._now()
        allowed, retry = await self._take(
            keys=[f"rl:{{{tenant_id}}}:bucket"],
            args=[self._rpm / 60.0, self._rpm, now, cost],
        )
        return Decision(bool(allowed), float(retry))

    async def enforce(self, tenant_id: str, *, cost: float = 1.0) -> None:
        decision = await self.take(tenant_id, cost=cost)
        if not decision.allowed:
            raise RateLimited(
                f"Rate limit of {self._rpm}/min exceeded.",
                retry_after=round(decision.retry_after_s, 2),
            )

    @asynccontextmanager
    async def slot(self, tenant_id: str) -> AsyncIterator[str]:
        """Hold one of the tenant's concurrent-run slots for the duration."""
        key = f"rl:{{{tenant_id}}}:slots"
        member = uuid.uuid4().hex
        now = await self._now()
        ok = await self._acquire(keys=[key], args=[self._concurrent, now, self._lease_s, member])
        if not ok:
            raise RateLimited(
                f"Tenant already has {self._concurrent} runs in flight.",
                concurrent_limit=self._concurrent,
            )
        try:
            yield member
        finally:
            await self._redis.zrem(key, member)

    async def in_flight(self, tenant_id: str) -> int:
        now = await self._now()
        key = f"rl:{{{tenant_id}}}:slots"
        await self._redis.zremrangebyscore(key, "-inf", now - self._lease_s)
        return int(await self._redis.zcard(key))

    async def _now(self) -> float:
        """Redis server time, so all workers agree even with clock skew."""
        seconds, micros = await self._redis.time()
        return float(seconds) + float(micros) / 1_000_000.0
