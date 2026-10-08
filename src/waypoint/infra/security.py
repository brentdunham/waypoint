"""API-key authentication and the principal it resolves to.

A key looks like ``ak_<key_id>.<secret>``. Only the SHA-256 of the whole string
is stored, so the database is useless to an attacker who reads it. The key id
travels in cleartext purely so lookups are an index hit rather than a scan.
"""

import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from waypoint.errors import Forbidden, Unauthorized
from waypoint.infra.db import Database

log = logging.getLogger(__name__)

PREFIX = "ak_"
CACHE_TTL_S = 30.0


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str
    user_id: str
    key_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require(self, scope: str) -> None:
        if scope not in self.scopes and "*" not in self.scopes:
            raise Forbidden(f"Key is missing the {scope!r} scope.")


ANONYMOUS = Principal(tenant_id="public", user_id="anonymous", key_id="-", scopes=frozenset("*"))


def generate_key() -> tuple[str, str, str]:
    """Return ``(raw_key, key_id, key_hash)``. The raw key is never stored."""
    key_id = secrets.token_hex(6)
    raw = f"{PREFIX}{key_id}.{secrets.token_urlsafe(32)}"
    return raw, key_id, hash_key(raw)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_key_id(raw: str) -> str | None:
    if not raw.startswith(PREFIX) or "." not in raw:
        return None
    return raw[len(PREFIX) :].split(".", 1)[0]


class KeyStore:
    """Looks keys up in Postgres, with a short in-process positive cache.

    The cache exists because every request pays this lookup; 30 seconds is
    short enough that a revocation takes effect while you are still reading
    the incident channel.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, tuple[float, Principal]] = {}

    async def create(
        self, *, tenant_id: str, user_id: str, scopes: list[str], label: str | None = None
    ) -> str:
        raw, key_id, key_hash = generate_key()
        await self._db.execute(
            """insert into api_keys (key_id, key_hash, tenant_id, user_id, scopes, label)
               values (%s, %s, %s, %s, %s, %s)""",
            (key_id, key_hash, tenant_id, user_id, scopes, label),
        )
        return raw

    async def upsert_raw(
        self, raw: str, *, tenant_id: str, user_id: str, scopes: list[str], label: str
    ) -> None:
        """Register a key whose plaintext you already chose (bootstrap only)."""
        key_id = parse_key_id(raw) or hashlib.sha256(raw.encode()).hexdigest()[:12]
        await self._db.execute(
            """insert into api_keys (key_id, key_hash, tenant_id, user_id, scopes, label)
               values (%s, %s, %s, %s, %s, %s)
               on conflict (key_hash) do update set revoked_at = null""",
            (key_id, hash_key(raw), tenant_id, user_id, scopes, label),
        )

    async def revoke(self, key_id: str) -> None:
        await self._db.execute(
            "update api_keys set revoked_at = now() where key_id = %s", (key_id,)
        )
        self._cache.clear()

    async def resolve(self, raw: str) -> Principal | None:
        digest = hash_key(raw)
        now = time.monotonic()
        if hit := self._cache.get(digest):
            expires, principal = hit
            if expires > now:
                return principal
            del self._cache[digest]

        row = await self._db.fetch_one(
            """select key_id, key_hash, tenant_id, user_id, scopes
               from api_keys where key_hash = %s and revoked_at is null""",
            (digest,),
        )
        if row is None:
            return None
        # Constant-time compare even though the lookup was by hash: cheap, and
        # keeps this correct if the query ever becomes a prefix match.
        if not hmac.compare_digest(row["key_hash"], digest):  # pragma: no cover
            return None

        principal = Principal(
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            key_id=row["key_id"],
            scopes=frozenset(row["scopes"] or ()),
        )
        self._cache[digest] = (now + CACHE_TTL_S, principal)
        return principal


_bearer_scheme = HTTPBearer(auto_error=False, description="An API key: `ak_<key_id>.<secret>`.")
_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


async def authenticate(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(_bearer_scheme)],
    header_key: Annotated[str | None, Security(_header_scheme)],
) -> Principal:
    """FastAPI dependency: resolve the caller. Cached on the request.

    The two ``Security`` parameters are what put the Authorize button on the
    OpenAPI page; neither raises on its own, so the 401 below stays ours.
    """
    if (existing := getattr(request.state, "principal", None)) is not None:
        return existing  # type: ignore[no-any-return]

    runtime = request.app.state.runtime
    if not runtime.settings.auth_enabled:
        request.state.principal = ANONYMOUS
        return ANONYMOUS

    raw = (bearer.credentials.strip() if bearer else None) or header_key
    if not raw:
        raise Unauthorized("Provide an API key via 'Authorization: Bearer ak_...'.")

    store: KeyStore | None = runtime.keys
    principal = await store.resolve(raw) if store is not None else None
    if principal is None:
        raise Unauthorized("Unknown or revoked API key.")

    request.state.principal = principal
    return principal


def requires(*scopes: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: authenticate, then assert every named scope.

    principal: Annotated[Principal, Depends(requires(SCOPE_RUN))]
    """

    async def dependency(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        for scope in scopes:
            principal.require(scope)
        return principal

    return dependency
