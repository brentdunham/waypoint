"""One long-lived event loop, shared by the whole worker process.

Flask is WSGI. Even with ``flask[async]`` an ``async def`` view is executed by
spinning up a *fresh* event loop per request, which quietly breaks anything
that pins itself to a loop: psycopg pools, LangGraph async checkpointers,
Redis connections, background tasks.

So: one daemon thread owns one loop for the lifetime of the process, every
loop-bound resource is created on it, and views hop onto it.

    from waypoint.aio import loop

    result = loop.run_sync(agent.ainvoke(state))       # from a sync view
    for chunk in loop.iter_sync(agent.astream(state)): # streaming responses
        ...

The thread and loop lifecycle is anyio's :class:`~anyio.from_thread.BlockingPortal`
rather than hand-rolled -- it is the same idea, already a transitive dependency
here (httpx pulls it in), and someone else maintains its edge cases. What is
*not* in anyio, and is the reason this module still exists, is
:meth:`BackgroundLoop.iter_sync`: bridging an async iterator into a sync one,
with prefetch and with cancellation of the producer when the consumer walks
away. That is what makes SSE work.
"""

import asyncio
import concurrent.futures as cf
import contextlib
import threading
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import AbstractContextManager
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal

_SENTINEL = object()

PREFETCH = 8
"""How far :meth:`BackgroundLoop.iter_sync` lets the producer run ahead.

Zero would make every item a separate thread round trip. Eight roughly halves
the per-item cost on a token stream without letting a cancelled run produce
much work nobody reads.
"""


class BackgroundLoop:
    """Owns an asyncio loop running in a dedicated daemon thread."""

    def __init__(self, name: str = "aio") -> None:
        self._name = name
        self._cm: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> BlockingPortal:
        with self._lock:
            if self._portal is None:
                self._cm = start_blocking_portal("asyncio", name=self._name)
                self._portal = self._cm.__enter__()
            return self._portal

    def stop(self) -> None:
        """Cancel outstanding work, then shut the loop and its thread down.

        ``cancel_remaining=True`` matters: the portal's own context manager
        exits by *waiting* for outstanding tasks, and a long-running graph would
        hold shutdown open indefinitely.
        """
        with self._lock:
            cm, portal = self._cm, self._portal
            self._cm = self._portal = None
        if portal is None:
            return
        with contextlib.suppress(Exception):
            portal.call(portal.stop, True)
        if cm is not None:
            # Joins the portal thread. Its own stop() call is now a no-op.
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)

    @property
    def portal(self) -> BlockingPortal:
        return self._portal or self.start()

    @property
    def running(self) -> bool:
        return self._portal is not None

    # -- submission ---------------------------------------------------------

    def submit[T](self, coro: Coroutine[Any, Any, T]) -> cf.Future[T]:
        """Schedule *coro* on the background loop from any thread.

        Cancelling the returned future cancels the task.
        """
        return self.portal.start_task_soon(lambda: coro)

    def run_sync[T](self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Block the calling (worker) thread until *coro* finishes."""
        return self.submit(coro).result(timeout)

    def iter_sync[T](self, agen: AsyncIterator[T], timeout: float | None = None) -> Iterator[T]:
        """Drain an async iterator from sync code -- e.g. a streaming response.

        This is the bridge that makes SSE work: a Flask streaming response is a
        *sync* generator, and re-entering the async world per item would
        otherwise mean a new event loop per item.

        The producer runs ahead into a bounded queue, so a token stream costs
        one thread round trip per ``PREFETCH`` items rather than per item. When
        the consumer stops early -- a client hanging up mid-stream -- the
        producer is cancelled rather than left generating output nobody reads.
        """
        queue: asyncio.Queue[tuple[BaseException | None, Any]] = asyncio.Queue(maxsize=PREFETCH)

        async def _pump() -> None:
            try:
                async for item in agen:
                    await queue.put((None, item))
            except asyncio.CancelledError:
                await queue.put((None, _SENTINEL))
                raise
            except BaseException as exc:
                await queue.put((exc, None))
            else:
                await queue.put((None, _SENTINEL))

        async def _drain() -> list[tuple[BaseException | None, Any]]:
            """Await one item, then take whatever else is already buffered.

            One thread round trip per *batch* rather than per item. A slow
            producer still yields one item per trip, so this costs nothing
            when tokens trickle and saves a lot when they arrive in bursts.
            """
            batch = [await queue.get()]
            while True:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    return batch

        task = self.submit(_pump())
        try:
            while True:
                for exc, item in self.submit(_drain()).result(timeout):
                    if exc is not None:
                        raise exc
                    if item is _SENTINEL:
                        return
                    yield item
        finally:
            task.cancel()


loop = BackgroundLoop()
