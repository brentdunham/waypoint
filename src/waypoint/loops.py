"""Event loop factory for running the API natively on Windows.

psycopg's async mode cannot use Windows' default ``ProactorEventLoop``, and
uvicorn picks that one on Windows, so the Postgres pool never opens there.
Point uvicorn at this factory instead:

    uv run python -m uvicorn waypoint.asgi:app --loop waypoint.loops:selector_loop

``python -m waypoint`` does this for you on Windows. Linux, and so every
container, keeps uvicorn's default (uvloop when installed).
"""

import asyncio


def selector_loop() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()
