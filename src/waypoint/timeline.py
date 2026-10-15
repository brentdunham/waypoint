"""A run's trace, as the timeline a reviewer sees (observability design, D7).

The API reads the run's trace from Jaeger's query service and turns it into a
list of steps in the product's own terms: the graph's nodes, and under them
model calls, tool calls and document searches, each with its timing and, for
model calls, model, tokens and cost.

Two guards keep this a view of the caller's own data. The endpoint finds the
run's trace id through the run record or ``run_audit``, which are checked
against the caller's tenant first; and every span read back must name that
tenant and the run, or the trace is refused. Only whitelisted attributes are
copied, so nothing a span might carry (prompt text with ``TRACING__CONTENT``,
document ids) reaches a reviewer by accident.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0

Kind = Literal["run", "node", "model", "tool", "retrieval"]

NUMERIC = {
    "waypoint.cost_usd": "cost_usd",
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "waypoint.searches": "searches",
    "waypoint.rag.hits": "hits",
    "waypoint.rag.rerank_tokens": "rerank_tokens",
}
TEXT = {
    "waypoint.node": "node",
    "waypoint.role": "role",
    "gen_ai.request.model": "model",
    "gen_ai.tool.name": "tool",
    "waypoint.tool.server": "server",
    "waypoint.rag.mode": "mode",
    "waypoint.status": "status",
}
"""Span attribute -> step field. Everything else on a span is dropped."""


class TraceNotReadyError(Exception):
    """The backend has not stored the trace yet (spans export in batches)."""


class TraceUnavailableError(Exception):
    """The backend could not be read."""


class TraceMismatchError(Exception):
    """The trace does not belong to this run and tenant."""


@dataclass(slots=True)
class Step:
    id: str
    parent: str | None
    kind: Kind
    name: str
    start_ms: float
    duration_ms: float
    error: bool = False
    fields: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent": self.parent,
            "kind": self.kind,
            "name": self.name,
            "start_ms": round(self.start_ms, 1),
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            **(self.fields or {}),
        }


async def fetch_trace(query_url: str, trace_id: str) -> dict[str, Any]:
    """The trace as Jaeger's query API returns it (``/api/traces/<id>``)."""
    url = f"{query_url.rstrip('/')}/api/traces/{trace_id}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise TraceUnavailableError(type(exc).__name__) from exc
    if response.status_code == httpx.codes.NOT_FOUND:
        raise TraceNotReadyError
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise TraceUnavailableError(f"HTTP {response.status_code}")
    data = response.json().get("data") or []
    if not data or not data[0].get("spans"):
        raise TraceNotReadyError
    trace: dict[str, Any] = data[0]
    return trace


def _tags(span: dict[str, Any]) -> dict[str, Any]:
    return {t.get("key"): t.get("value") for t in span.get("tags") or []}


def _kind(name: str, tags: dict[str, Any]) -> Kind | None:
    if name.startswith("run ") or name == "run.attempt":
        return "run"
    if name.startswith("node "):
        return "node"
    if name == "rag.retrieve":
        return "retrieval"
    if tags.get("gen_ai.operation.name") == "chat":
        return "model"
    if tags.get("gen_ai.operation.name") == "execute_tool":
        return "tool"
    return None


def timeline(trace: dict[str, Any], *, run_id: str, tenant_id: str) -> dict[str, Any]:
    """Steps relative to the run's start, ordered by time; refuses a foreign trace."""
    spans = trace.get("spans") or []
    steps: list[Step] = []
    roots: list[dict[str, Any]] = []
    for span in spans:
        tags = _tags(span)
        name = str(span.get("operationName", ""))
        kind = _kind(name, tags)
        if kind is None:
            continue
        if tags.get("waypoint.tenant_id") != tenant_id or tags.get("waypoint.run_id") != run_id:
            raise TraceMismatchError(name)
        if kind == "run":
            roots.append(span)
        parent = next(
            (
                r.get("spanID")
                for r in span.get("references") or []
                if r.get("refType") == "CHILD_OF"
            ),
            None,
        )
        fields: dict[str, Any] = {}
        for key, field in NUMERIC.items():
            if isinstance(tags.get(key), int | float) and not isinstance(tags.get(key), bool):
                fields[field] = tags[key]
        for key, field in TEXT.items():
            if isinstance(tags.get(key), str):
                fields[field] = tags[key]
        steps.append(
            Step(
                id=str(span.get("spanID")),
                parent=str(parent) if parent else None,
                kind=kind,
                name=name,
                start_ms=float(span.get("startTime", 0)) / 1000,
                duration_ms=float(span.get("duration", 0)) / 1000,
                error=bool(tags.get("error")) or "error.type" in tags,
                fields=fields,
            )
        )
    runs = [s for s in steps if s.kind == "run"]
    terminal = [
        s
        for s in runs
        if (s.fields or {}).get("status") in {"completed", "interrupted", "failed", "cancelled"}
    ]
    if not terminal:
        # A handed-off attempt can export before its successor finishes. Keep
        # the client's poll alive until the terminal attempt and cumulative cost arrive.
        raise TraceNotReadyError
    root = min(roots, key=lambda s: float(s.get("startTime", 0)))
    start = float(root.get("startTime", 0)) / 1000
    created = _tags(root).get("waypoint.run.created_at")
    if isinstance(created, str):
        start = dt.datetime.fromisoformat(created).timestamp() * 1000
    latest = max(terminal, key=lambda s: s.start_ms + s.duration_ms)
    duration = latest.start_ms + latest.duration_ms - start
    known = {s.id for s in steps}
    for step in steps:
        step.start_ms -= start
        if step.parent not in known:
            step.parent = None
    steps.sort(key=lambda s: (s.start_ms, s.kind != "node"))
    return {
        "status": "ready",
        "run_id": run_id,
        "trace_id": trace.get("traceID"),
        "duration_ms": round(duration, 1),
        "cost_usd": (latest.fields or {}).get("cost_usd"),
        "steps": [s.public() for s in steps if s.kind != "run"],
    }
