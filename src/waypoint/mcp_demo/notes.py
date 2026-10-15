"""The demo MCP server: a notes store, kept apart per tenant (decision 5).

Four tools, two of each kind, so Workspace shows both paths of the approval
policy (T4) against one trusted server:

* ``search`` and ``list`` declare ``readOnlyHint`` and run at once;
* ``add`` and ``delete`` do not, so Workspace pauses for a person first.

The tenant comes from each call's ``_meta`` (``io.waypoint/tenant``), which
Waypoint sets from the key that started the run; the model cannot choose it.
A call without one is refused. The server knows nothing else about Waypoint.

The evals run it in process (``evals/driver.py``) with :class:`MemoryNotes`.
As a service (``python -m waypoint.mcp_demo.notes``, the ``notes`` compose
service) it speaks Streamable HTTP at ``/mcp``, keeps notes in SQLite
(:class:`SqliteNotes`), and refuses any request without its shared token
(``NOTES_TOKEN``, sent by Waypoint from ``MCP__SERVERS[].token``). It is on the
compose network only; nothing publishes it.

Note numbers are per tenant, so one tenant's numbers say nothing about
another's notes.
"""

import contextlib
import datetime as dt
import hmac
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

TENANT_META = "io.waypoint/tenant"
LIST_LIMIT = 20
MIN_WORD = 3
"""Shorter words (a, of, to) would match nearly every note."""
MIN_TOKEN = 16


@dataclass(frozen=True, slots=True)
class Note:
    note_id: int
    text: str
    created_at: str


class NoteStore(Protocol):
    def add(self, tenant: str, text: str) -> Note: ...
    def delete(self, tenant: str, note_id: int) -> bool: ...
    def all(self, tenant: str) -> list[Note]: ...


@dataclass(slots=True)
class MemoryNotes:
    """One process's notes. Enough for tests and evals."""

    notes: dict[str, list[Note]] = field(default_factory=dict)
    next_ids: dict[str, int] = field(default_factory=dict)

    def add(self, tenant: str, text: str) -> Note:
        number = self.next_ids.get(tenant, 1)
        self.next_ids[tenant] = number + 1
        note = Note(number, text, _now())
        self.notes.setdefault(tenant, []).append(note)
        return note

    def delete(self, tenant: str, note_id: int) -> bool:
        mine = self.notes.get(tenant, [])
        kept = [n for n in mine if n.note_id != note_id]
        self.notes[tenant] = kept
        return len(kept) < len(mine)

    def all(self, tenant: str) -> list[Note]:
        return list(self.notes.get(tenant, []))


class SqliteNotes:
    """Notes in one SQLite file, so they survive a restart. A connection per call:
    FastMCP runs these synchronous tools on worker threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._db() as db:
            db.execute("pragma journal_mode=wal")
            db.execute(
                "create table if not exists notes ("
                " tenant text not null, number integer not null, text text not null,"
                " created_at text not null, primary key (tenant, number))"
            )

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:  # one transaction: commit, or roll back on error
                yield db
        finally:
            db.close()

    def add(self, tenant: str, text: str) -> Note:
        with self._db() as db:
            db.execute("begin immediate")  # the next number is read and written together
            (last,) = db.execute(
                "select coalesce(max(number), 0) from notes where tenant = ?", (tenant,)
            ).fetchone()
            note = Note(int(last) + 1, text, _now())
            db.execute(
                "insert into notes values (?, ?, ?, ?)",
                (tenant, note.note_id, note.text, note.created_at),
            )
        return note

    def delete(self, tenant: str, note_id: int) -> bool:
        with self._db() as db:
            cursor = db.execute(
                "delete from notes where tenant = ? and number = ?", (tenant, note_id)
            )
            return cursor.rowcount > 0

    def all(self, tenant: str) -> list[Note]:
        with self._db() as db:
            rows = db.execute(
                "select number, text, created_at from notes where tenant = ? order by number",
                (tenant,),
            ).fetchall()
        return [Note(int(n), str(t), str(c)) for n, t, c in rows]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _tenant(ctx: Context) -> str:
    meta = (ctx.request_context.meta if ctx.request_context else None) or {}
    tenant = meta.get(TENANT_META)
    if not isinstance(tenant, str) or not tenant:
        raise ToolError("This server needs the caller's tenant in _meta; none was sent.")
    return tenant


def _show(notes: list[Note]) -> str:
    if not notes:
        return "No notes."
    return "\n".join(f"#{n.note_id} ({n.created_at[:10]}): {n.text}" for n in notes)


def build(store: NoteStore) -> FastMCP:
    server = FastMCP("notes")
    read_only = ToolAnnotations(read_only_hint=True)

    @server.tool(annotations=read_only)
    def search(query: str, ctx: Context) -> str:
        """Search the user's notes for any of the words in the query."""
        words = [w for w in query.lower().split() if len(w) >= MIN_WORD] or [query.lower()]
        found = [n for n in store.all(_tenant(ctx)) if any(w in n.text.lower() for w in words)]
        return _show(found)

    @server.tool(name="list", annotations=read_only)
    def list_notes(ctx: Context) -> str:
        """List the user's most recent notes, newest first."""
        return _show(list(reversed(store.all(_tenant(ctx))))[:LIST_LIMIT])

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    def add(text: str, ctx: Context) -> str:
        """Save a new note for the user."""
        note = store.add(_tenant(ctx), text.strip())
        return f"Saved note #{note.note_id}."

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True))
    def delete(note_id: int, ctx: Context) -> str:
        """Delete one of the user's notes by its number."""
        if not store.delete(_tenant(ctx), note_id):
            raise ToolError(f"No note #{note_id}.")
        return f"Deleted note #{note_id}."

    return server


# -- as a service --------------------------------------------------------------------


class RequireToken:
    """ASGI middleware: every request but ``/healthz`` carries the shared token.

    The server is on the compose network only, so this is the second guard,
    not the first: it keeps another container on that network from calling
    tools with a tenant id of its choosing (open decision 4).
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] != "/healthz":
            sent = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(sent, self.expected):
                response = JSONResponse({"error": "missing or wrong token"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def http_app(store: NoteStore, token: str, *, allowed_hosts: list[str] | None = None) -> Any:
    """The server as an ASGI app: MCP at ``/mcp``, ``/healthz`` for compose."""
    server = build(store)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = server.http_app(path="/mcp", allowed_hosts=allowed_hosts)
    return RequireToken(app, token)


def main() -> None:
    """``python -m waypoint.mcp_demo.notes``, configured by the environment."""
    token = os.environ.get("NOTES_TOKEN", "")
    if len(token) < MIN_TOKEN:
        sys.exit(f"Set NOTES_TOKEN to a shared secret of at least {MIN_TOKEN} characters.")
    hosts = [h.strip() for h in os.environ.get("NOTES_ALLOWED_HOSTS", "").split(",") if h.strip()]
    app = http_app(
        SqliteNotes(os.environ.get("NOTES_DB", "notes.sqlite3")),
        token,
        allowed_hosts=hosts or None,
    )
    uvicorn.run(
        app,
        # Every interface of its container; compose publishes no port for it.
        host=os.environ.get("NOTES_HOST", "0.0.0.0"),
        port=int(os.environ.get("NOTES_PORT", "8001")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
