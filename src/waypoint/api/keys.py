"""API key administration. Requires the ``keys:admin`` scope."""

from fastapi import APIRouter, Response

from waypoint.api.deps import Admin, Runtime
from waypoint.errors import ServiceUnavailable
from waypoint.infra.security import KeyStore
from waypoint.schemas import CreateKey, KeyCreated

router = APIRouter(prefix="/keys", tags=["keys"])


def _store(runtime: Runtime) -> KeyStore:
    if runtime.keys is None:
        raise ServiceUnavailable("API keys need Postgres; DATABASE_URL is unset.")
    return runtime.keys


@router.post("", status_code=201)
async def create(payload: CreateKey, runtime: Runtime, _: Admin) -> KeyCreated:
    raw = await _store(runtime).create(
        tenant_id=payload.tenant_id,
        user_id=payload.user_id,
        scopes=payload.scopes,
        label=payload.label,
    )
    # The only time the plaintext key ever exists outside the caller's hands.
    return KeyCreated(api_key=raw, tenant_id=payload.tenant_id, scopes=payload.scopes)


@router.delete("/{key_id}", status_code=204)
async def revoke(key_id: str, runtime: Runtime, _: Admin) -> Response:
    await _store(runtime).revoke(key_id)
    return Response(status_code=204)
