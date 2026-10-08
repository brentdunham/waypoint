"""Runs: start, follow, resume, cancel.

Starting a run returns ``202`` immediately with a ``run_id``. Output is read
separately from ``GET /runs/<id>/events``, an SSE stream that supports
``Last-Event-ID`` so a dropped connection resumes exactly where it stopped
rather than replaying or losing tokens.
"""

import logging
from collections.abc import Iterator
from typing import Any

from flask import Blueprint, Response, g, jsonify, request

from waypoint.agents import get_runtime, get_spec
from waypoint.agents.models import ModelChoiceError
from waypoint.agents.registry import models_for
from waypoint.aio import loop
from waypoint.errors import BadRequest
from waypoint.infra.bus import TERMINAL
from waypoint.infra.security import current_principal, requires
from waypoint.schemas import SCOPE_READ, SCOPE_RUN, StartRun

log = logging.getLogger(__name__)

bp = Blueprint("runs", __name__)


@bp.post("/threads/<thread_id>/runs")
@requires(SCOPE_RUN)
def start(thread_id: str) -> Any:
    payload = StartRun.model_validate(request.get_json(silent=True) or {})
    principal = current_principal()
    runtime = get_runtime()
    thread = loop.run_sync(runtime.threads.require(thread_id, principal))
    spec = get_spec(thread["agent"])

    if payload.resume is None and payload.input is None and payload.message is None:
        raise BadRequest("Provide 'input', 'message', or 'resume'.")

    graph_input: dict[str, Any] | None = None
    if payload.resume is None:
        graph_input = dict(payload.input or {})
        if payload.message is not None:
            if spec.input_key == "messages":
                graph_input.setdefault("messages", []).append(
                    {"role": "user", "content": payload.message}
                )
            else:
                graph_input.setdefault(spec.input_key, payload.message)
        _guard_size(graph_input, runtime.settings.limits.max_input_chars)

    try:
        models = models_for(runtime.settings, spec.name, payload.models)
    except ModelChoiceError as exc:
        raise BadRequest(str(exc)) from None

    if runtime.limiter is not None:
        loop.run_sync(runtime.limiter.enforce(principal.tenant_id), timeout=5)

    record = loop.run_sync(
        runtime.runs.start(
            agent=thread["agent"],
            thread_id=thread_id,
            principal=principal,
            request_id=_request_id(),
            payload=graph_input,
            resume=payload.resume,
            checkpoint_id=payload.checkpoint_id,
            durability=payload.durability,
            models=models,
        ),
        timeout=15,
    )
    loop.run_sync(runtime.threads.touch(thread_id, title=payload.message or None), timeout=10)

    if request.args.get("wait") in {"1", "true", "yes"}:
        return jsonify(_await_terminal(record.run_id)), 200

    body = record.public() | {
        "events_url": f"/api/v1/runs/{record.run_id}/events",
        "cancel_url": f"/api/v1/runs/{record.run_id}/cancel",
    }
    return jsonify(body), 202


@bp.get("/runs/<run_id>")
@requires(SCOPE_READ)
def show(run_id: str) -> Any:
    runtime = get_runtime()
    record = loop.run_sync(runtime.runs.require(run_id, current_principal()), timeout=5)
    return jsonify(record.public())


@bp.get("/runs/<run_id>/events")
@requires(SCOPE_READ)
def events(run_id: str) -> Response:
    """Server-Sent Events, resumable via ``Last-Event-ID``."""
    runtime = get_runtime()
    loop.run_sync(runtime.runs.require(run_id, current_principal()), timeout=5)
    last_id = request.headers.get("Last-Event-ID") or request.args.get("last_event_id") or "0"

    def generate() -> Iterator[str]:
        # Tell proxies not to buffer, and prime the connection so the browser
        # fires `onopen` even if the first real event is slow.
        yield ": stream open\n\n"
        try:
            for event in loop.iter_sync(runtime.runs.events(run_id, last_id=last_id)):
                yield event.sse()
        except GeneratorExit:  # client hung up; iter_sync cancels the producer
            raise
        except Exception:
            # Following the run failed, not the run: it keeps going on its own
            # task and its events keep landing in the stream. So end this
            # response without a terminal event; the client reconnects with
            # Last-Event-ID and resumes exactly where it stopped. Sending
            # run.failed here would tell the UI a healthy run had died.
            log.exception("sse stream failed; client should resume", extra={"run_id": run_id})
            yield ": stream lost, reconnect with Last-Event-ID\n\n"

    response = Response(generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


@bp.post("/runs/<run_id>/cancel")
@requires(SCOPE_RUN)
def cancel(run_id: str) -> Any:
    runtime = get_runtime()
    record = loop.run_sync(runtime.runs.cancel(run_id, current_principal()), timeout=10)
    return jsonify(record.public()), 202


def _await_terminal(run_id: str) -> dict[str, Any]:
    """Block until the run finishes. Convenience for scripts and tests."""
    runtime = get_runtime()
    last: dict[str, Any] = {"run_id": run_id, "status": "unknown"}
    for event in loop.iter_sync(runtime.runs.events(run_id, last_id="0")):
        if event.type in TERMINAL:
            last = {"run_id": run_id, "status": event.type.removeprefix("run."), **event.data}
            break
    return last


def _guard_size(payload: dict[str, Any], limit: int) -> None:
    size = len(str(payload))
    if size > limit:
        raise BadRequest(f"Input of {size} chars exceeds the {limit} char limit.")


def _request_id() -> str:
    return str(getattr(g, "request_id", "-"))
