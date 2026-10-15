"""Run orchestration: accept, queue, drive, stream, interrupt, resume, cancel.

The central idea is that **a run is not an HTTP request**. Starting a run
returns immediately with a ``run_id``. The API (:class:`RunManager`) admits
the run, records it and puts it on the run queue (``infra/queue.py``); a
worker (:class:`RunWorker`) takes it off and executes the graph, writing every
event to the bus. With Redis the worker is another process
(``python -m waypoint.worker``, docs/design/queue.md); without it, the API
process runs one itself. Clients attach with SSE, from any API process,
possibly after a reconnect, possibly twice. A run that hits ``interrupt()``
simply stops in a terminal state that can be resumed hours later by a second
request, which is a new run.
"""

import asyncio
import contextlib
import datetime as dt
import logging
import os
import socket
import tempfile
import time
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
from waypoint.infra.queue import Claimed, RunJob, RunQueue
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
    link_from_dict,
    link_to_dict,
    new_trace_id,
    start_run_span,
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
    """The API's side of a run: admit it, record it, queue it, follow it."""

    def __init__(
        self,
        *,
        bus: EventBus,
        db: Database | None,
        limiter: Limiter | None,
        settings: Settings,
        graphs: dict[str, Graph],
        queue: RunQueue,
        tracing: Tracing = NOOP,
    ) -> None:
        self._tracing = tracing
        self._bus = bus
        self._db = db
        self._limiter = limiter
        self._settings = settings
        self._graphs = graphs
        self._queue = queue

    async def aclose(self) -> None:
        """Nothing to stop: runs belong to workers."""

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
        """Admit and enqueue a run. ``link``: the span of the request, if traced."""
        if agent not in self._graphs:
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
            # The run is its own trace, linked to the request (O8). The worker
            # makes the span; the id is chosen here so the 202 and run.started
            # carry it from the start.
            trace_id=new_trace_id(self._tracing),
        )

        # Take a concurrency slot *before* returning, so an over-quota caller
        # gets a 429 from the POST rather than a mysterious failed run. The
        # worker that drives the run gives it back.
        slot = None
        if self._limiter is not None:
            slot = await self._limiter.acquire(principal.tenant_id)

        # From here until the job is on the queue, nothing owns the slot but
        # this frame. If anything below fails (Redis blips on set_meta, the
        # caller's run_sync timeout cancels us) give the slot back now instead
        # of leaving it held until the lease sweeps it minutes later.
        try:
            await self._bus.set_meta(run_id, record.public())
            await _audit_start(self._db, record)
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
            await self._queue.enqueue(
                RunJob(
                    run_id=run_id,
                    thread_id=thread_id,
                    agent=agent,
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    created_at=record.created_at,
                    request_id=request_id,
                    scopes=sorted(principal.scopes),
                    payload=payload,
                    resume=resume,
                    checkpoint_id=checkpoint_id,
                    durability=durability,
                    models=record.models,
                    trace_id=record.trace_id,
                    link=link_to_dict(link),
                    slot=slot,
                )
            )
        except BaseException as exc:
            await self._abandon(record, slot, exc)
            raise
        return record

    async def _abandon(self, record: RunRecord, slot: str | None, exc: BaseException) -> None:
        """Undo a start that failed before the run was queued."""
        record.status, record.error = "failed", f"Run could not start: {type(exc).__name__}"
        with contextlib.suppress(Exception):
            await self._bus.set_meta(record.run_id, record.public())
        with contextlib.suppress(Exception):
            await _audit_end(self._db, record)
        if slot is not None and self._limiter is not None:
            with contextlib.suppress(Exception):
                await self._limiter.release(record.tenant_id, slot)

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

    async def trace_id(self, run_id: str, principal: Principal) -> str | None:
        """The run's trace id, if it is the caller's tenant's run (404 otherwise).

        The Redis record lives an hour after the run; ``run_audit`` keeps the
        id for as long as the trace backend keeps the trace (a week).
        """
        record = await self.get(run_id)
        if record is not None:
            if record.tenant_id != principal.tenant_id:
                raise NotFound(f"Unknown run {run_id!r}.")
            return record.trace_id
        row = None
        if self._db is not None:
            with contextlib.suppress(Exception):  # not a uuid: no such run
                row = await self._db.fetch_one(
                    "select tenant_id, trace_id from run_audit where run_id = %s", (run_id,)
                )
        if row is None or row["tenant_id"] != principal.tenant_id:
            raise NotFound(f"Unknown run {run_id!r}.")
        trace_id: str | None = row["trace_id"]
        return trace_id

    async def cancel(self, run_id: str, principal: Principal) -> RunRecord:
        """Ask for a run to stop: the flag for a run no worker holds yet, the
        bus for the worker that holds it (Q8)."""
        record = await self.require(run_id, principal)
        if record.status != "running":
            raise Conflict(f"Run is already {record.status}.")
        await self._queue.request_cancel(run_id)
        await self._bus.request_cancel(run_id)
        return record


