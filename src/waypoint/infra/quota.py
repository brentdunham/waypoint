"""Daily upload limits per tenant (``UPLOADS__*``, docs/design/tools.md T13).

Three counters per tenant per UTC day: files, bytes and chunks. Files and
bytes are taken when an upload arrives, before anything is stored; chunks are
taken by the ingest run once a document is chunked, before it is embedded, so
a refused document costs nothing. Taking is all or nothing: a request that
would pass any limit takes nothing and is refused with :class:`OverQuota`,
whose ``retry_after`` is the next midnight UTC.

A fixed UTC day, not a refilling bucket like the dollar budgets: a reviewer
reads "10 files a day, reset at midnight UTC" and can plan around it, and an
upload is a few large events rather than a steady flow that a bucket smooths.

Redis holds the counters when it is configured, so they hold across workers;
the check and the increment are one Lua script. Without Redis (development,
tests) they live in this process. Admin keys are exempt; the callers decide
that, this module only counts.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from waypoint.errors import OverQuota
from waypoint.settings import UploadSettings

FILES, BYTES, CHUNKS = "files", "bytes", "chunks"
MB = 1024 * 1024
KEEP_S = 2 * 86_400
"""A day's counters are kept a day longer than they are used, then expire."""

# KEYS[1]=the tenant's hash for the day. ARGV=[ttl, then (field, amount, limit) per counter].
# Returns {0, ''} when taken, or {index of the first counter over its limit, used so far}.
# Negative amounts give back and are never refused; no counter goes below zero.
_TAKE = """
local key, ttl = KEYS[1], tonumber(ARGV[1])
local n = (#ARGV - 1) / 3
for i = 0, n - 1 do
  local amount, limit = tonumber(ARGV[3 + 3 * i]), tonumber(ARGV[4 + 3 * i])
  local used = tonumber(redis.call('HGET', key, ARGV[2 + 3 * i]) or '0')
  if amount > 0 and used + amount > limit then return {i + 1, tostring(used)} end
end
for i = 0, n - 1 do
  local field = ARGV[2 + 3 * i]
  if redis.call('HINCRBY', key, field, ARGV[3 + 3 * i]) < 0 then
    redis.call('HSET', key, field, 0)
  end
end
redis.call('EXPIRE', key, ttl)
return {0, ''}
"""


@dataclass(frozen=True, slots=True)
class Usage:
    """What a tenant has used today, and the limits."""

    files: int
    bytes: int
    chunks: int
    files_per_day: int
    bytes_per_day: int
    chunks_per_day: int
    resets_at: str
    """The next midnight UTC, ISO 8601."""


type Counter = tuple[str, int, int]
"""(field, amount, limit)."""


def _day(now: dt.datetime) -> str:
    return now.strftime("%Y%m%d")


def _until_midnight(now: dt.datetime) -> tuple[float, dt.datetime]:
    midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds(), midnight


def _mb(n: int) -> str:
    return f"{n / MB:.1f} MB"


