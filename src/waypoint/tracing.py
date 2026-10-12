"""OpenTelemetry traces: one per run, from the HTTP request to each model call.

Observability design, phase 3 (decisions O6 to O9):

* **Export over OTLP/HTTP, or not at all.** ``OTEL_EXPORTER_OTLP_ENDPOINT``
  set means spans go to ``<endpoint>/v1/traces`` (Jaeger, Tempo, Langfuse,
  Honeycomb: any backend, switched by config). Unset means a no-op tracer and
  no measurable overhead. The standard ``OTEL_*`` variables (headers, service
  name, resource attributes) are read by the SDK itself.
* **A run is its own trace** (O8). Its root span is linked to the
  ``POST /runs`` request span instead of parented by it: a run outlives its
  request by minutes and may be resumed hours later from another worker.
* **Our own callback handler makes the graph and model spans** (O7).
  :class:`TraceHandler` turns LangChain callbacks into one span per graph node
  and per chat model call, parented by ``parent_run_id`` rather than the
  current context, so spans nest correctly under concurrent workers. The
  attributes are chosen here: GenAI semantic conventions (still "Development"
  status) for model and tokens, ``waypoint.*`` for role, cost, tenant and run.
  Keeping the mapping in this one module makes a convention rename one edit.
* **No content by default** (O9). Prompts, completions and retrieved text stay
  out of spans unless ``TRACING__CONTENT=true``, which is for local debugging.

The provider is built per process, when the app is created. gunicorn imports
the app in each worker after forking (no ``--preload``), so every worker has
its own exporter thread.
"""

import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_core.runnables.config import var_child_runnable_config
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Link, Span, SpanContext, SpanKind, Status, StatusCode, Tracer

from waypoint.usage import PriceTable, load_prices, replies, usage_of

if TYPE_CHECKING:
    from waypoint.settings import Settings

log = logging.getLogger(__name__)

SERVICE = "waypoint"
CONTENT_LIMIT = 8_000
"""Characters of prompt or completion kept per span with TRACING__CONTENT."""
MAX_DEPTH = 1_000
"""Guard on walking parent runs; LangGraph nests a few levels, never this many."""
PROBES = "healthz,readyz"
"""Not traced: hit every 30 s by health checks, and they say nothing about a run."""


@dataclass(slots=True)
class Tracing:
    """The process's tracer, or a no-op one. Shut down when the app stops."""

    tracer: Tracer
    provider: TracerProvider | None
    content: bool = False

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    def shutdown(self) -> None:
        if self.provider is not None:
            self.provider.shutdown()


NOOP = Tracing(tracer=trace.NoOpTracer(), provider=None)


def setup_tracing(settings: Settings, exporter: SpanExporter | None = None) -> Tracing:
    """Build this process's tracer from settings.

    ``exporter`` replaces the OTLP one and exports synchronously; tests pass
    an in-memory exporter. Not registered as the global provider: each app
    (and each test app) keeps its own, so two never fight over the global.
    """
    endpoint = settings.otel_exporter_otlp_endpoint
    if exporter is None and not endpoint:
        return NOOP
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        assert endpoint is not None
        url = endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=url)))
        log.info("tracing to %s", url)
    return Tracing(
        tracer=provider.get_tracer(SERVICE), provider=provider, content=settings.tracing.content
    )


def current_span_context() -> SpanContext | None:
    """The current span's context if it is a real, sampled one."""
    ctx = trace.get_current_span().get_span_context()
    return ctx if ctx.is_valid else None


def trace_id_of(span: Span) -> str | None:
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


# -- the run span ------------------------------------------------------------------------


def start_run_span(
    tracing: Tracing,
    *,
    agent: str,
    run_id: str,
    thread_id: str,
    tenant_id: str,
    resume: bool,
    link: SpanContext | None,
) -> Span:
    """A run's root span: a new trace, linked to the request that started it (O8)."""
    return tracing.tracer.start_span(
        f"run {agent}",
        context=otel_context.Context(),  # a new root, whatever is current
        kind=SpanKind.INTERNAL,
        links=[Link(link)] if link is not None else None,
        attributes={
            "waypoint.run_id": run_id,
            "waypoint.thread_id": thread_id,
            "waypoint.tenant_id": tenant_id,
            "waypoint.agent": agent,
            "waypoint.resume": resume,
        },
    )


