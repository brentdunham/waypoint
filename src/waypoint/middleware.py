"""Per-request plumbing: request id, access log, body size limit, last-resort 500.

A plain ASGI middleware rather than Starlette's ``BaseHTTPMiddleware``, which
runs the endpoint in a separate task: that breaks the log ContextVar bound
here and interferes with streaming responses, and this service streams.
"""

import json
import logging
import time
import uuid
from typing import Any

from opentelemetry import trace
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from waypoint.errors import CONTENT_TYPE, PayloadTooLarge, problem_body
from waypoint.log_config import bind, unbind

log = logging.getLogger("waypoint.access")

PROBES = frozenset({"/api/v1/healthz", "/api/v1/readyz"})
"""Hit every 30 s by Docker's healthcheck and by orchestrators. A successful
probe is logged at DEBUG, so the access log shows traffic; a failing one is
still logged at INFO, since that is the line you go looking for."""


def _name_span_by_route(scope: Scope) -> None:
    """``POST /api/v1/threads/{thread_id}/runs``: the route, known once routed.

    The tracing middleware names its span before routing, so it starts as the
    method alone; ids in span names would make each request its own
    operation in a trace backend.
    """
    span = trace.get_current_span()
    if scope.get("route") is None or not span.is_recording():
        return  # no route matched (a 404): the method alone is the name
    # The route's own path is relative to its router, so rebuild the full
    # template from the URL by putting each path parameter's name back.
    params = {str(v): k for k, v in (scope.get("path_params") or {}).items()}
    segments = str(scope.get("path", "")).split("/")
    path = "/".join(f"{{{params[s]}}}" if s in params else s for s in segments)
    span.update_name(f"{scope.get('method', 'HTTP')} {path}")
    span.set_attribute("http.route", path)


class RequestContext:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get("x-request-id") or str(uuid.uuid4())
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = time.perf_counter()
        # Under the tracing middleware, when it is on: log lines join traces.
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            token = bind(request_id=request_id, trace_id=format(span.trace_id, "032x"))
        else:
            token = bind(request_id=request_id)
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                _name_span_by_route(scope)
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
                self._log(scope, status, started)
            await send(message)

        try:
            declared = headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_body_bytes:
                await self._problem(send_with_id, request_id, PayloadTooLarge(self._too_big()))
                return
            await self.app(scope, self._limited(receive), send_with_id)
        except Exception as exc:
            if response_started:
                raise  # mid-stream: nothing can be sent now; let the server log it
            log.exception("unhandled exception", extra={"error_type": type(exc).__name__})
            await self._problem(send_with_id, request_id, None)
        finally:
            unbind(token)

    def _too_big(self) -> str:
        return f"Request body exceeds {self.max_body_bytes} bytes."

    def _limited(self, receive: Receive) -> Receive:
        """Counts a body sent without Content-Length (chunked) as it arrives.

        Raises Starlette's ``HTTPException`` because FastAPI re-raises that one
        from body parsing; anything else it turns into a 400.
        """
        seen = 0

        async def wrapped() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body_bytes:
                    raise HTTPException(PayloadTooLarge.status, self._too_big())
            return message

        return wrapped

    @staticmethod
    async def _problem(send: Send, request_id: str, exc: PayloadTooLarge | None) -> None:
        if exc is None:
            status, title, detail, slug = (
                500,
                "Internal Server Error",
                "An unexpected error occurred.",
                "error",
            )
        else:
            status, title, detail, slug = exc.status, exc.title, exc.detail, exc.slug
        body = json.dumps(problem_body(status, title, detail, slug, request_id=request_id))
        payload = body.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", CONTENT_TYPE.encode()),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    @staticmethod
    def _log(scope: Scope, status: int, started: float) -> None:
        principal = scope.get("state", {}).get("principal")
        method, path = scope.get("method", "-"), scope.get("path", "-")
        quiet = path in PROBES and status < 400  # noqa: PLR2004
        log.log(
            logging.DEBUG if quiet else logging.INFO,
            "%s %s -> %s",
            method,
            path,
            status,
            extra={
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "tenant_id": getattr(principal, "tenant_id", None),
            },
        )
