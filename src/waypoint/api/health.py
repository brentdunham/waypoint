"""Liveness and readiness.

``/healthz`` answers "is this process alive" -- it must never touch a
dependency, or a Redis blip becomes a rolling restart. ``/readyz`` answers
"should traffic come here", and does check.
"""

from typing import Any

from flask import Blueprint, jsonify

from waypoint.agents import get_runtime
from waypoint.aio import loop

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz() -> Any:
    return jsonify(status="ok", loop="running" if loop.running else "stopped")


@bp.get("/readyz")
def readyz() -> Any:
    runtime = get_runtime()
    checks = loop.run_sync(runtime.health(), timeout=5)
    degraded = [k for k, v in checks.items() if v == "down"]
    body = {"status": "degraded" if degraded else "ready", "checks": checks}
    return jsonify(body), (503 if degraded else 200)
