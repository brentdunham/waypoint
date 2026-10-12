"""Runs: start, follow, resume, cancel.

Starting a run returns ``202`` immediately with a ``run_id``. Output is read
separately from ``GET /runs/<id>/events``, an SSE stream that supports
``Last-Event-ID`` so a dropped connection resumes exactly where it stopped
rather than replaying or losing tokens.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from waypoint.agents import get_spec
from waypoint.agents.models import ModelChoiceError
from waypoint.agents.registry import models_for
from waypoint.api.deps import Reader, Runner, Runtime
from waypoint.errors import BadRequest
from waypoint.infra.bus import TERMINAL
from waypoint.schemas import Run, RunResult, RunStarted, StartRun

log = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])

WAIT_VALUES = frozenset({"1", "true", "yes"})


@router.post(
    "/threads/{thread_id}/runs",
    status_code=202,
    response_model=RunStarted,
    responses={200: {"model": RunResult, "description": "With `?wait=true`: the finished run."}},
)
async def start(
    thread_id: str,
    *,
    request: Request,
    runtime: Runtime,
    principal: Runner,
    payload: Annotated[StartRun, Body(default_factory=StartRun)],
    wait: Annotated[str | None, Query(description="`true` blocks until the run ends.")] = None,
) -> RunStarted | JSONResponse:
    thread = await runtime.threads.require(thread_id, principal)
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
        await runtime.limiter.enforce(principal.tenant_id)
        await runtime.limiter.admit(principal.tenant_id)

    record = await runtime.runs.start(
        agent=thread["agent"],
        thread_id=thread_id,
        principal=principal,
        request_id=str(getattr(request.state, "request_id", "-")),
        payload=graph_input,
        resume=payload.resume,
        checkpoint_id=payload.checkpoint_id,
        durability=payload.durability,
        models=models,
    )
    await runtime.threads.touch(thread_id, title=payload.message or None)

    if wait in WAIT_VALUES:
        result = await _await_terminal(runtime, record.run_id) | {"models": record.models}
        return JSONResponse(
            RunResult.model_validate(result).model_dump(mode="json"), status_code=200
        )

    return RunStarted.model_validate(
        record.public()
        | {
            "events_url": f"/api/v1/runs/{record.run_id}/events",
            "cancel_url": f"/api/v1/runs/{record.run_id}/cancel",
        }
    )


@router.get("/runs/{run_id}")
async def show(run_id: str, runtime: Runtime, principal: Reader) -> Run:
    return Run.model_validate((await runtime.runs.require(run_id, principal)).public())


@router.get(
    "/runs/{run_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Server-Sent Events."}},
)
async def events(
    run_id: str,
    runtime: Runtime,
    principal: Reader,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id: Annotated[
        str | None, Query(description="For clients that cannot set the Last-Event-ID header.")
    ] = None,
) -> StreamingResponse:
    """Server-Sent Events, resumable via ``Last-Event-ID``."""
    await runtime.runs.require(run_id, principal)
    last_id = last_event_id_header or last_event_id or "0"

    async def generate() -> AsyncIterator[str]:
        # Prime the connection so the browser fires `onopen` even if the first
        # real event is slow.
        yield ": stream open\n\n"
        try:
            async for event in runtime.runs.events(run_id, last_id=last_id):
                yield event.sse()
        except asyncio.CancelledError:  # client hung up; Starlette cancels us
            raise
        except Exception:
            # Following the run failed, not the run: it keeps going on its own
            # task and its events keep landing in the stream. So end this
            # response without a terminal event; the client reconnects with
            # Last-Event-ID and resumes exactly where it stopped. Sending
            # run.failed here would tell the UI a healthy run had died.
            log.exception("sse stream failed; client should resume", extra={"run_id": run_id})
            yield ": stream lost, reconnect with Last-Event-ID\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        # Tell proxies (nginx in compose) not to buffer or transform the stream.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel(run_id: str, runtime: Runtime, principal: Runner) -> Run:
    return Run.model_validate((await runtime.runs.cancel(run_id, principal)).public())


async def _await_terminal(runtime: Runtime, run_id: str) -> dict[str, Any]:
    """Wait for the run's terminal event. Convenience for scripts and tests."""
    last: dict[str, Any] = {"run_id": run_id, "status": "unknown"}
    async with contextlib.aclosing(runtime.runs.events(run_id, last_id="0")) as stream:
        async for event in stream:
            if event.type in TERMINAL:
                last = {"run_id": run_id, "status": event.type.removeprefix("run."), **event.data}
                break
    return last


def _guard_size(payload: dict[str, Any], limit: int) -> None:
    size = len(str(payload))
    if size > limit:
        raise BadRequest(f"Input of {size} chars exceeds the {limit} char limit.")
