"""Liveness and readiness.

``/healthz`` answers "is this process alive" -- it must never touch a
dependency, or a Redis blip becomes a rolling restart. ``/readyz`` answers
"should traffic come here", and does check.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from waypoint.api.deps import Runtime
from waypoint.schemas import Health, Readiness

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> Health:
    return Health(status="ok")


@router.get(
    "/readyz",
    response_model=Readiness,
    responses={503: {"model": Readiness, "description": "A dependency is down."}},
)
async def readyz(runtime: Runtime) -> Readiness | JSONResponse:
    checks = await runtime.health()
    degraded = [k for k, v in checks.items() if v == "down"]
    body = Readiness(status="degraded" if degraded else "ready", checks=checks)
    if degraded:
        return JSONResponse(body.model_dump(), status_code=503)
    return body
