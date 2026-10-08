"""The shared event loop. Small surface, load-bearing behaviour."""

import asyncio

import pytest

from waypoint.aio import BackgroundLoop


@pytest.fixture
def bg():
    loop = BackgroundLoop(name="test-aio")
    loop.start()
    yield loop
    loop.stop()


def test_run_sync_returns_the_value(bg: BackgroundLoop) -> None:
    async def work() -> int:
        await asyncio.sleep(0)
        return 41 + 1

    assert bg.run_sync(work()) == 42


def test_every_submission_lands_on_the_same_loop(bg: BackgroundLoop) -> None:
    async def which() -> int:
        return id(asyncio.get_running_loop())

    assert bg.run_sync(which()) == bg.run_sync(which())


def test_exceptions_propagate_to_the_caller(bg: BackgroundLoop) -> None:
    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        bg.run_sync(boom())


def test_iter_sync_drains_an_async_generator(bg: BackgroundLoop) -> None:
    async def gen():
        for i in range(5):
            yield i

    assert list(bg.iter_sync(gen())) == [0, 1, 2, 3, 4]


def test_iter_sync_reraises_producer_errors(bg: BackgroundLoop) -> None:
    async def gen():
        yield 1
        raise RuntimeError("producer died")

    with pytest.raises(RuntimeError, match="producer died"):
        list(bg.iter_sync(gen()))


def test_abandoning_the_consumer_cancels_the_producer(bg: BackgroundLoop) -> None:
    """A client hanging up mid-SSE must stop the work behind it."""
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def gen():
        try:
            for i in range(1_000_000):
                yield i
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    iterator = bg.iter_sync(gen())
    assert next(iterator) == 0
    iterator.close()

    bg.run_sync(asyncio.sleep(0.1))
    assert cancelled == [True]
    _ = started


def test_a_stopped_loop_restarts_on_demand(bg: BackgroundLoop) -> None:
    bg.stop()
    assert not bg.running
    assert bg.run_sync(_answer()) == 7
    assert bg.running


async def _answer() -> int:
    return 7
