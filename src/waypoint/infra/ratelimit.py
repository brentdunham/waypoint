"""Per-tenant rate limiting and concurrency slots, enforced in Redis.

Both are Lua so they are atomic across workers -- a check-then-set in Python
would let two workers hand out the same last slot.

* :meth:`Limiter.take` is a token bucket: smooth, allows a burst up to the
  bucket size, refills continuously. Fairer than a fixed window, which lets a
  caller spend a whole minute's budget in the last second and the next
  minute's in the first.
* :meth:`Limiter.slot` is a counting semaphore with a lease TTL, so a worker
  that dies mid-run cannot leak a slot forever.
* :meth:`Limiter.admit` and :meth:`Limiter.charge` are the daily spending
  budgets (``BUDGET__*``): a bucket of US dollars per tenant and one for the
  whole server, refilling continuously. Admit before a run starts, charge
  each model call's cost when it ends (observability design, O3 and O4).
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis

from waypoint.errors import OverBudget, RateLimited
from waypoint.settings import BudgetSettings

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


# KEYS: budget buckets (the tenant's, then the global one; either may be
# absent). ARGV: [now, usd, capacity per key]. With usd = 0 this only reads
# (admission); with usd > 0 it charges every key. A bucket refills at
# capacity / 86400 per second up to capacity, and may go below zero.
# Returns {allowed, retry_after, index of the first empty key or 0, balance per key}.
_BUDGET = """
local now, usd = tonumber(ARGV[1]), tonumber(ARGV[2])
local allowed, retry, blocked = 1, 0, 0
local balances = {}
for i, key in ipairs(KEYS) do
  local cap = tonumber(ARGV[2 + i])
  local rate = cap / 86400
  local state = redis.call('HMGET', key, 'usd', 'ts')
  local bal, ts = tonumber(state[1]), tonumber(state[2])
  if bal == nil then bal = cap; ts = now end
  bal = math.min(cap, bal + math.max(0, now - ts) * rate)
  if usd > 0 then
    bal = bal - usd
    redis.call('HSET', key, 'usd', tostring(bal), 'ts', tostring(now))
    -- Kept until it would have refilled to full anyway, plus a minute.
    redis.call('EXPIRE', key, math.ceil((cap - bal) / rate) + 60)
  elseif bal <= 0 then
    allowed = 0
    if blocked == 0 then blocked = i end
    local wait = (0 - bal) / rate + 1
    if wait > retry then retry = wait end
  end
  balances[#balances + 1] = tostring(bal)
end
return {allowed, tostring(retry), blocked, unpack(balances)}
"""

DAY_S = 86_400
GLOBAL_TENANT = "global"
"""The global budget's key is ``rl:{global}:usd``. Runs on one Redis, not a
cluster: the script touches a tenant's key and this one together."""


@dataclass(slots=True)
class Balance:
    """A tenant's budget and what is left of it now."""

    budget_usd_per_day: float
    balance_usd: float


@dataclass(slots=True)
class Decision:
    allowed: bool
    retry_after_s: float = 0.0


class Limiter:
    def __init__(
        self,
        client: Redis,
        *,
        rpm: int,
        concurrent: int,
        lease_s: float,
        budget: BudgetSettings | None = None,
    ) -> None:
        self._redis = client
        self._rpm = rpm
        self._concurrent = concurrent
        self._lease_s = lease_s
        self._budget = budget if budget is not None and budget.active else None
        self._take = client.register_script(_TOKEN_BUCKET)
        self._acquire = client.register_script(_ACQUIRE_SLOT)
        self._spend = client.register_script(_BUDGET)

    # -- spending budgets ----------------------------------------------------------

    @property
    def budgets_on(self) -> bool:
        return self._budget is not None

    def _buckets(self, tenant_id: str) -> list[tuple[str, float]]:
        """(key, daily budget) for each bucket this tenant draws on; empty if none."""
        budget = self._budget
        if budget is None or budget.exempt(tenant_id):
            return []
        buckets: list[tuple[str, float]] = []
        if (own := budget.tenant_budget(tenant_id)) is not None:
            buckets.append((f"rl:{{{tenant_id}}}:usd", own))
        assert budget.global_usd_per_day is not None  # active means both are set
        buckets.append((f"rl:{{{GLOBAL_TENANT}}}:usd", budget.global_usd_per_day))
        return buckets

    async def _run_budget(
        self, buckets: list[tuple[str, float]], usd: float
    ) -> tuple[bool, float, int, list[float]]:
        now = await self._now()
        allowed, retry, blocked, *balances = await self._spend(
            keys=[key for key, _ in buckets], args=[now, usd, *(cap for _, cap in buckets)]
        )
        return bool(allowed), float(retry), int(blocked), [float(b) for b in balances]

    async def admit(self, tenant_id: str) -> None:
        """Raise :class:`OverBudget` unless the tenant's and the server's balances
        are above zero. Exempt tenants, and every tenant with budgets off, pass."""
        buckets = self._buckets(tenant_id)
        if not buckets:
            return
        allowed, retry, blocked, _ = await self._run_budget(buckets, 0.0)
        if allowed:
            return
        retry = round(retry, 0)
        if buckets[blocked - 1][0].startswith(f"rl:{{{GLOBAL_TENANT}}}"):
            # Reviewers see that the server is over budget, not how far (D4).
            raise OverBudget(
                "This demo has reached its daily spending limit. It refills over the day.",
                scope="server",
                retry_after=retry,
            )
        raise OverBudget(
            f"This key's daily budget (${buckets[blocked - 1][1]:.2f}) is used up. "
            "It refills continuously over the day.",
            scope="tenant",
            retry_after=retry,
        )

    async def charge(self, tenant_id: str, usd: float) -> None:
        """Take ``usd`` off every bucket the tenant draws on. May go below zero."""
        buckets = self._buckets(tenant_id)
        if buckets and usd > 0:
            await self._run_budget(buckets, usd)

    async def balance(self, tenant_id: str) -> Balance | None:
        """The tenant's own budget and balance; ``None`` if it has none."""
        budget = self._budget.tenant_budget(tenant_id) if self._budget else None
        if budget is None or self._budget is None or self._budget.exempt(tenant_id):
            return None
        key = f"rl:{{{tenant_id}}}:usd"
        _, _, _, (balance,) = await self._run_budget([(key, budget)], 0.0)
        return Balance(budget_usd_per_day=budget, balance_usd=balance)

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
