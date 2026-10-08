"""Application factory.

Everything runs on the server's one event loop: request handlers, the
Postgres pool, the LangGraph checkpointer and store, Redis, and every run
task. The runtime that owns them is built in the app's lifespan, so it exists
before the first request and is torn down (runs cancelled, pools closed)
when the server stops.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from waypoint.agents.models import check_model_config
from waypoint.agents.registry import roles_by_agent
from waypoint.agents.runtime import start_runtime, stop_runtime
from waypoint.api import api
from waypoint.errors import register_error_handlers
from waypoint.log_config import configure_logging
from waypoint.middleware import RequestContext
from waypoint.settings import Settings, get_settings

__all__ = ["API_PREFIX", "create_app"]

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
MAX_BODY_BYTES = 8 * 1024 * 1024

DESCRIPTION = """Durable, resumable, human-in-the-loop LangGraph agent runs.

Authenticate with an API key: `Authorization: Bearer ak_<key_id>.<secret>`.
Start a run with `POST /threads/{thread_id}/runs` (202 plus a `run_id`), then
follow it on `GET /runs/{run_id}/events`, a Server-Sent Events stream that
resumes from `Last-Event-ID`. Errors are `application/problem+json` (RFC 9457).
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_output=settings.json_logs)
    # Here as well as in the runtime: a typo in MODELS__* should stop the
    # import, before a server binds a port, not surface in the lifespan.
    check_model_config(settings, roles_by_agent())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = await start_runtime(settings)
        try:
            yield
        finally:
            await stop_runtime(app.state.runtime)
            app.state.runtime = None

    app = FastAPI(
        title="Waypoint",
        version="0.1.0",
        description=DESCRIPTION,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.state.settings = settings
    app.state.runtime = None

    register_error_handlers(app)
    app.include_router(api, prefix=API_PREFIX)

    # Added last runs first: RequestContext wraps CORS, so preflight responses
    # carry X-Request-ID and show up in the access log too.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "X-Request-ID",
            "X-API-Key",
        ],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(RequestContext, max_body_bytes=MAX_BODY_BYTES)

    log.info("app created", extra={"env": settings.env})
    return app
