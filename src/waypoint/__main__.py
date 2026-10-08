"""``python -m waypoint`` -- development server."""

import sys

import uvicorn

from waypoint.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "waypoint.asgi:app",
        host="127.0.0.1",
        port=8000,
        reload=not settings.is_production,
        reload_dirs=["src"],
        log_config=None,
        timeout_graceful_shutdown=10,
        # psycopg cannot run on Windows' default loop; see waypoint.loops.
        loop="waypoint.loops:selector_loop" if sys.platform == "win32" else "auto",
    )


if __name__ == "__main__":
    main()
