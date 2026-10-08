"""ASGI entrypoint -- this is the one to serve.

    uvicorn waypoint.asgi:asgi_app --reload
    gunicorn waypoint.asgi:asgi_app -k uvicorn.workers.UvicornWorker

``WsgiToAsgi`` runs the WSGI app in a threadpool, so a worker blocked on a long
SSE stream does not block the process.
"""

from asgiref.wsgi import WsgiToAsgi

from waypoint import create_app

flask_app = create_app()
asgi_app = WsgiToAsgi(flask_app)  # type: ignore[no-untyped-call]
