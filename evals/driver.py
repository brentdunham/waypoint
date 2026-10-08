"""Run one case through an agent's graph, in process, and record what happened.

Not over HTTP: the API adds nothing an eval measures, and running the graph
directly lets each case have its own store (seeded memories) and lets every
model call be observed with a callback. The graph is the same ``build``
function the service registers, so a prompt or model change is evaluated
exactly as it would ship.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from evals.cases import Case
from evals.replay import Replay
from waypoint.agents.context import AgentContext, memory_namespace
from waypoint.agents.models import INTERCEPTOR, build_embeddings
from waypoint.agents.registry import get_spec
from waypoint.serialization import jsonable
from waypoint.settings import Settings

EVAL_TENANT = "evals"


@dataclass(slots=True)
class Call:
    """One model call. ``role`` and ``model`` come from the router's metadata."""

    role: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    latency_s: float = 0.0


@dataclass(slots=True)
class Transcript:
    agent: str
    case_id: str
    trial: int
    models: dict[str, str]
    values: dict[str, Any] = field(default_factory=dict)
    """Final graph state, JSON-shaped."""
    interrupts: list[Any] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    """Live model calls. Replayed ones are counted in ``replayed`` instead."""
    replayed: dict[str, int] = field(default_factory=dict)
    """Role -> model calls answered from the replay store (not billed)."""
    error: str | None = None
    elapsed_s: float = 0.0

    def tool_calls(self) -> list[dict[str, Any]]:
        return [
            call
            for message in self.values.get("messages", [])
            for call in message.get("tool_calls") or []
        ]

    def reply(self) -> str:
        """The agent's final answer: the briefing, or the last AI message."""
        if report := self.values.get("report"):
            return str(report)
        for message in reversed(self.values.get("messages", [])):
            if message.get("type") == "ai" and message.get("content"):
                return str(message["content"])
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "case_id": self.case_id,
            "trial": self.trial,
            "models": self.models,
            "values": self.values,
            "interrupts": self.interrupts,
            "calls": [
                {
                    "role": c.role,
                    "model": c.model,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "searches": c.searches,
                    "latency_s": round(c.latency_s, 3),
                }
                for c in self.calls
            ],
            "replayed": self.replayed,
            "error": self.error,
            "elapsed_s": round(self.elapsed_s, 2),
        }


class UsageRecorder(AsyncCallbackHandler):
    """Collects a :class:`Call` per chat model call.

    The structured-output path of the scripted test model is not a chat model,
    so offline runs record nothing; with a real model every call lands here.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._open: dict[uuid.UUID, tuple[float, Call]] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        meta = metadata or {}
        call = Call(
            role=str(meta.get("model_role", "unknown")),
            model=str(meta.get("model_name", "unknown")),
        )
        self._open[run_id] = (time.perf_counter(), call)

    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        started, call = self._open.pop(run_id, (None, None))
        if call is None or started is None:
            return
        call.latency_s = time.perf_counter() - started
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                call.input_tokens += int(usage.get("input_tokens", 0))
                call.output_tokens += int(usage.get("output_tokens", 0))
                raw = (getattr(message, "response_metadata", None) or {}).get("usage") or {}
                server = raw.get("server_tool_use") or {}
                call.searches += int(server.get("web_search_requests", 0) or 0)
        self.calls.append(call)

    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._open.pop(run_id, None)


def _turn_input(input_key: str, text: str) -> dict[str, Any]:
    if input_key == "messages":
        return {"messages": [{"role": "user", "content": text}]}
    return {input_key: text}


async def run_case(
    settings: Settings,
    agent: str,
    case: Case,
    *,
    models: dict[str, str],
    trial: int = 0,
    replay: Replay | None = None,
) -> Transcript:
    """Run every turn of ``case``; never raises for an agent failure.

    With ``replay``, model calls seen before are answered from its store and
    new ones are recorded (``evals.replay``).
    """
    token = INTERCEPTOR.set(replay)
    try:
        return await _run_case(settings, agent, case, models=models, trial=trial, replay=replay)
    finally:
        INTERCEPTOR.reset(token)


async def _run_case(
    settings: Settings,
    agent: str,
    case: Case,
    *,
    models: dict[str, str],
    trial: int,
    replay: Replay | None,
) -> Transcript:
    spec = get_spec(agent)
    user = f"{case.id}-{trial}-{uuid.uuid4().hex[:6]}"
    transcript = Transcript(agent=agent, case_id=case.id, trial=trial, models=dict(models))

    embeddings = build_embeddings(settings)
    index = (
        {"dims": settings.embeddings_dims, "embed": embeddings, "fields": ["fact"]}
        if embeddings is not None
        else None
    )
    store = InMemoryStore(index=index)  # type: ignore[arg-type]
    for n, fact in enumerate(case.seed):
        await store.aput(memory_namespace(EVAL_TENANT, user), f"seed{n}", {"fact": fact})

    graph = spec.builder(settings, checkpointer=InMemorySaver(), store=store)
    recorder = UsageRecorder()
    run_id = str(uuid.uuid4())
    context = AgentContext(
        tenant_id=EVAL_TENANT,
        user_id=user,
        request_id=f"eval-{case.id}",
        run_id=run_id,
        agent=agent,
        models=tuple(sorted(models.items())),
    )
    config: Any = {
        "configurable": {"thread_id": run_id},
        "recursion_limit": settings.limits.recursion_limit,
        "callbacks": [recorder],
        "tags": ["evals", f"case:{case.id}"],
    }
    answers = iter(case.resume)
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.limits.run_timeout_s):
            for turn in case.turns:
                graph_input: Any = _turn_input(spec.input_key, turn)
                while True:
                    async for _ in graph.astream(graph_input, config, context=context):
                        pass
                    snapshot = await graph.aget_state(config)
                    if not snapshot.interrupts:
                        break
                    transcript.interrupts.extend(jsonable(i.value) for i in snapshot.interrupts)
                    answer = next(answers, None)
                    if answer is None:
                        break  # the case stops at an unanswered interrupt
                    graph_input = Command(resume=answer)
        snapshot = await graph.aget_state(config)
        transcript.values = jsonable(snapshot.values) or {}
    except Exception as exc:
        transcript.error = f"{type(exc).__name__}: {exc}"
    transcript.elapsed_s = time.perf_counter() - started
    transcript.calls = recorder.calls
    if replay is not None:
        transcript.replayed = dict(sorted(replay.replayed.items()))
    return transcript
