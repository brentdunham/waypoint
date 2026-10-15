"""The run queue: the API puts accepted runs on it, workers take them off.

docs/design/queue.md. With Redis the queue is the stream ``runs:queue`` and
its consumer group ``workers``: ``XADD`` to enqueue, ``XREADGROUP`` to claim
one run for one worker, ``XACK`` and ``XDEL`` when the run ends. A claimed run
stays in the group's pending list until then, which is what lets a later
phase hand a dead worker's runs to another one.

Also here, because they are the queue's bookkeeping rather than the run's:

* **The cancel flag** (``run:{id}:cancel``). Cancelling publishes on the bus
  for the worker driving the run, and sets this for a run no worker holds
  yet: the worker that claims it checks the flag first (Q8).
* **Worker heartbeats** (``worker:{consumer}``, with a TTL). ``/readyz``
  counts the live ones, so a queue with nobody reading it is visible.

``MemoryRunQueue`` is the same interface in one process, for development and
tests without Redis: the API process runs the worker itself.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

log = logging.getLogger(__name__)

STREAM = "runs:queue"
GROUP = "workers"
CANCEL_TTL_S = 86_400
"""A cancel flag outlives any run that could still be waiting for it."""


@dataclass(slots=True)
class RunJob:
    """Everything a worker needs to drive a run, as the API accepted it."""

    run_id: str
    thread_id: str
    agent: str
    tenant_id: str
    user_id: str
    created_at: str
    request_id: str
    scopes: list[str] = field(default_factory=list)
    payload: dict[str, Any] | None = None
    resume: Any = None
    """The ``Command(resume=...)`` value; ``None`` means a fresh input."""
    checkpoint_id: str | None = None
    durability: str = "async"
    models: dict[str, str] = field(default_factory=dict)
    trace_id: str | None = None
    """Chosen by the API; the worker's run span takes it (tracing.new_trace_id)."""
    link: dict[str, str] | None = None
    """The span of the request that started the run (O8), as hex ids."""
    slot: str | None = None
    """The tenant concurrency slot the API took; the worker gives it back."""

    def dumps(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))

    @classmethod
    def loads(cls, raw: str | bytes) -> RunJob:
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class Claimed:
    entry_id: str
    job: RunJob


class RunQueue(Protocol):
    async def ensure(self) -> None: ...
    async def enqueue(self, job: RunJob) -> str: ...
    async def read(self, consumer: str, *, block_ms: int) -> Claimed | None: ...
    async def ack(self, entry_id: str) -> None: ...
    async def request_cancel(self, run_id: str) -> None: ...
    async def cancel_requested(self, run_id: str) -> bool: ...
    async def beat(self, consumer: str, info: dict[str, Any], *, ttl_s: float) -> None: ...
    async def forget(self, consumer: str) -> None: ...
    async def workers(self) -> list[dict[str, Any]]: ...
    async def stats(self) -> dict[str, int]: ...


class RedisRunQueue:
    def __init__(self, client: Redis) -> None:
        self._redis = client

    async def ensure(self) -> None:
        """Create the stream and the group if they do not exist. Idempotent."""
        try:
            await self._redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, job: RunJob) -> str:
        raw = cast("bytes", await self._redis.xadd(STREAM, {b"j": job.dumps().encode()}))
        return raw.decode()

    async def read(self, consumer: str, *, block_ms: int) -> Claimed | None:
        try:
            result = await self._redis.xreadgroup(
                GROUP, consumer, {STREAM: ">"}, count=1, block=block_ms
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            # The stream went away (a flushed Redis): make it again and carry on.
            await self.ensure()
            return None
        for _stream, entries in cast("list[tuple[bytes, list[Any]]]", result or []):
            for entry_id, fields in entries:
                entry = entry_id.decode()
                try:
                    return Claimed(entry, RunJob.loads(fields[b"j"]))
                except Exception:
                    log.exception("dropping an unreadable queue entry %s", entry)
                    await self.ack(entry)
        return None

    async def ack(self, entry_id: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xack(STREAM, GROUP, entry_id)
            pipe.xdel(STREAM, entry_id)
            await pipe.execute()

    async def request_cancel(self, run_id: str) -> None:
        await self._redis.set(f"run:{run_id}:cancel", b"1", ex=CANCEL_TTL_S)

    async def cancel_requested(self, run_id: str) -> bool:
        return bool(await self._redis.exists(f"run:{run_id}:cancel"))

    async def beat(self, consumer: str, info: dict[str, Any], *, ttl_s: float) -> None:
        body = json.dumps({"consumer": consumer, **info}, default=str)
        await self._redis.set(f"worker:{consumer}", body, px=int(ttl_s * 1000))

    async def forget(self, consumer: str) -> None:
        await self._redis.delete(f"worker:{consumer}")

    async def workers(self) -> list[dict[str, Any]]:
        found = []
        async for key in self._redis.scan_iter(match="worker:*", count=100):
            raw = await self._redis.get(key)
            if raw:
                found.append(cast("dict[str, Any]", json.loads(raw)))
        return found

    async def stats(self) -> dict[str, int]:
        """Entries in the stream, and those claimed by a worker and not yet acked."""
        length = int(await self._redis.xlen(STREAM))
        try:
            pending = await self._redis.xpending(STREAM, GROUP)
        except ResponseError:  # no group yet: no worker has read
            pending = None
        running = int(pending["pending"]) if pending else 0
        return {"queued": max(0, length - running), "running": running}


class MemoryRunQueue:
    """One process's queue. Nothing survives the process, and nothing needs to."""

    def __init__(self) -> None:
        self._jobs: asyncio.Queue[Claimed] = asyncio.Queue()
        self._seq = 0
        self._running: set[str] = set()
        self._cancelled: set[str] = set()
        self._workers: dict[str, dict[str, Any]] = {}

    async def ensure(self) -> None:
        return None

    async def enqueue(self, job: RunJob) -> str:
        self._seq += 1
        entry = f"0-{self._seq}"
        await self._jobs.put(Claimed(entry, job))
        return entry

    async def read(self, consumer: str, *, block_ms: int) -> Claimed | None:
        try:
            claimed = await asyncio.wait_for(self._jobs.get(), block_ms / 1000)
        except TimeoutError:
            return None
        self._running.add(claimed.entry_id)
        return claimed

    async def ack(self, entry_id: str) -> None:
        self._running.discard(entry_id)

    async def request_cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)

    async def cancel_requested(self, run_id: str) -> bool:
        return run_id in self._cancelled

    async def beat(self, consumer: str, info: dict[str, Any], *, ttl_s: float) -> None:
        self._workers[consumer] = {"consumer": consumer, **info}

    async def forget(self, consumer: str) -> None:
        self._workers.pop(consumer, None)

    async def workers(self) -> list[dict[str, Any]]:
        return list(self._workers.values())

    async def stats(self) -> dict[str, int]:
        return {"queued": self._jobs.qsize(), "running": len(self._running)}
