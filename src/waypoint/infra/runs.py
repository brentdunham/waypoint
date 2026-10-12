"""Run orchestration: start, stream, interrupt, resume, cancel.

The central idea is that **a run is not an HTTP request**. Starting a run
returns immediately with a ``run_id``; the graph executes as a task on the
shared event loop and writes every event to the bus. Clients then attach with
SSE, possibly from a different worker, possibly after a reconnect, possibly
twice. A run that hits ``interrupt()`` simply stops in a terminal state that
can be resumed hours later by a second request.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from opentelemetry import trace
from opentelemetry.trace import Span, SpanContext
from psycopg.types.json import Jsonb

from waypoint.agents.context import AgentContext
from waypoint.agents.types import Graph
from waypoint.errors import Conflict, NotFound
from waypoint.infra.bus import TERMINAL, Event, EventBus
from waypoint.infra.db import Database
from waypoint.infra.ratelimit import Limiter
from waypoint.infra.security import Principal
from waypoint.log_config import bind, unbind
from waypoint.serialization import jsonable, message_to_dict
from waypoint.settings import Settings
from waypoint.tracing import (
    NOOP,
    TRACE,
    TraceHandler,
    Tracing,
    end_run_span,
    start_run_span,
    trace_id_of,
)
from waypoint.usage import METER, Meter

log = logging.getLogger(__name__)

STREAM_MODES = ["updates", "messages", "custom"]


@dataclass(slots=True)
class RunRecord:
    run_id: str
    thread_id: str
    agent: str
    tenant_id: str
    user_id: str
    status: str
    created_at: str
    error: str | None = None
    models: dict[str, str] = field(default_factory=dict)
    """Role -> model this run used. Empty for runs started before routing existed."""
    usage: dict[str, Any] | None = None
    """Cost, tokens and searches, per role and in total (``Meter.summary``).
    ``None`` while running, and for runs from before metering existed."""
    trace_id: str | None = None
    """The run's OpenTelemetry trace (32 hex digits); ``None`` with tracing off."""

    def public(self) -> dict[str, Any]:
        return asdict(self)