ALIVE_FILE = os.path.join(tempfile.gettempdir(), "waypoint-worker.alive")
"""Touched on every heartbeat by ``python -m waypoint.worker``; the compose
healthcheck checks its age."""


def consumer_name() -> str:
    """Unique per process: a restarted worker never inherits a dead one's name."""
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class RunWorker:
    """Takes runs off the queue and drives them, up to ``QUEUE__CONCURRENCY`` at once."""

    def __init__(
        self,
        *,
        queue: RunQueue,
        bus: EventBus,
        db: Database | None,
        limiter: Limiter | None,
        settings: Settings,
        graphs: dict[str, Graph],
        tracing: Tracing = NOOP,
        consumer: str | None = None,
        status: Callable[[], dict[str, Any]] | None = None,
        alive_file: str | None = None,
        drain: bool = True,
    ) -> None:
        """``status``: extra facts for the heartbeat (MCP servers' state).
        ``alive_file``: touched on each heartbeat, for a container healthcheck.
        ``drain``: wait ``QUEUE__DRAIN_S`` for runs at shutdown; the API's own
        worker (no Redis) stops them at once, as a development server should."""
        self._queue = queue
        self._bus = bus
        self._db = db
        self._limiter = limiter
        self._settings = settings
        self._graphs = graphs
        self._tracing = tracing
        self.consumer = consumer or consumer_name()
        self._status = status
        self._alive_file = alive_file
        self._capacity = asyncio.Semaphore(settings.queue.concurrency)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._background: list[asyncio.Task[None]] = []
        self._started_at = _now()
        self.claimed = 0
        self._driving: set[str] = set()
        """Runs this worker has taken off the queue since it started."""
        self.drain_s = settings.queue.drain_s if drain else 0.0

    @property
    def running(self) -> int:
        return len(self._tasks)

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if self._background:
            return
        await self._queue.ensure()
        await self._beat_once()
        self._background = [
            asyncio.create_task(self._loop(), name=f"run-worker-{self.consumer}"),
            asyncio.create_task(self._listen(), name="run-cancel-listener"),
            asyncio.create_task(self._heartbeat(), name="run-worker-heartbeat"),
        ]
        log.info(
            "run worker started",
            extra={"consumer": self.consumer, "concurrency": self._settings.queue.concurrency},
        )

    async def aclose(self) -> None:
        """Stop taking runs, give those in flight ``QUEUE__DRAIN_S`` to end, stop the rest."""
        loop_task = self._background[0] if self._background else None
        if loop_task is not None:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        if self._tasks:
            _done, still = await asyncio.wait(list(self._tasks.values()), timeout=self.drain_s)
            for task in still:
                task.cancel()
            if still:
                log.warning("stopped %d runs still going at shutdown", len(still))
                await asyncio.gather(*still, return_exceptions=True)
        for task in self._background[1:]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background = []
        with contextlib.suppress(Exception):
            await self._queue.forget(self.consumer)

    async def _loop(self) -> None:
        block_ms = self._settings.stream.block_ms
        while True:
            await self._capacity.acquire()
            try:
                claimed = await self._queue.read(self.consumer, block_ms=block_ms)
            except asyncio.CancelledError:
                self._capacity.release()
                raise
            except Exception:
                self._capacity.release()
                log.warning("run queue read failed; retrying", exc_info=True)
                await asyncio.sleep(1.0)
                continue
            if claimed is None:
                self._capacity.release()
                continue
            self.claimed += 1
            run_id = claimed.job.run_id
            task = asyncio.create_task(self._run(claimed), name=f"run-{run_id}")
            self._tasks[run_id] = task

            def done(_t: asyncio.Task[None], run_id: str = run_id) -> None:
                self._tasks.pop(run_id, None)
                self._capacity.release()

            task.add_done_callback(done)

    async def _listen(self) -> None:
        """Cancel runs this worker drives when someone asks, from any API process."""
        while True:
            try:
                async for run_id in self._bus.cancellations():
                    # Only a run inside its graph is cancelled here. One still
                    # being set up finds the cancel flag, which is set before
                    # this message is sent, when it enters the graph (_drive).
                    task = self._tasks.get(run_id)
                    if run_id in self._driving and task is not None and not task.done():
                        log.info("cancelling run", extra={"run_id": run_id})
                        task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("cancel listener reconnecting", exc_info=True)
                await asyncio.sleep(1.0)

    async def _heartbeat(self) -> None:
        every = self._settings.queue.heartbeat_s
        while True:
            await asyncio.sleep(every)
            try:
                await self._beat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("worker heartbeat failed", exc_info=True)

    async def _beat_once(self) -> None:
        info: dict[str, Any] = {
            "started_at": self._started_at,
            "running": self.running,
            "claimed": self.claimed,
            "concurrency": self._settings.queue.concurrency,
        }
        if self._status is not None:
            info.update(self._status())
        await self._queue.beat(self.consumer, info, ttl_s=self._settings.queue.heartbeat_s * 3)
        if self._alive_file:
            await asyncio.to_thread(_touch, self._alive_file)

    # -- one run ----------------------------------------------------------------

    async def _run(self, claimed: Claimed) -> None:
        """Drive one claimed run, and take it off the queue however it ends."""
        job = claimed.job
        entered = False
        try:
            if await self._queue.cancel_requested(job.run_id):
                await self._end_unstarted(job, "cancelled", None)
            elif (graph := self._graphs.get(job.agent)) is None:
                await self._end_unstarted(job, "failed", f"No agent named {job.agent!r}.")
            else:
                entered = True
                await self._drive(job, graph)
        except asyncio.CancelledError:
            if not entered:  # stopped at shutdown before its graph began
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._end_unstarted(job, "cancelled", None))
            # Otherwise the run already wrote its run.cancelled.
        except Exception:
            log.exception("run worker could not finish a run", extra={"run_id": job.run_id})
        finally:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._queue.ack(claimed.entry_id))

    async def _end_unstarted(self, job: RunJob, status: str, error: str | None) -> None:
        """End a run that never reached its graph: cancelled while queued, or unknown."""
        record = _record(job)
        record.status, record.error = status, error
        event = "run.cancelled" if status == "cancelled" else "run.failed"
        data: dict[str, Any] = {"run_id": job.run_id}
        if error:
            data |= {"error": error, "type": "NotFound"}
        await self._bus.append(job.run_id, event, data)
        with contextlib.suppress(Exception):
            await self._bus.set_meta(job.run_id, record.public())
        with contextlib.suppress(Exception):
            await _audit_end(self._db, record)
        await self._release_slot(job)

    async def _release_slot(self, job: RunJob) -> None:
        if job.slot is not None and self._limiter is not None:
            with contextlib.suppress(Exception):
                await self._limiter.release(job.tenant_id, job.slot)

    async def _open(self, job: RunJob) -> Span:
        """The run's span, and its slot's lease restarted now that it starts."""
        span = start_run_span(
            self._tracing,
            agent=job.agent,
            run_id=job.run_id,
            thread_id=job.thread_id,
            tenant_id=job.tenant_id,
            resume=job.resume is not None,
            link=link_from_dict(job.link),
            trace_id=job.trace_id,
        )
        if job.slot is not None and self._limiter is not None:
            # The lease ran from when the API accepted the run; time in the
            # queue should not count against it.
            with contextlib.suppress(Exception):
                await self._limiter.refresh(job.tenant_id, job.slot)
        return span

    def _config(self, job: RunJob) -> tuple[RunnableConfig, AgentContext]:
        # Identity is not plumbing: it goes in the context below, never here.
        # Configurable values are copied into checkpoint metadata, and tools
        # that read identity from config would bypass the context entirely.
        configurable: dict[str, Any] = {"thread_id": job.thread_id}
        if job.checkpoint_id:
            configurable["checkpoint_id"] = job.checkpoint_id
        config: RunnableConfig = {
            "configurable": configurable,
            "recursion_limit": self._settings.limits.recursion_limit,
            "run_id": uuid.UUID(job.run_id),
            "metadata": {"tenant_id": job.tenant_id, "agent": job.agent},
            "tags": [f"tenant:{job.tenant_id}", f"agent:{job.agent}"],
        }
        context = AgentContext(
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            request_id=job.request_id,
            run_id=job.run_id,
            agent=job.agent,
            models=tuple(sorted(job.models.items())),
            scopes=tuple(job.scopes),
        )
        return config, context

    async def _drive(self, job: RunJob, graph: Graph) -> None:
        record = _record(job)
        run_id, limits, durability, resume = (
            job.run_id,
            self._settings.limits,
            job.durability,
            job.resume,
        )
        span = await self._open(job)
        config, context = self._config(job)
        graph_input: Any = Command(resume=resume) if resume is not None else job.payload

        # Every chat model call in the graph reports to the meter through the
        # callbacks; embeddings and reranking find it through METER. This task
        # has its own copy of the context, so neither leaks to other runs.
        meter = Meter(charge=self._charger(record.tenant_id))
        observed = _Observed.open(self._tracing, record, span, meter)
        config["callbacks"] = observed.callbacks

        status, error = "completed", None
        self._driving.add(run_id)
        try:
            if await self._queue.cancel_requested(run_id):
                raise asyncio.CancelledError  # asked before the listener would act
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
            self._driving.discard(run_id)
            record.status, record.error = status, error
            self._usage(record, meter)  # the terminal append may not have been reached
            # Costs reported after the last model call (a final rerank, say).
            await asyncio.shield(meter.flush())
            with contextlib.suppress(Exception):
                await self._bus.set_meta(run_id, record.public())
            try:
                await _audit_end(self._db, record)
            except Exception:
                # Spend is never lost silently: if Postgres missed it, the log has it.
                log.warning("could not write the run's audit row", extra={"usage": record.usage})
            observed.close(status, error, record.usage)
            await self._release_slot(job)

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


def _touch(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(time.time()))


def _record(job: RunJob) -> RunRecord:
    return RunRecord(
        run_id=job.run_id,
        thread_id=job.thread_id,
        agent=job.agent,
        tenant_id=job.tenant_id,
        user_id=job.user_id,
        status="running",
        created_at=job.created_at,
        models=dict(job.models),
        trace_id=job.trace_id,
    )


# -- audit -------------------------------------------------------------------


async def _audit_start(db: Database | None, record: RunRecord) -> None:
    if db is None:
        return
    with contextlib.suppress(Exception):
        await db.execute(
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


async def _audit_end(db: Database | None, record: RunRecord) -> None:
    if db is None:
        return
    usage = record.usage or {}
    await db.execute(
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
