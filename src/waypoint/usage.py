"""What a model call costs: the price table and the pricing rules.

One copy for the service and the evals (observability design, decision O1).
The eval scorecards were the first user and their numbers are the ones checked
against real bills, so any change here must keep them: ``tests/test_usage.py``
holds the parity test.

The table is ``prices.toml`` beside this module, shipped as package data so the
API image (which copies ``src`` but not ``evals``) can read it.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any, Self

PRICES_FILE = "prices.toml"


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


def call_cost(price: Price, input_tokens: int, output_tokens: int, searches: int = 0) -> float:
    """USD for one call. ``long_*`` rates apply above ``long_threshold`` input tokens.

    A web-search call is several requests billed as one call here, so a call
    whose summed input crosses the threshold is priced at the long rate even
    when each request stayed under it. That overstates, never understates.
    """
    long = price.long_threshold is not None and input_tokens > price.long_threshold
    rate_in = price.long_input if long and price.long_input is not None else price.input
    rate_out = price.long_output if long and price.long_output is not None else price.output
    tokens = input_tokens * rate_in + output_tokens * rate_out
    return tokens / 1_000_000 + searches * price.search
