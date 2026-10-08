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
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from waypoint.agents.context import AgentContext
from waypoint.agents.types import Graph
from waypoint.errors import Conflict, NotFound
from waypoint.infra.bus import TERMINAL, Event, EventBus
from waypoint.infra.db import Database
from waypoint.infra.ratelimit import Limiter
from waypoint.infra.security import Principal
from waypoint.serialization import jsonable, message_to_dict
from waypoint.settings import Settings

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
    ) -> None:
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
    ) -> RunRecord:
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

        # Take a concurrency slot *before* returning, so an over-quota caller
        # gets a 429 from the POST rather than a mysterious failed run.
        release: Any = None
        if self._limiter is not None:
            release = self._limiter.slot(principal.tenant_id)
            await release.__aenter__()

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
                {"run_id": run_id, "thread_id": thread_id, "agent": agent},
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
                ),
                name=f"run-{run_id}",
            )
        except BaseException as exc:
            await self._abandon(record, release, exc)
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
                    },
                )
            else:
                await self._bus.append(
                    run_id,
                    "run.completed",
                    {"result": self._summarize(snapshot.values)},
                )
        except asyncio.CancelledError:
            status = "cancelled"
            await self._bus.append(run_id, "run.cancelled", {"run_id": run_id})
            raise
        except TimeoutError:
            status, error = "failed", f"Run exceeded {limits.run_timeout_s}s."
            await self._bus.append(run_id, "run.failed", {"error": error, "type": "timeout"})
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("run failed", extra={"run_id": run_id, "agent": record.agent})
            await self._bus.append(
                run_id, "run.failed", {"error": error, "type": type(exc).__name__}
            )
        finally:
            record.status, record.error = status, error
            with contextlib.suppress(Exception):
                await self._bus.set_meta(run_id, record.public())
                await self._audit_end(record)
            if release is not None:
                with contextlib.suppress(Exception):
                    await release.__aexit__(None, None, None)

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
                """insert into run_audit (run_id, thread_id, tenant_id, user_id, agent, status)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (run_id) do nothing""",
                (
                    record.run_id,
                    record.thread_id,
                    record.tenant_id,
                    record.user_id,
                    record.agent,
                    record.status,
                ),
            )

    async def _audit_end(self, record: RunRecord) -> None:
        if self._db is None:
            return
        await self._db.execute(
            "update run_audit set status = %s, error = %s, ended_at = now() where run_id = %s",
            (record.status, record.error, record.run_id),
        )


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
