"""Plain WSGI entrypoint -- ``gunicorn waypoint.wsgi:app``. Prefer ``waypoint.asgi``."""

from waypoint import create_app

app = create_app()
