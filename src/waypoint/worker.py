"""``python -m waypoint.worker``: take runs off the queue and drive them.

docs/design/queue.md. The API accepts runs and puts them on the Redis queue;
this process, one per container, drives up to ``QUEUE__CONCURRENCY`` of them
at once on one event loop. It has no HTTP server. It reports itself alive in
Redis (``/readyz`` on the API counts workers) and by touching a file the
container healthcheck reads.

SIGTERM or Ctrl-C stops it: it takes no new runs, gives those in flight
``QUEUE__DRAIN_S`` to end, then stops the rest.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time

from waypoint.agents.models import check_model_config
from waypoint.agents.registry import roles_by_agent
from waypoint.agents.runtime import start_runtime, stop_runtime
from waypoint.infra.runs import ALIVE_FILE
from waypoint.log_config import configure_logging
from waypoint.loops import selector_loop
from waypoint.settings import Settings, get_settings
from waypoint.tracing import setup_tracing
from waypoint.usage import check_prices

log = logging.getLogger("waypoint.worker")


async def serve(settings: Settings) -> None:
    """Run a worker until SIGTERM or SIGINT."""
    tracing = setup_tracing(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Not on Windows: there, Ctrl-C cancels this coroutine instead, and the
        # finally below still drains.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    runtime = await start_runtime(settings, tracing=tracing, role="worker")
    try:
        await stop.wait()
        log.info("stopping: draining runs in flight")
    finally:
        await stop_runtime(runtime)
        tracing.shutdown()


def check_alive(max_age_s: float) -> int:
    """The container healthcheck: 0 if the heartbeat file is fresh."""
    try:
        age = time.time() - os.path.getmtime(ALIVE_FILE)
    except OSError:
        return 1
    return 0 if age < max_age_s else 1


def main() -> None:
    settings = get_settings()
    if sys.argv[1:2] == ["--check"]:
        sys.exit(check_alive(settings.queue.heartbeat_s * 3))
    configure_logging(level=settings.log_level, json_output=settings.json_logs)
    check_model_config(settings, roles_by_agent())
    if unpriced := check_prices(settings):  # raises instead when budgets are on
        log.warning("no price for %s: their runs report cost as unknown", ", ".join(unpriced))
    # psycopg cannot run on Windows' default loop; see waypoint.loops.
    factory = selector_loop if sys.platform == "win32" else None
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(settings), loop_factory=factory)


if __name__ == "__main__":
    main()
