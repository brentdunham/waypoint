"""What a run costs: the price table, the pricing rules and the meter.

One copy for the service and the evals (observability design, decision O1).
The eval scorecards were the first user and their numbers are the ones checked
against real bills, so any change here must keep them: ``tests/test_usage.py``
holds the parity test.

The table is ``prices.toml`` beside this module, shipped as package data so the
API image (which copies ``src`` but not ``evals``) can read it.

:class:`Meter` is a LangChain callback handler: every chat model call in a run
reports its usage to it, attributed to a role through the ``model_role`` and
``model_name`` metadata that ``RoleModel.named`` puts on each role's runnable.
Costs outside LangChain (query and document embeddings, reranking) are reported
with :func:`report_embedding` and :func:`report_rerank`, which find the current
run's meter through :data:`METER` and do nothing when there is none.
"""

import logging
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Self, override

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

if TYPE_CHECKING:
    from waypoint.settings import Settings

log = logging.getLogger(__name__)

PRICES_FILE = "prices.toml"

CACHE_READ = 0.1
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0
"""Prompt-cache multipliers of the input rate (platform.claude.com pricing,
checked 2026-10-10): a cache read costs a tenth of an input token, a write
1.25 times (5-minute TTL) or twice (1-hour TTL)."""


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens for one chat model, plus USD per web search.

    ``long_*`` rates apply to calls whose input is above ``long_threshold``
    tokens, for models priced by prompt length.
    """

    input: float
    output: float
    search: float = 0.0
    long_threshold: int | None = None
    long_input: float | None = None
    long_output: float | None = None

    def __post_init__(self) -> None:
        long = (self.long_threshold, self.long_input, self.long_output)
        if any(v is not None for v in long) and any(v is None for v in long):
            raise ValueError("long_threshold, long_input and long_output go together")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> Self:
        threshold = row.get("long_threshold")
        return cls(
            input=float(row["input"]),
            output=float(row["output"]),
            search=float(row.get("search", 0.0)),
            long_threshold=int(threshold) if threshold is not None else None,
            long_input=float(row["long_input"]) if "long_input" in row else None,
            long_output=float(row["long_output"]) if "long_output" in row else None,
        )


@dataclass(frozen=True, slots=True)
class PriceTable:
    as_of: str
    models: dict[str, Price]
    """Chat models, by model name."""
    embeddings: dict[str, float]
    """USD per million input tokens, by ``RAG__EMBEDDINGS_MODEL`` spec."""
    rerank: dict[str, float]
    """USD per million billed tokens, by ``RAG__RERANK_MODEL``."""


def parse_prices(text: str) -> PriceTable:
    """A price table from TOML text in the ``prices.toml`` format."""
    data = tomllib.loads(text)
    models = data.get("models", {})
    return PriceTable(
        as_of=str(data.get("as_of", "")),
        models={k: Price.from_mapping(v) for k, v in models.items() if isinstance(v, dict)},
        embeddings={k: float(v) for k, v in data.get("embeddings", {}).items()},
        rerank={k: float(v) for k, v in data.get("rerank", {}).items()},
    )


@cache
def load_prices() -> PriceTable:
    """The packaged table, read once per process."""
    return parse_prices(files("waypoint").joinpath(PRICES_FILE).read_text(encoding="utf-8"))


def call_cost(
    price: Price,
    input_tokens: int,
    output_tokens: int,
    searches: int = 0,
    *,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """USD for one call. ``long_*`` rates apply above ``long_threshold`` input tokens.

    ``input_tokens`` is the uncached input, as the API bills it; cache reads
    and writes are passed separately and priced at their multiple of the input
    rate. The long-context threshold applies to all input, cached or not.

    A web-search call is several requests billed as one call here, so a call
    whose summed input crosses the threshold is priced at the long rate even
    when each request stayed under it. That overstates, never understates.
    """
    cached = cache_read_tokens + cache_write_5m_tokens + cache_write_1h_tokens
    long = price.long_threshold is not None and input_tokens + cached > price.long_threshold
    rate_in = price.long_input if long and price.long_input is not None else price.input
    rate_out = price.long_output if long and price.long_output is not None else price.output
    billed_in = (
        input_tokens
        + cache_read_tokens * CACHE_READ
        + cache_write_5m_tokens * CACHE_WRITE_5M
        + cache_write_1h_tokens * CACHE_WRITE_1H
    )
    tokens = billed_in * rate_in + output_tokens * rate_out
    return tokens / 1_000_000 + searches * price.search


# -- one call's usage ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallUsage:
    """What one chat model reply reports, split the way the API bills it."""

    input_tokens: int = 0
    """Uncached input. langchain's ``usage_metadata["input_tokens"]`` is the
    total with the cached parts included; this is that total minus them."""
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    """Server-side web search requests, billed per request on top of tokens."""

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )

    def cost(self, price: Price) -> float:
        return call_cost(
            price,
            self.input_tokens,
            self.output_tokens,
            self.searches,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens,
        )


def usage_of(message: Any) -> CallUsage:
    """The usage a chat model reply reports, or zeros when it reports none.

    langchain-anthropic reports cache writes per TTL when the API does, and
    then zeroes the generic ``cache_creation`` key; a write reported only
    under the generic key is taken as the 5-minute TTL, the API's default.
    Web searches are in ``response_metadata["usage"]``, not in
    ``usage_metadata``.
    """
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    read = int(details.get("cache_read") or 0)
    write_1h = int(details.get("ephemeral_1h_input_tokens") or 0)
    write_5m = int(details.get("ephemeral_5m_input_tokens") or 0) + int(
        details.get("cache_creation") or 0
    )
    total_in = int(usage.get("input_tokens") or 0)
    raw = (getattr(message, "response_metadata", None) or {}).get("usage") or {}
    server = raw.get("server_tool_use") or {}
    return CallUsage(
        input_tokens=max(0, total_in - read - write_5m - write_1h),
        cache_read_tokens=read,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        output_tokens=int(usage.get("output_tokens") or 0),
        searches=int(server.get("web_search_requests") or 0),
    )


def replies(response: LLMResult) -> list[Any]:
    """The messages in a chat model's ``on_llm_end`` result."""
    return [
        message
        for generations in response.generations
        for generation in generations
        if (message := getattr(generation, "message", None)) is not None
    ]


