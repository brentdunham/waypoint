"""RFC 9457 problem-details error handling.

Every failure that leaves this service is ``application/problem+json`` with a
stable ``type`` slug, so clients can branch on the slug instead of parsing prose.
"""

import logging
import math
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException

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


class PayloadTooLarge(AppError):
    status, title, slug = 413, "Content Too Large", "payload-too-large"


class RateLimited(AppError):
    status, title, slug = 429, "Too Many Requests", "rate-limited"


class OverBudget(RateLimited):
    """A daily spending budget is used up (observability design, phase 2)."""

    slug = "over-budget"


class OverQuota(RateLimited):
    """A daily upload limit is used up (``UPLOADS__*``, docs/design/tools.md T13)."""

    slug = "over-quota"


class UpstreamError(AppError):
    status, title, slug = 502, "Upstream Error", "upstream-error"


class ServiceUnavailable(AppError):
    status, title, slug = 503, "Service Unavailable", "unavailable"


def http_status(exc: BaseException) -> int | None:
    """The HTTP status an upstream provider's error carries, whatever its SDK calls it.

    OpenAI's and Anthropic's SDKs say ``status_code``; Voyage's says
    ``http_status``. Reading only the first let a rejected Voyage key through
    the boot probe as a mere network warning (found 2026-10-10).
    """
    for attr in ("status_code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def problem_body(
    status: int,
    title: str,
    detail: str,
    slug: str = "error",
    *,
    request_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": f"{BASE_TYPE}{slug}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if request_id:
        body["request_id"] = request_id
    return body | extra


def problem(
    request: Request, status: int, title: str, detail: str, slug: str = "error", **extra: Any
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    body = problem_body(status, title, detail, slug, request_id=request_id, **extra)
    headers = {}
    if isinstance(retry := extra.get("retry_after"), int | float):
        # Whole seconds, rounded up (RFC 9110): retrying at the stated time must work.
        headers["Retry-After"] = str(max(1, math.ceil(retry)))
    return JSONResponse(body, status_code=status, media_type=CONTENT_TYPE, headers=headers)


def validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Pydantic errors without the echoed input, URL or context.

    Body fields are reported by name alone (``["agent"]``, not ``["body",
    "agent"]``), as they were before FastAPI; query and header errors keep
    their location prefix, since that is what tells you where to look.
    """
    out = []
    for error in errors:
        loc = list(error.get("loc", ()))
        if loc[:1] == ["body"]:
            loc = loc[1:]
        out.append({"type": error.get("type"), "loc": loc, "msg": error.get("msg")})
    return out


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status >= SERVER_ERROR:
            log.warning("%s: %s", exc.slug, exc.detail)
        return problem(request, exc.status, exc.title, exc.detail, exc.slug, **exc.extra)

    async def _invalid(request: Request, errors: Sequence[Any]) -> JSONResponse:
        return problem(
            request,
            422,
            "Unprocessable Entity",
            "Request failed validation.",
            "validation-failed",
            errors=validation_errors(errors),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return await _invalid(request, exc.errors())

    @app.exception_handler(ValidationError)
    async def _model_invalid(request: Request, exc: ValidationError) -> JSONResponse:
        return await _invalid(request, exc.errors(include_url=False, include_context=False))

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        title = HTTPStatus(exc.status_code).phrase
        slug = PayloadTooLarge.slug if exc.status_code == PayloadTooLarge.status else "http-error"
        response = problem(request, exc.status_code, title, str(exc.detail or title), slug)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    # Anything else is turned into a 500 problem by ``middleware.RequestContext``,
    # which, unlike an ``Exception`` handler here, still adds X-Request-ID.
