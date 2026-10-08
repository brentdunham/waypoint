"""Application factory.

Views in this service are deliberately **synchronous**. All async work is
submitted to the one shared event loop in :mod:`app.aio`. That is the opposite
of what ``flask[async]`` invites you to do, and it is deliberate: Flask runs an
``async def`` view in a brand-new event loop per request, which cannot hold a
connection pool, a Redis subscription, or a running graph.
"""

import atexit
import logging
import time
import uuid
from typing import Any

from flask import Flask, g, request

from waypoint.agents.runtime import init_runtime, shutdown_runtime
from waypoint.aio import loop
from waypoint.api import api
from waypoint.errors import register_error_handlers
from waypoint.log_config import bind, configure_logging, unbind
from waypoint.settings import Settings, get_settings

__all__ = ["create_app"]

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.json_logs)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        DEBUG=settings.debug,
        JSON_SORT_KEYS=False,
        PROPAGATE_EXCEPTIONS=False,
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    )
    app.extensions["settings"] = settings

    _register_hooks(app, settings)
    register_error_handlers(app)
    app.register_blueprint(api, url_prefix="/api/v1")

    try:
        init_runtime(app, settings)
    except BaseException:
        # A half-built runtime leaves pool workers and a running loop behind,
        # which drown the real traceback in "Task was destroyed" noise.
        loop.stop()
        raise
    atexit.register(_shutdown, app)

    log.info("app created", extra={"env": settings.env})
    return app


def _register_hooks(app: Flask, settings: Settings) -> None:
    allowed = set(settings.cors_origins)

    @app.before_request
    def _open() -> Any:
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.started = time.perf_counter()
        g.log_token = bind(request_id=g.request_id)
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        return None

    @app.after_request
    def _close(response: Any) -> Any:
        response.headers["X-Request-ID"] = getattr(g, "request_id", "-")
        origin = request.headers.get("Origin")
        if origin and (origin in allowed or "*" in allowed):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Last-Event-ID, X-Request-ID"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Expose-Headers"] = "X-Request-ID"
            response.headers["Access-Control-Max-Age"] = "600"
        principal = getattr(g, "principal", None)
        log.info(
            "%s %s -> %s",
            request.method,
            request.path,
            response.status_code,
            extra={
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - g.started) * 1000, 2),
                "tenant_id": getattr(principal, "tenant_id", None),
            },
        )
        return response

    @app.teardown_request
    def _unbind(_exc: BaseException | None) -> None:
        if token := getattr(g, "log_token", None):
            unbind(token)


def _shutdown(app: Flask) -> None:
    shutdown_runtime(app)
    loop.stop()