class RunManager:
    def __init__(
        self,
        *,
        bus: EventBus,
        db: Database | None,
        limiter: Limiter | None,
        settings: Settings,
        graphs: dict[str, Graph],
        tracing: Tracing = NOOP,
    ) -> None:
        self._tracing = tracing
        self._bus = bus
        self._db = db
        self._limiter = limiter
        self._settings = settings
        self._graphs = graphs
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_listener: asyncio.Task[None] | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start_listener(self) -> None:
        """Watch for cancellations requested on *other* workers."""
        if self._cancel_listener is not None:
            return

        async def _listen() -> None:
            while True:
                try:
                    async for run_id in self._bus.cancellations():
                        task = self._tasks.get(run_id)
                        if task is not None and not task.done():
                            log.info("cancelling run", extra={"run_id": run_id})
                            task.cancel()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("cancel listener reconnecting", exc_info=True)
                    await asyncio.sleep(1.0)

        self._cancel_listener = asyncio.create_task(_listen(), name="run-cancel-listener")

    async def aclose(self) -> None:
        if self._cancel_listener:
            self._cancel_listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cancel_listener
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # -- starting ------------------------------------------------------------

    async def start(
        self,
        *,
        agent: str,
        thread_id: str,
        principal: Principal,
        request_id: str,
        payload: dict[str, Any] | None = None,
        resume: Any = None,
        checkpoint_id: str | None = None,
        durability: str = "async",
        models: dict[str, str] | None = None,
        link: SpanContext | None = None,
    ) -> RunRecord:
        """``link``: the span of the request that started the run, if traced."""
        graph = self._graphs.get(agent)
        if graph is None:
            raise NotFound(f"No agent named {agent!r}.")

        run_id = str(uuid.uuid4())
        record = RunRecord(
            run_id=run_id,
            thread_id=thread_id,
            agent=agent,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            status="running",
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            models=dict(models or {}),
        )
        # The run is its own trace, linked to the request (O8). Started here so
        # the trace id is on the record and the run.started event from the start.
        span = start_run_span(
            self._tracing,
            agent=agent,
            run_id=run_id,
            thread_id=thread_id,
            tenant_id=principal.tenant_id,
            resume=resume is not None,
            link=link,
        )
        record.trace_id = trace_id_of(span)

        # Take a concurrency slot *before* returning, so an over-quota caller
        # gets a 429 from the POST rather than a mysterious failed run.
        release: Any = None
        if self._limiter is not None:
            release = self._limiter.slot(principal.tenant_id)
            try:
                await release.__aenter__()
            except BaseException:
                end_run_span(span, "refused", None, None)
                raise

        # From here until the task exists, nothing owns the slot but this frame.
        # If anything below fails -- Redis blips on set_meta, the caller's
        # run_sync timeout cancels us -- give the slot back now instead of
        # leaving it held until the lease sweeps it minutes later.
        try:
            await self._bus.set_meta(run_id, record.public())
            await self._audit_start(record)
            await self._bus.append(
                run_id,
                "run.started",
                {
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "agent": agent,
                    "models": record.models,
                    "trace_id": record.trace_id,
                },
            )

            task = asyncio.create_task(
                self._drive(
                    record=record,
                    graph=graph,
                    payload=payload,
                    resume=resume,
                    checkpoint_id=checkpoint_id,
                    request_id=request_id,
                    durability=durability,
                    release=release,
                    span=span,
                ),
                name=f"run-{run_id}",
            )
        except BaseException as exc:
            await self._abandon(record, release, exc)
            end_run_span(span, "failed", record.error, None)
            raise
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))
        return record

    async def _abandon(self, record: RunRecord, release: Any, exc: BaseException) -> None:
        """Undo a start that failed before its task took ownership of the slot."""
        record.status, record.error = "failed", f"Run could not start: {type(exc).__name__}"
        with contextlib.suppress(Exception):
            await self._bus.set_meta(record.run_id, record.public())
        with contextlib.suppress(Exception):
            await self._audit_end(record)
        if release is not None:
            with contextlib.suppress(Exception):
                await release.__aexit__(None, None, None)

    # -- execution ------------------------------------------------------------

    async def _drive(
        self,
        *,
        record: RunRecord,
        graph: Graph,
        payload: dict[str, Any] | None,
        resume: Any,
        checkpoint_id: str | None,
        request_id: str,
        durability: str,
        release: Any,
        span: Span,
    ) -> None:
        run_id = record.run_id
        limits = self._settings.limits
        # Identity is not plumbing: it goes in the context below, never here.
        # Configurable values are copied into checkpoint metadata, and tools
        # that read identity from config would bypass the context entirely.
        configurable: dict[str, Any] = {"thread_id": record.thread_id}
        if checkpoint_id:
            configurable["checkpoint_id"] = checkpoint_id
        config: RunnableConfig = {
            "configurable": configurable,
            "recursion_limit": limits.recursion_limit,
            "run_id": uuid.UUID(run_id),
            "metadata": {"tenant_id": record.tenant_id, "agent": record.agent},
            "tags": [f"tenant:{record.tenant_id}", f"agent:{record.agent}"],
        }
        context = AgentContext(
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            request_id=request_id,
            run_id=run_id,
            agent=record.agent,
            models=tuple(sorted(record.models.items())),
        )
        graph_input: Any = Command(resume=resume) if resume is not None else payload

        # Every chat model call in the graph reports to the meter through the
        # callbacks; embeddings and reranking find it through METER. This task
        # has its own copy of the context, so neither leaks to other runs.
        meter = Meter(charge=self._charger(record.tenant_id))
        observed = _Observed.open(self._tracing, record, span, meter)
        config["callbacks"] = observed.callbacks

        status, error = "completed", None
        try:
            async with asyncio.timeout(limits.run_timeout_s):
                async for mode, chunk in graph.astream(  # type: ignore[call-overload]
                    graph_input,
                    config=config,
                    context=context,
                    stream_mode=STREAM_MODES,
                    durability=durability,
                ):
                    await self._emit(run_id, mode, chunk)

            snapshot = await graph.aget_state(config)
            if snapshot.interrupts:
                status = "interrupted"
                await self._bus.append(
                    run_id,
                    "run.interrupted",
                    {
                        "interrupts": [
                            {"id": getattr(i, "id", None), "value": jsonable(i.value)}
                            for i in snapshot.interrupts
                        ],
                        "next": list(snapshot.next or ()),
                        "usage": self._usage(record, meter),
                    },
                )
            else:
                await self._bus.append(
                    run_id,
                    "run.completed",
                    {
                        "result": self._summarize(snapshot.values),
                        "usage": self._usage(record, meter),
                    },
                )
        except asyncio.CancelledError:
            status = "cancelled"
            await self._bus.append(
                run_id, "run.cancelled", {"run_id": run_id, "usage": self._usage(record, meter)}
            )
            raise
        except TimeoutError:
            status, error = "failed", f"Run exceeded {limits.run_timeout_s}s."
            await self._bus.append(
                run_id,
                "run.failed",
                {"error": error, "type": "timeout", "usage": self._usage(record, meter)},
            )
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("run failed")
            await self._bus.append(
                run_id,
                "run.failed",
                {"error": error, "type": type(exc).__name__, "usage": self._usage(record, meter)},
            )
        finally:
            record.status, record.error = status, error
            self._usage(record, meter)  # the terminal append may not have been reached
            # Costs reported after the last model call (a final rerank, say).
            await asyncio.shield(meter.flush())
            with contextlib.suppress(Exception):
                await self._bus.set_meta(run_id, record.public())
            try:
                await self._audit_end(record)
            except Exception:
                # Spend is never lost silently: if Postgres missed it, the log has it.
                log.warning("could not write the run's audit row", extra={"usage": record.usage})
            observed.close(status, error, record.usage)
            if release is not None:
                with contextlib.suppress(Exception):
                    await release.__aexit__(None, None, None)

    def _charger(self, tenant_id: str) -> Callable[[float], Awaitable[None]] | None:
        """What the run's meter charges its costs to: the tenant's budget, if any."""
        limiter = self._limiter
        if limiter is None or not limiter.budgets_on:
            return None

        async def charge(usd: float) -> None:
            await limiter.charge(tenant_id, usd)

        return charge

    @staticmethod
    def _usage(record: RunRecord, meter: Meter) -> dict[str, Any] | None:
        """Snapshot the meter onto the record; never fails the run."""
        try:
            record.usage = meter.summary()
        except Exception:
            log.warning("could not summarise the run's usage", exc_info=True)
        return record.usage

    async def _emit(self, run_id: str, mode: str, chunk: Any) -> None:
        match mode:
            case "messages":
                message, meta = chunk
                text = getattr(message, "content", "")
                if isinstance(text, list):  # provider content blocks
                    text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
                if text:
                    await self._bus.append(
                        run_id,
                        "token",
                        {"text": text, "node": (meta or {}).get("langgraph_node")},
                    )
            case "updates":
                for node, update in (chunk or {}).items():
                    if node == "__interrupt__":
                        continue  # emitted once, authoritatively, from the snapshot
                    await self._bus.append(
                        run_id,
                        "node",
                        {"node": node, "update": jsonable(update)},
                    )
            case "custom":
                await self._bus.append(run_id, "custom", {"payload": jsonable(chunk)})
            case _:  # pragma: no cover - defensive
                log.debug("unhandled stream mode %s", mode)

    @staticmethod
    def _summarize(values: Any) -> dict[str, Any]:
        if not isinstance(values, dict):
            return {"values": jsonable(values)}
        out: dict[str, Any] = {}
        if messages := values.get("messages"):
            out["message"] = message_to_dict(messages[-1])
        for key in ("report", "plan", "findings", "topic", "step_count", "metadata"):
            if key in values:
                out[key] = jsonable(values[key])
        return out

    # -- reading ---------------------------------------------------------------

    async def get(self, run_id: str) -> RunRecord | None:
        meta = await self._bus.get_meta(run_id)
        return RunRecord(**meta) if meta else None

    async def require(self, run_id: str, principal: Principal) -> RunRecord:
        record = await self.get(run_id)
        if record is None:
            raise NotFound(f"Unknown run {run_id!r}.")
        if record.tenant_id != principal.tenant_id:
            raise NotFound(f"Unknown run {run_id!r}.")  # do not leak existence
        return record

    async def events(
        self, run_id: str, *, last_id: str = "0", heartbeat_s: float | None = None
    ) -> AsyncGenerator[Event]:
        """Replay from ``last_id`` then follow live until a terminal event."""
        block_ms = self._settings.stream.block_ms
        heartbeat = heartbeat_s or self._settings.stream.heartbeat_s
        idle = 0.0
        while True:
            events = await self._bus.read(run_id, last_id=last_id, block_ms=block_ms)
            if not events:
                idle += block_ms / 1000
                if idle >= heartbeat:
                    idle = 0.0
                    yield Event(id=last_id, type="heartbeat", data={"ts": _now()})
                record = await self.get(run_id)
                if record and record.status not in {"running"}:
                    # Terminal state reached while we were blocked with nothing
                    # new to read: the run ended, stop following.
                    tail = await self._bus.read(run_id, last_id=last_id, block_ms=0)
                    for event in tail:
                        yield event
                    return
                continue
            idle = 0.0
            for event in events:
                last_id = event.id
                yield event
                if event.type in TERMINAL:
                    return

    async def cancel(self, run_id: str, principal: Principal) -> RunRecord:
        record = await self.require(run_id, principal)
        if record.status != "running":
            raise Conflict(f"Run is already {record.status}.")
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        else:
            await self._bus.request_cancel(run_id)  # it belongs to another worker
        return record

    # -- audit -------------------------------------------------------------------

    async def _audit_start(self, record: RunRecord) -> None:
        if self._db is None:
            return
        with contextlib.suppress(Exception):
            await self._db.execute(
                """insert into run_audit
                       (run_id, thread_id, tenant_id, user_id, agent, status, trace_id)
                   values (%s, %s, %s, %s, %s, %s, %s)
                   on conflict (run_id) do nothing""",
                (
                    record.run_id,
                    record.thread_id,
                    record.tenant_id,
                    record.user_id,
                    record.agent,
                    record.status,
                    record.trace_id,
                ),
            )

    async def _audit_end(self, record: RunRecord) -> None:
        if self._db is None:
            return
        usage = record.usage or {}
        await self._db.execute(
            """update run_audit
               set status = %s, error = %s, ended_at = now(),
                   cost_usd = %s, input_tokens = %s, output_tokens = %s, searches = %s,
                   usage = %s
               where run_id = %s""",
            (
                record.status,
                record.error,
                usage.get("cost_usd"),
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("searches"),
                Jsonb(usage["roles"]) if "roles" in usage else None,
                record.run_id,
            ),
        )