def end_run_span(span: Span, status: str, error: str | None, usage: dict[str, Any] | None) -> None:
    span.set_attribute("waypoint.status", status)
    if usage:
        if usage.get("cost_usd") is not None:
            span.set_attribute("waypoint.cost_usd", float(usage["cost_usd"]))
        span.set_attribute("gen_ai.usage.input_tokens", int(usage.get("input_tokens") or 0))
        span.set_attribute("gen_ai.usage.output_tokens", int(usage.get("output_tokens") or 0))
        span.set_attribute("waypoint.searches", int(usage.get("searches") or 0))
    if status == "failed":
        span.set_status(Status(StatusCode.ERROR, (error or "")[:200]))
    span.end()


# -- graph and model spans ------------------------------------------------------------------


class TraceHandler(AsyncCallbackHandler):
    """LangChain callbacks to spans: graph nodes, chat model calls, tools.

    Every span is parented explicitly. A callback run that gets no span of its
    own (the graph itself, a node's inner runnables) is remembered, so its
    children attach to the nearest ancestor that has one, and in the end to
    the run span. Never raises into the run: each method catches and logs.
    """

    def __init__(
        self,
        tracing: Tracing,
        root: Span,
        *,
        attributes: dict[str, str],
        prices: PriceTable | None = None,
    ) -> None:
        self._tracer = tracing.tracer
        self._content = tracing.content
        self._root = root
        self._attributes = attributes
        self._prices = prices if prices is not None else load_prices()
        self._spans: dict[uuid.UUID, Span] = {}
        self._parents: dict[uuid.UUID, uuid.UUID | None] = {}
        self._models: dict[uuid.UUID, str] = {}

    # -- parenting --

    def span_for(self, run_id: uuid.UUID | None) -> Span:
        """The span of ``run_id`` or of its nearest ancestor with one; the run span."""
        seen = 0
        while run_id is not None and seen < MAX_DEPTH:
            if (span := self._spans.get(run_id)) is not None:
                return span
            run_id = self._parents.get(run_id)
            seen += 1
        return self._root

    def _start(
        self,
        name: str,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None,
        attributes: dict[str, Any],
        kind: SpanKind = SpanKind.INTERNAL,
    ) -> None:
        parent = trace.set_span_in_context(self.span_for(parent_run_id))
        self._spans[run_id] = self._tracer.start_span(
            name, context=parent, kind=kind, attributes=self._attributes | attributes
        )

    def _end(self, run_id: uuid.UUID, error: BaseException | None = None) -> Span | None:
        self._parents.pop(run_id, None)
        span = self._spans.pop(run_id, None)
        if span is not None:
            if error is not None:
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(Status(StatusCode.ERROR, type(error).__name__))
            span.end()
        return span

    # -- graph nodes --

    @override
    async def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any] | Any,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._parents[run_id] = parent_run_id
            node = (metadata or {}).get("langgraph_node")
            # A node's own run is named after it; its inner runnables inherit
            # the metadata but not the name, so they get no span of their own.
            if node and (name or kwargs.get("name")) == node:
                attrs: dict[str, Any] = {"waypoint.node": node}
                if (step := (metadata or {}).get("langgraph_step")) is not None:
                    attrs["waypoint.step"] = int(step)
                self._start(f"node {node}", run_id, parent_run_id, attrs)
        except Exception:
            log.warning("could not trace a chain start", exc_info=True)

    @override
    async def on_chain_end(self, outputs: Any, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._end(run_id)

    @override
    async def on_chain_error(
        self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any
    ) -> None:
        # GraphInterrupt is how a node pauses for a person: not a failure.
        self._end(run_id, None if type(error).__name__ == "GraphInterrupt" else error)

    # -- chat model calls --

    @override
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._parents[run_id] = parent_run_id
            meta = metadata or {}
            model = str(meta.get("model_name") or meta.get("ls_model_name") or "unknown")
            attrs: dict[str, Any] = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": str(meta.get("ls_provider") or "unknown"),
                "gen_ai.request.model": model,
                "waypoint.role": str(meta.get("model_role") or "unknown"),
            }
            if self._content:
                attrs["waypoint.prompt"] = _text(messages)
            self._models[run_id] = model
            self._start(f"chat {model}", run_id, parent_run_id, attrs, SpanKind.CLIENT)
        except Exception:
            log.warning("could not trace a model call", exc_info=True)

    @override
    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        span = self._spans.get(run_id)
        model = self._models.pop(run_id, None)
        try:
            if span is not None:
                self._annotate(span, response, model)
        except Exception:
            log.warning("could not annotate a model span", exc_info=True)
        self._end(run_id)

    @override
    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._models.pop(run_id, None)
        self._end(run_id, error)

    def _annotate(self, span: Span, response: LLMResult, requested: str | None) -> None:
        messages = replies(response)
        total = [usage_of(m) for m in messages]
        tokens_in = sum(u.total_input_tokens for u in total)
        tokens_out = sum(u.output_tokens for u in total)
        span.set_attribute("gen_ai.usage.input_tokens", tokens_in)
        span.set_attribute("gen_ai.usage.output_tokens", tokens_out)
        span.set_attribute("waypoint.cache_read_tokens", sum(u.cache_read_tokens for u in total))
        span.set_attribute(
            "waypoint.cache_write_tokens",
            sum(u.cache_write_5m_tokens + u.cache_write_1h_tokens for u in total),
        )
        span.set_attribute("waypoint.searches", sum(u.searches for u in total))
        reported = next(
            (
                str(m.response_metadata.get("model_name"))
                for m in messages
                if getattr(m, "response_metadata", None) and m.response_metadata.get("model_name")
            ),
            None,
        )
        if reported:
            span.set_attribute("gen_ai.response.model", reported)
        price = self._prices.models.get(requested) if requested else None
        if price is not None:
            usd = sum(u.cost(price) for u in total)
            span.set_attribute("waypoint.cost_usd", usd)
            span.set_attribute("gen_ai.usage.cost", usd)  # read by Langfuse
        if self._content:
            span.set_attribute("waypoint.completion", _text([messages]))

    # -- tools --

    @override
    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: uuid.UUID,
        parent_run_id: uuid.UUID | None = None,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._parents[run_id] = parent_run_id
            tool = str(name or (serialized or {}).get("name") or "tool")
            attrs: dict[str, Any] = {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool,
            }
            if self._content:
                attrs["waypoint.tool_input"] = input_str[:CONTENT_LIMIT]
            self._start(f"execute_tool {tool}", run_id, parent_run_id, attrs)
        except Exception:
            log.warning("could not trace a tool call", exc_info=True)

    @override
    async def on_tool_end(self, output: Any, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._end(run_id)

    @override
    async def on_tool_error(
        self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any
    ) -> None:
        self._end(run_id, error)

    def close(self) -> None:
        """End anything still open (a cancelled run), so no span is lost."""
        for run_id in list(self._spans):
            span = self._spans.pop(run_id)
            span.set_attribute("waypoint.unfinished", True)
            span.end()
        self._parents.clear()
        self._models.clear()


def _text(batches: Sequence[Sequence[Any]]) -> str:
    parts: list[str] = []
    for batch in batches:
        for message in batch:
            content = getattr(message, "content", message)
            if isinstance(content, list):
                content = " ".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict) and "text" in b
                )
            parts.append(str(content))
    return "\n".join(parts)[:CONTENT_LIMIT]


# -- spans for code outside LangChain -------------------------------------------------------

TRACE: ContextVar[TraceHandler | None] = ContextVar("trace_handler", default=None)
"""The current run's handler, so code outside LangChain can add spans in the
right place. Set by ``RunManager._drive``; unset in evals and tests."""


def _parent_context() -> otel_context.Context | None:
    """The span of the runnable that is executing now, from LangChain's config."""
    handler = TRACE.get()
    if handler is None:
        return None
    config = var_child_runnable_config.get() or {}
    manager = config.get("callbacks")
    run_id = getattr(manager, "parent_run_id", None)
    return trace.set_span_in_context(handler.span_for(run_id))


@contextmanager
def child_span(name: str, **attributes: Any) -> Iterator[Span | None]:
    """A span under whatever node is running, for work LangChain does not see.

    Yields ``None`` outside a traced run, so callers can skip computing
    attributes nobody will read.
    """
    handler = TRACE.get()
    parent = _parent_context()
    if handler is None or parent is None:
        yield None
        return
    span = handler._tracer.start_span(
        name, context=parent, attributes=handler._attributes | attributes
    )
    try:
        yield span
    except BaseException as exc:
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        raise
    finally:
        span.end()