class UploadQuota:
    """The counters, with the messages a refusal carries. Subclasses store them."""

    def __init__(self, settings: UploadSettings) -> None:
        self.settings = settings

    @property
    def bytes_per_day(self) -> int:
        return int(self.settings.mb_per_day * MB)

    # -- storage, per subclass ----------------------------------------------------

    async def _now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    async def _apply(self, key: str, counters: list[Counter]) -> tuple[int, int]:
        raise NotImplementedError

    async def _read(self, key: str) -> dict[str, int]:
        raise NotImplementedError

    # -- the API ---------------------------------------------------------------------

    async def _key(self, tenant_id: str) -> tuple[str, dt.datetime]:
        now = await self._now()
        return f"quota:{{{tenant_id}}}:{_day(now)}", now

    async def take_files(self, tenant_id: str, files: int, nbytes: int) -> None:
        """Count an upload's files and bytes, or raise :class:`OverQuota` and count nothing."""
        key, now = await self._key(tenant_id)
        counters = [
            (FILES, files, self.settings.files_per_day),
            (BYTES, nbytes, self.bytes_per_day),
        ]
        refused, used = await self._apply(key, counters)
        if not refused:
            return
        wait, _ = _until_midnight(now)
        if counters[refused - 1][0] == FILES:
            detail = (
                f"This key may upload {self.settings.files_per_day} files a day; {used} "
                f"are used today and this upload has {files}."
            )
            limit, requested = self.settings.files_per_day, files
        else:
            detail = (
                f"This key may upload {_mb(self.bytes_per_day)} a day; {_mb(used)} are "
                f"used today and this upload has {_mb(nbytes)}."
            )
            limit, requested = self.bytes_per_day, nbytes
        raise OverQuota(
            f"{detail} The limit resets at midnight UTC.",
            limit=counters[refused - 1][0],
            limit_per_day=limit,
            used=used,
            requested=requested,
            retry_after=wait,
        )

    async def give_back_files(self, tenant_id: str, files: int, nbytes: int) -> None:
        """Return files that were not stored after all (duplicates of stored ones)."""
        if files or nbytes:
            key, _ = await self._key(tenant_id)
            await self._apply(key, [(FILES, -files, 0), (BYTES, -nbytes, 0)])

    async def take_chunks(self, tenant_id: str, chunks: int) -> None:
        """Count a document's chunks, or raise :class:`OverQuota` and count nothing."""
        key, now = await self._key(tenant_id)
        limit = self.settings.chunks_per_day
        refused, used = await self._apply(key, [(CHUNKS, chunks, limit)])
        if refused:
            wait, _ = _until_midnight(now)
            raise OverQuota(
                f"This document has {chunks:,} chunks; this key may index {limit:,} a day "
                f"and {used:,} are used today. The limit resets at midnight UTC.",
                limit=CHUNKS,
                limit_per_day=limit,
                used=used,
                requested=chunks,
                retry_after=wait,
            )

    async def give_back_chunks(self, tenant_id: str, chunks: int) -> None:
        """Return chunks that were counted but not stored (embedding failed)."""
        if chunks:
            key, _ = await self._key(tenant_id)
            await self._apply(key, [(CHUNKS, -chunks, 0)])

    async def usage(self, tenant_id: str) -> Usage:
        key, now = await self._key(tenant_id)
        counts = await self._read(key)
        _, midnight = _until_midnight(now)
        return Usage(
            files=counts.get(FILES, 0),
            bytes=counts.get(BYTES, 0),
            chunks=counts.get(CHUNKS, 0),
            files_per_day=self.settings.files_per_day,
            bytes_per_day=self.bytes_per_day,
            chunks_per_day=self.settings.chunks_per_day,
            resets_at=midnight.isoformat(),
        )


class RedisUploadQuota(UploadQuota):
    def __init__(self, client: Redis, settings: UploadSettings) -> None:
        super().__init__(settings)
        self._redis = client
        self._take = client.register_script(_TAKE)

    async def _now(self) -> dt.datetime:
        """Redis server time, so every worker agrees on when the day ends."""
        seconds, micros = await self._redis.time()
        return dt.datetime.fromtimestamp(float(seconds) + float(micros) / 1e6, dt.UTC)

    async def _apply(self, key: str, counters: list[Counter]) -> tuple[int, int]:
        args: list[Any] = [KEEP_S]
        for field, amount, limit in counters:
            args += [field, amount, limit]
        refused, used = await self._take(keys=[key], args=args)
        return int(refused), int(used or 0)

    async def _read(self, key: str) -> dict[str, int]:
        raw = await self._redis.hgetall(key)
        return {_text(k): int(v) for k, v in raw.items()}


class MemoryUploadQuota(UploadQuota):
    """One process's counters. Atomic because nothing in ``_apply`` awaits."""

    def __init__(self, settings: UploadSettings) -> None:
        super().__init__(settings)
        self._counts: dict[str, dict[str, int]] = {}

    async def _apply(self, key: str, counters: list[Counter]) -> tuple[int, int]:
        day = key.rsplit(":", 1)[-1]
        for old in [k for k in self._counts if k.rsplit(":", 1)[-1] < day]:
            del self._counts[old]
        counts = self._counts.setdefault(key, {})
        for i, (field, amount, limit) in enumerate(counters, start=1):
            used = counts.get(field, 0)
            if amount > 0 and used + amount > limit:
                return i, used
        for field, amount, _ in counters:
            counts[field] = max(0, counts.get(field, 0) + amount)
        return 0, 0

    async def _read(self, key: str) -> dict[str, int]:
        return dict(self._counts.get(key, {}))


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value
