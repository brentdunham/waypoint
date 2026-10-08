"""RFC 9457 problem-details error handling.

Every failure that leaves this service is ``application/problem+json`` with a
stable ``type`` slug, so clients can branch on the slug instead of parsing prose.
"""

import logging
from typing import Any

from flask import Flask, g, jsonify
from pydantic import ValidationError
from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)

CONTENT_TYPE = "application/problem+json"
BASE_TYPE = "https://errors.waypoint.local/"
SERVER_ERROR = 500


class AppError(Exception):
    """Base class for expected, client-visible failures."""

    status = 500
    title = "Internal Server Error"
    slug = "internal-error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra


class BadRequest(AppError):
    status, title, slug = 400, "Bad Request", "bad-request"


class Unauthorized(AppError):
    status, title, slug = 401, "Unauthorized", "unauthorized"


class Forbidden(AppError):
    status, title, slug = 403, "Forbidden", "forbidden"


class NotFound(AppError):
    status, title, slug = 404, "Not Found", "not-found"


class Conflict(AppError):
    status, title, slug = 409, "Conflict", "conflict"


class RateLimited(AppError):
    status, title, slug = 429, "Too Many Requests", "rate-limited"


class UpstreamError(AppError):
    status, title, slug = 502, "Upstream Error", "upstream-error"


class ServiceUnavailable(AppError):
    status, title, slug = 503, "Service Unavailable", "unavailable"


def problem(status: int, title: str, detail: str, slug: str = "error", **extra: Any) -> Any:
    body: dict[str, Any] = {
        "type": f"{BASE_TYPE}{slug}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if request_id := getattr(g, "request_id", None):
        body["request_id"] = request_id
    body |= extra
    response = jsonify(body)
    response.status_code = status
    response.content_type = CONTENT_TYPE
    return response


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def _app_error(exc: AppError) -> Any:
        if exc.status >= SERVER_ERROR:
            log.warning("%s: %s", exc.slug, exc.detail)
        return problem(exc.status, exc.title, exc.detail, exc.slug, **exc.extra)

    @app.errorhandler(ValidationError)
    def _validation_error(exc: ValidationError) -> Any:
        return problem(
            422,
            "Unprocessable Entity",
            "Request body failed validation.",
            "validation-failed",
            errors=exc.errors(include_url=False, include_context=False, include_input=False),
        )

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException) -> Any:
        return problem(exc.code or 500, exc.name, exc.description or exc.name, "http-error")

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception) -> Any:
        log.exception("unhandled exception", extra={"error_type": type(exc).__name__})
        return problem(500, "Internal Server Error", "An unexpected error occurred.")
