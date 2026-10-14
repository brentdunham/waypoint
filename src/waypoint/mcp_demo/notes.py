"""The demo MCP server: a notes store, kept apart per tenant (decision 5).

Four tools, two of each kind, so Workspace shows both paths of the approval
policy (T4) against one trusted server:

* ``search`` and ``list`` declare ``readOnlyHint`` and run at once;
* ``add`` and ``delete`` do not, so Workspace pauses for a person first.

The tenant comes from each call's ``_meta`` (``io.waypoint/tenant``), which
Waypoint sets from the key that started the run; the model cannot choose it.
A call without one is refused. The server knows nothing else about Waypoint.

The evals run it in process (``evals/driver.py``) with :class:`MemoryNotes`.
Running it as its own service over HTTP, with a shared token and a store
that survives a restart, is phase 5.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

TENANT_META = "io.waypoint/tenant"
LIST_LIMIT = 20
MIN_WORD = 3
"""Shorter words (a, of, to) would match nearly every note."""


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
    next_id: int = 1

    def add(self, tenant: str, text: str) -> Note:
        note = Note(self.next_id, text, dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
        self.next_id += 1
        self.notes.setdefault(tenant, []).append(note)
        return note

    def delete(self, tenant: str, note_id: int) -> bool:
        mine = self.notes.get(tenant, [])
        kept = [n for n in mine if n.note_id != note_id]
        self.notes[tenant] = kept
        return len(kept) < len(mine)

    def all(self, tenant: str) -> list[Note]:
        return list(self.notes.get(tenant, []))


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