# -- the meter -------------------------------------------------------------------------


@dataclass(slots=True)
class RoleUsage:
    """One role's usage in a run. Chat roles count calls and tokens; ``embed``
    and ``rerank`` count only ``tokens``."""

    models: set[str] = field(default_factory=set)
    calls: int = 0
    input_tokens: int = 0
    """All input, cached parts included, so it reads like the API console."""
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    searches: int = 0
    tokens: int = 0
    """Embedding or rerank tokens (estimated for embeddings, see report_embedding)."""
    cost_usd: float | None = 0.0
    """``None`` once a call by a model missing from the price table is counted."""
    priced_usd: float = 0.0
    """The cost of the priced calls alone: what the budget is charged."""

    def charge(self, usd: float | None) -> None:
        if usd is None:
            self.cost_usd = None
            return
        self.priced_usd += usd
        if self.cost_usd is not None:
            self.cost_usd += usd

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"model": ", ".join(sorted(self.models))}
        if self.calls:
            out |= {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "searches": self.searches,
            }
        if self.tokens:
            out["tokens"] = self.tokens
        out["cost_usd"] = round(self.cost_usd, 6) if self.cost_usd is not None else None
        return out


UNKNOWN = "unknown"


class Meter(AsyncCallbackHandler):
    """Accumulates one run's usage and cost, per role.

    Never raises into the run: LangChain logs and ignores handler errors by
    default (``raise_error`` stays off), and every method here also catches
    its own errors and logs them at WARNING, so a pricing bug costs a usage
    line, never a run.

    With ``charge`` (the budget, ``Limiter.charge`` bound to the run's tenant)
    each chat model call's cost is charged as soon as the call ends, along
    with any embedding and rerank cost reported since the last charge;
    :meth:`flush` charges what is left when the run ends. A failed charge is
    logged and not retried: the spend is still in the summary and
    ``run_audit``, only not enforced (observability design, Risks).
    """

    def __init__(
        self,
        prices: PriceTable | None = None,
        *,
        charge: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.prices = prices if prices is not None else load_prices()
        self.roles: dict[str, RoleUsage] = {}
        self._open: dict[uuid.UUID, tuple[str, str]] = {}
        self._charge = charge
        self.charged_usd = 0.0

    def _role(self, role: str) -> RoleUsage:
        return self.roles.setdefault(role, RoleUsage())

    # -- chat model calls, through callbacks --

    @override
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
        # A model built outside a RoleModel (the contextualizer) has no role
        # metadata; langchain still reports the model as ``ls_model_name``.
        model = meta.get("model_name") or meta.get("ls_model_name") or UNKNOWN
        self._open[run_id] = (str(meta.get("model_role") or UNKNOWN), str(model))

    @override
    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        role_model = self._open.pop(run_id, None)
        if role_model is None:
            return
        try:
            for message in replies(response):
                self.add_call(*role_model, usage_of(message))
        except Exception:
            log.warning("could not meter a model call", exc_info=True)
        await self.flush()

    def priced_usd(self) -> float:
        """What the priced calls so far cost; unpriced ones count as zero here."""
        return sum(row.priced_usd for row in self.roles.values())

    async def flush(self) -> None:
        """Charge the budget for everything not yet charged."""
        if self._charge is None:
            return
        due = self.priced_usd() - self.charged_usd
        if due <= 0:
            return
        # Counted before the await, so concurrent calls never charge twice.
        self.charged_usd += due
        try:
            await self._charge(due)
        except Exception:
            log.warning("could not charge the budget", exc_info=True, extra={"usd": due})

    @override
    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        self._open.pop(run_id, None)

    def add_call(self, role: str, model: str, usage: CallUsage) -> None:
        row = self._role(role)
        row.models.add(model)
        row.calls += 1
        row.input_tokens += usage.total_input_tokens
        row.output_tokens += usage.output_tokens
        row.cache_read_tokens += usage.cache_read_tokens
        row.cache_write_tokens += usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
        row.searches += usage.searches
        price = self.prices.models.get(model)
        row.charge(usage.cost(price) if price is not None else None)

    # -- everything else, reported by the code that spent it --

    def add(self, role: str, model: str, *, tokens: int, usd: float | None) -> None:
        """A cost that is not a chat model call: embeddings, reranking."""
        row = self._role(role)
        row.models.add(model)
        row.tokens += tokens
        row.charge(usd)

    # -- reading --

    def summary(self) -> dict[str, Any]:
        """Per-role usage plus run totals; the shape ``run_audit.usage`` stores.

        ``cost_usd`` is ``None`` when any call was by a model the price table
        does not list: a total that silently left a call out would be wrong.
        """
        roles = {name: row.summary() for name, row in sorted(self.roles.items())}
        costs = [row.cost_usd for row in self.roles.values()]
        unknown = any(c is None for c in costs)
        return {
            "cost_usd": None if unknown else round(sum(c or 0.0 for c in costs), 6),
            "input_tokens": sum(r.input_tokens for r in self.roles.values()),
            "output_tokens": sum(r.output_tokens for r in self.roles.values()),
            "searches": sum(r.searches for r in self.roles.values()),
            "roles": roles,
        }


METER: ContextVar[Meter | None] = ContextVar("usage_meter", default=None)
"""The current run's meter, for code that spends money outside LangChain.

Set by ``RunManager._drive`` around the graph, so graph nodes and the tasks
they start see it. Unset in evals and tests unless they set it.
"""


def report_embedding(model: str | None, tokens: int, *, role: str = "embed") -> None:
    """Charge ``tokens`` embedded with ``model`` (a ``RAG__EMBEDDINGS_MODEL`` spec).

    Callers pass an estimate (``rag.chunk.estimate_tokens``): langchain's
    embeddings interface returns vectors only, not the provider's count.
    """
    meter = METER.get()
    if meter is None or not model or tokens <= 0:
        return
    rate = meter.prices.embeddings.get(model)
    meter.add(
        role, model, tokens=tokens, usd=tokens * rate / 1_000_000 if rate is not None else None
    )


def report_rerank(model: str, tokens: int) -> None:
    """Charge ``tokens`` the reranker billed (Voyage reports the exact count)."""
    meter = METER.get()
    if meter is None or tokens <= 0:
        return
    rate = meter.prices.rerank.get(model)
    meter.add(
        "rerank", model, tokens=tokens, usd=tokens * rate / 1_000_000 if rate is not None else None
    )


class UnpricedModelError(ValueError):
    """A model the service may call is not in the price table."""


def check_prices(settings: Settings, prices: PriceTable | None = None) -> list[str]:
    """Models the service may call that ``prices.toml`` does not list (D3).

    With budgets on that is a boot error: an unpriced model would spend money
    without being charged to anyone's budget. Off, it is returned for a
    warning, and those runs report their cost as unknown.
    """
    prices = prices if prices is not None else load_prices()
    named = set(settings.allowed_models)
    if settings.rag.contextual and settings.rag.enabled:
        named.add(settings.rag.context_model)
    missing = sorted(m for m in named if m not in prices.models)
    if missing and settings.budget.active and not settings.fake_model:
        raise UnpricedModelError(
            f"Budgets are on, but src/waypoint/prices.toml has no price for: "
            f"{', '.join(missing)}. Add them (platform.claude.com pricing), or the "
            "service would spend without charging any budget."
        )
    return missing
