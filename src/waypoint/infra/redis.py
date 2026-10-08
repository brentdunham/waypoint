"""Redis client factory and health check.

Redis carries four things here: the run event bus (Streams), cross-worker run
cancellation (Pub/Sub), per-tenant rate limits and concurrency slots (Lua), and
the LangGraph node cache.
"""

import logging
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger(__name__)


def create_client(dsn: str, *, socket_timeout_s: float) -> Redis:
    """``socket_timeout_s`` must outlast the longest blocking command we send.

    The event bus parks in ``XREAD BLOCK <STREAM__BLOCK_MS>`` while a run is
    quiet. redis-py applies its socket timeout to that read too, and its
    default is 5s, the same as the default block. Whenever a run went 5s
    without an event (a research worker searching the web, say) the client
    gave up a moment before Redis would have answered, and the SSE stream died.
    Settings refuse a block that is not comfortably shorter than this timeout.
    """
    return Redis.from_url(
        dsn,
        decode_responses=False,  # stream payloads are bytes; we decode explicitly
        health_check_interval=30,
        socket_keepalive=True,
        socket_timeout=socket_timeout_s,
        retry_on_timeout=True,
    )


async def ping(client: Redis | None) -> bool:
    if client is None:
        return False
    try:
        pong: Any = await client.ping()
    except Exception:
        return False
    return bool(pong)
