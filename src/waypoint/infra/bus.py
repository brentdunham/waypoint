"""The run event bus.

A run is started by whichever worker got the POST, but the SSE connection that
watches it may land on a different worker, and may reconnect after a dropped
network. So run output cannot live in process memory: it goes into a Redis
Stream, which gives ordering, replay from an arbitrary id, and a bounded
backlog for free. That is what makes ``Last-Event-ID`` resumption work.

``MemoryBus`` is the same interface for single-process development with no
Redis running. It cannot resume across workers, and says so.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from redis.asyncio import Redis

from waypoint.infra.queue import OwnershipLostError

log = logging.getLogger(__name__)

CANCEL_CHANNEL = "runs:cancel"


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    type: str
    data: dict[str, Any]

    def sse(self) -> str:
        """Render as a Server-Sent Event frame."""
        payload = json.dumps(self.data, default=str, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"


TERMINAL = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted"})


class EventBus(Protocol):
    async def append(
        self, run_id: str, type_: str, data: dict[str, Any], *, owner: str | None = None
    ) -> str: ...
    async def read(self, run_id: str, *, last_id: str, block_ms: int) -> list[Event]: ...
    async def set_meta(self, run_id: str, meta: dict[str, Any]) -> None: ...
    async def get_meta(self, run_id: str) -> dict[str, Any] | None: ...
    async def request_cancel(self, run_id: str) -> None: ...
    def cancellations(self) -> AsyncIterator[str]: ...
    async def close(self) -> None: ...
    async def finish(
        self,
        run_id: str,
        type_: str,
        data: dict[str, Any],
        meta: dict[str, Any],
        *,
        owner: str | None = None,
    ) -> None: ...


APPEND = """
if ARGV[1] ~= '' and redis.call('get', KEYS[1]) ~= ARGV[1] then return false end
local id = redis.call('xadd', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', 't', ARGV[3], 'd', ARGV[4])
redis.call('expire', KEYS[2], ARGV[5])
return id
"""

FINISH = """
if ARGV[1] ~= '' and redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('set', KEYS[3], ARGV[6], 'EX', ARGV[5])
redis.call('xadd', KEYS[2], 'MAXLEN', '~', ARGV[2], '*', 't', ARGV[3], 'd', ARGV[4])
redis.call('expire', KEYS[2], ARGV[5])
if redis.call('get', KEYS[4]) == ARGV[7] then redis.call('del', KEYS[4]) end
return 1
"""


class RedisBus:
    def __init__(self, client: Redis, *, maxlen: int, ttl_s: int) -> None:
        self._redis = client
        self._maxlen = maxlen
        self._ttl = ttl_s

    def _stream(self, run_id: str) -> str:
        return f"run:{run_id}:events"

    def _meta(self, run_id: str) -> str:
        return f"run:{run_id}:meta"

    async def append(
        self, run_id: str, type_: str, data: dict[str, Any], *, owner: str | None = None
    ) -> str:
        key = self._stream(run_id)
        raw = await self._redis.eval(
            APPEND,
            2,
            f"run:{run_id}:owner",
            key,
            owner or "",
            self._maxlen,
            type_,
            json.dumps(data, default=str),
            self._ttl,
        )
        if not raw:
            raise OwnershipLostError
        return cast("bytes", raw).decode()

    async def finish(
        self,
        run_id: str,
        type_: str,
        data: dict[str, Any],
        meta: dict[str, Any],
        *,
        owner: str | None = None,
    ) -> None:
        ok = await self._redis.eval(
            FINISH,
            4,
            f"run:{run_id}:owner",
            self._stream(run_id),
            self._meta(run_id),
            f"thread:{meta['thread_id']}:active",
            owner or "",
            self._maxlen,
            type_,
            json.dumps(data, default=str),
            self._ttl,
            json.dumps(meta, default=str),
            run_id,
        )
        if not ok:
            raise OwnershipLostError

    async def read(self, run_id: str, *, last_id: str, block_ms: int) -> list[Event]:
        result = cast(
            "list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]]",
            await self._redis.xread({self._stream(run_id): last_id}, count=200, block=block_ms),
        )
        events: list[Event] = []
        for _stream, entries in result or []:
            for entry_id, fields in entries:
                events.append(
                    Event(
                        id=entry_id.decode(),
                        type=fields[b"t"].decode(),
                        data=json.loads(fields[b"d"]),
                    )
                )
        return events

    async def set_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        key = self._meta(run_id)
        await self._redis.set(key, json.dumps(meta, default=str), ex=self._ttl)

    async def get_meta(self, run_id: str) -> dict[str, Any] | None:
        raw = cast("bytes | None", await self._redis.get(self._meta(run_id)))
        return cast("dict[str, Any]", json.loads(raw)) if raw else None

    async def request_cancel(self, run_id: str) -> None:
        await self._redis.publish(CANCEL_CHANNEL, run_id.encode())

    async def cancellations(self) -> AsyncIterator[str]:
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(CANCEL_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    yield bytes(message["data"]).decode()
        finally:
            await pubsub.unsubscribe(CANCEL_CHANNEL)
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        await self._redis.aclose()


@dataclass
class _MemStream:
    events: list[Event] = field(default_factory=list)
    waiters: list[asyncio.Event] = field(default_factory=list)
    seq: int = 0


class MemoryBus:
    """Single-process fallback. Same semantics, no durability, no fan-out."""

    def __init__(self, *, maxlen: int = 2000) -> None:
        self._streams: dict[str, _MemStream] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._cancels: asyncio.Queue[str] = asyncio.Queue()
        self._maxlen = maxlen

    def _stream(self, run_id: str) -> _MemStream:
        return self._streams.setdefault(run_id, _MemStream())

    async def append(
        self, run_id: str, type_: str, data: dict[str, Any], *, owner: str | None = None
    ) -> str:
        stream = self._stream(run_id)
        stream.seq += 1
        event = Event(id=f"{int(time.time() * 1000)}-{stream.seq}", type=type_, data=data)
        stream.events.append(event)
        del stream.events[: max(0, len(stream.events) - self._maxlen)]
        for waiter in stream.waiters:
            waiter.set()
        stream.waiters.clear()
        return event.id

    async def finish(
        self,
        run_id: str,
        type_: str,
        data: dict[str, Any],
        meta: dict[str, Any],
        *,
        owner: str | None = None,
    ) -> None:
        await self.set_meta(run_id, meta)
        await self.append(run_id, type_, data)

    async def read(self, run_id: str, *, last_id: str, block_ms: int) -> list[Event]:
        stream = self._stream(run_id)
        pending = [e for e in stream.events if _gt(e.id, last_id)]
        if pending:
            return pending
        waiter = asyncio.Event()
        stream.waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), block_ms / 1000)
        except TimeoutError:
            return []
        return [e for e in stream.events if _gt(e.id, last_id)]

    async def set_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        self._meta[run_id] = meta

    async def get_meta(self, run_id: str) -> dict[str, Any] | None:
        return self._meta.get(run_id)

    async def request_cancel(self, run_id: str) -> None:
        await self._cancels.put(run_id)

    async def cancellations(self) -> AsyncIterator[str]:
        while True:
            yield await self._cancels.get()

    async def close(self) -> None:
        return None


def _gt(a: str, b: str) -> bool:
    if b in ("0", "0-0", ""):
        return True
    at, aseq = (int(x) for x in a.split("-"))
    bt, bseq = (int(x) for x in b.split("-"))
    return (at, aseq) > (bt, bseq)
