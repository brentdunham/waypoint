"""API key administration. Requires the ``keys:admin`` scope."""

from typing import Any

from flask import Blueprint, jsonify, request

from waypoint.agents import get_runtime
from waypoint.aio import loop
from waypoint.errors import ServiceUnavailable
from waypoint.infra.security import requires
from waypoint.schemas import SCOPE_ADMIN, CreateKey

bp = Blueprint("keys", __name__, url_prefix="/keys")


@bp.post("")
@requires(SCOPE_ADMIN)
def create() -> Any:
    payload = CreateKey.model_validate(request.get_json(silent=True) or {})
    runtime = get_runtime()
    if runtime.keys is None:
        raise ServiceUnavailable("API keys need Postgres; DATABASE_URL is unset.")
    raw = loop.run_sync(
        runtime.keys.create(
            tenant_id=payload.tenant_id,
            user_id=payload.user_id,
            scopes=payload.scopes,
            label=payload.label,
        ),
        timeout=10,
    )
    # The only time the plaintext key ever exists outside the caller's hands.
    return jsonify(api_key=raw, tenant_id=payload.tenant_id, scopes=payload.scopes), 201


@bp.delete("/<key_id>")
@requires(SCOPE_ADMIN)
def revoke(key_id: str) -> Any:
    runtime = get_runtime()
    if runtime.keys is None:
        raise ServiceUnavailable("API keys need Postgres; DATABASE_URL is unset.")
    loop.run_sync(runtime.keys.revoke(key_id), timeout=10)
    return "", 204
