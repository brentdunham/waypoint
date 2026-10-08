"""ASGI entrypoint -- this is the one to serve.

uvicorn waypoint.asgi:app --reload
gunicorn waypoint.asgi:app -k uvicorn.workers.UvicornWorker
"""

from waypoint import create_app

app = create_app()
asgi_app = app
"""The name the Dockerfile and docs used before FastAPI; kept so both work."""
