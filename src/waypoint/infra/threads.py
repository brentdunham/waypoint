"""Thread ownership.

LangGraph's checkpointer stores thread *state*; it has no idea who a thread
belongs to. A thread id is effectively a capability -- anyone who knows it can
resume the conversation -- so ownership lives here, in our own table, and is
checked on every request that names a thread.
"""

import logging
import uuid
from typing import Any

from waypoint.errors import NotFound
from waypoint.infra.db import Database
from waypoint.infra.security import Principal

log = logging.getLogger(__name__)


class ThreadStore:
    def __init__(self, db: Database | None) -> None:
        self._db = db
        # Fallback for running with no Postgres: ownership held in process.
        self._memory: dict[str, dict[str, Any]] = {}

    async def create(
        self, principal: Principal, *, agent: str, title: str | None = None
    ) -> dict[str, Any]:
        thread_id = str(uuid.uuid4())
        row: dict[str, Any] = {
            "thread_id": thread_id,
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "agent": agent,
            "title": title,
            "metadata": {},
        }
        if self._db is None:
            self._memory[thread_id] = row
            return row
        await self._db.execute(
            """insert into threads (thread_id, tenant_id, user_id, agent, title)
               values (%s, %s, %s, %s, %s)""",
            (thread_id, principal.tenant_id, principal.user_id, agent, title),
        )
        created = await self._db.fetch_one(
            "select * from threads where thread_id = %s", (thread_id,)
        )
        return _public(created or row)

    async def get(self, thread_id: str) -> dict[str, Any] | None:
        if self._db is None:
            return self._memory.get(thread_id)
        row = await self._db.fetch_one("select * from threads where thread_id = %s", (thread_id,))
        return _public(row) if row else None

    async def require(self, thread_id: str, principal: Principal) -> dict[str, Any]:
        try:
            uuid.UUID(thread_id)
        except ValueError:
            raise NotFound(f"Unknown thread {thread_id!r}.") from None
        row = await self.get(thread_id)
        if row is None:
            raise NotFound(f"Unknown thread {thread_id!r}.")
        principal.require_owner(row["tenant_id"], row["user_id"], f"thread {thread_id!r}")
        return row

    async def list(self, principal: Principal, *, limit: int = 50) -> list[dict[str, Any]]:
        if self._db is None:
            rows = [
                r
                for r in self._memory.values()
                if r["tenant_id"] == principal.tenant_id
                and (r["user_id"] == principal.user_id or principal.tenant_admin)
            ]
            return rows[:limit]
        rows = await self._db.fetch_all(
            """select * from threads
               where tenant_id = %s and (user_id = %s or %s)
               order by updated_at desc limit %s""",
            (
                principal.tenant_id,
                principal.user_id,
                principal.tenant_admin,
                limit,
            ),
        )
        return [_public(r) for r in rows]

    async def touch(self, thread_id: str, *, title: str | None = None) -> None:
        if self._db is None:
            row = self._memory.get(thread_id)
            if row is not None and title and not row.get("title"):
                row["title"] = title
            return
        await self._db.execute(
            """update threads
                  set updated_at = now(),
                      title = coalesce(title, %s)
                where thread_id = %s""",
            (title, thread_id),
        )

    async def delete(self, thread_id: str, principal: Principal) -> None:
        await self.require(thread_id, principal)
        if self._db is None:
            self._memory.pop(thread_id, None)
            return
        await self._db.execute("delete from threads where thread_id = %s", (thread_id,))


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("thread_id",):
        if key in out:
            out[key] = str(out[key])
    for key in ("created_at", "updated_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat() if hasattr(out[key], "isoformat") else out[key]
    return out