@dataclass(slots=True)
class _Observed:
    """What a run's task sets up so it can be measured and traced, and undoes.

    Every chat model call reports to the meter (callbacks); embeddings and
    reranking find it through ``METER``. With tracing on, ``TraceHandler``
    makes node and model spans under the run span, and retrieval finds it
    through ``TRACE``. The run span is current for the task, so anything
    traced inside belongs to the run's trace, and log lines carry its ids.
    The task has its own copy of the context, so none of this leaks to other
    runs.
    """

    callbacks: list[Any]
    span: Span
    tracer: TraceHandler | None
    tokens: Sequence[Callable[[], object]]

    @classmethod
    def open(cls, tracing: Tracing, record: RunRecord, span: Span, meter: Meter) -> _Observed:
        tracer = None
        if tracing.enabled:
            tracer = TraceHandler(
                tracing,
                span,
                attributes={
                    "waypoint.run_id": record.run_id,
                    "waypoint.tenant_id": record.tenant_id,
                    "waypoint.agent": record.agent,
                },
            )
        meter_token = METER.set(meter)
        trace_token = TRACE.set(tracer)
        scope = trace.use_span(span, end_on_exit=False)
        scope.__enter__()
        log_token = bind(
            run_id=record.run_id,
            agent=record.agent,
            tenant_id=record.tenant_id,
            trace_id=record.trace_id,
        )
        undo = [
            lambda: unbind(log_token),
            lambda: scope.__exit__(None, None, None),
            lambda: TRACE.reset(trace_token),
            lambda: METER.reset(meter_token),
        ]
        callbacks: list[Any] = [meter] if tracer is None else [meter, tracer]
        return cls(callbacks=callbacks, span=span, tracer=tracer, tokens=undo)

    def close(self, status: str, error: str | None, usage: dict[str, Any] | None) -> None:
        if self.tracer is not None:
            self.tracer.close()
        with contextlib.suppress(Exception):
            end_run_span(self.span, status, error, usage)
        for undo in self.tokens:
            undo()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
