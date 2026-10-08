"""``python -m waypoint`` -- development server."""

import uvicorn

from waypoint.settings import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "waypoint.asgi:asgi_app",
        host="127.0.0.1",
        port=8000,
        reload=not settings.is_production,
        reload_dirs=["src"],
        log_config=None,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
