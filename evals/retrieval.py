"""The retrieval eval: does search find the passages that answer a question?

Design decision R7. No LLM at query time, so it is cheap enough to run on
every retrieval change. Per mode (vector, keyword, hybrid) it reports
recall@5, recall@k, recall@20 and MRR, by query kind, with latency and the
cost of building the index.

* **Corpus.** ``corpora/<name>.toml`` names files at a pinned git commit, read
  with ``git cat-file``, so the gold set does not rot as the docs change.
* **Gold set.** ``datasets/retrieval.jsonl``. Each query lists *spans*: exact
  text from a document. A retrieved chunk is relevant when it comes from that
  document and contains the span (whitespace-normalised), so labels survive any
  change of chunking. ``--check`` confirms every span is findable at all.
* **Index.** Built with ``rag/index.py``, the code the ingest agent runs, into
  a Postgres of its own (``EVALS__DATABASE_URL``) under tenant ``evals``. The
  corpus name carries the index fingerprint, so a rerun reuses the vectors and
  pays only for query embeddings.
* **Recall.** For a query with several spans, the share of spans found in the
  top n. MRR uses the rank of the first relevant chunk, 0 if none in the top 20.
* **No-answer queries** have no spans. They report the best vector score, so a
  "nothing relevant" threshold can be set from data rather than guessed.
"""

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import statistics
import sys
import tomllib
from collections import defaultdict
from collections.abc import Coroutine, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from langchain_core.embeddings import Embeddings
from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.cases import DATASETS
from evals.corpus import (
    TENANT,
    Index,
    Manifest,
    build_index,
    index_fingerprint,
    load_manifest,
    read_corpus,
)
from evals.scorecard import BASELINES, PRICES, RESULTS, git_sha
from waypoint.agents.models import HashEmbeddings, build_rag_embeddings
from waypoint.infra.db import Database
from waypoint.loops import selector_loop
from waypoint.rag.chunk import Chunk, chunk_sections
from waypoint.rag.parse import media_type_for, parse
from waypoint.rag.retrieve import MODES, Mode, retrieve
from waypoint.rag.store import CorpusStore, MemoryCorpusStore, PgCorpusStore
from waypoint.settings import Settings

DATASET = DATASETS / "retrieval.jsonl"
PROPOSED = DATASETS / "retrieval.proposed.jsonl"
DEPTH = 20
"""Results retrieved per query: recall@20 and MRR look this deep."""
FLOOR = 0.05
"""Smallest drop in recall@k or MRR that fails the gate. Retrieval is
deterministic for a fixed index, so there is no trial spread to tolerate."""

type Kind = Literal["paraphrase", "identifier", "multi-hop", "no-answer"]


class SpanRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: str
    span: str = Field(min_length=10)


class Span(SpanRef):
    """One thing the answer needs, found by ``span`` or by any of its alternatives.

    A query's spans are all needed (a multi-hop query lists two); alternatives
    are other passages that say the same thing, because docs repeat
    themselves and a hit on any copy answers the question.
    """

    alternatives: list[SpanRef] = Field(default_factory=list)

    @property
    def options(self) -> list[SpanRef]:
        return [SpanRef(document=self.document, span=self.span), *self.alternatives]


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    corpus: str
    kind: Kind
    query: str = Field(min_length=3)
    relevant: list[Span] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="after")
    def _answerable_iff_spans(self) -> RetrievalCase:
        if (self.kind == "no-answer") != (not self.relevant):
            raise ValueError("no-answer cases have no spans; every other kind needs one")
        return self


def load_cases(path: Path) -> list[RetrievalCase]:
    cases = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            cases.append(RetrievalCase.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"{path.name}:{n}: {exc}") from exc
    if len({c.id for c in cases}) != len(cases):
        raise ValueError(f"{path.name}: duplicate case ids")
    return cases


# -- corpus -----------------------------------------------------------------------


def chunk_corpus(files: dict[str, bytes], chunk_tokens: int) -> dict[str, list[Chunk]]:
    """What indexing will produce, without embedding: for span checks."""
    out = {}
    for path, content in files.items():
        media_type = media_type_for(path) or "text/plain"
        parsed = parse(content, media_type, path)
        out[path] = chunk_sections(parsed.sections, path, target_tokens=chunk_tokens)
    return out


def normalize(text: str) -> str:
    return " ".join(text.split())


def contains(document: str, paragraphs: Sequence[str], span: SpanRef) -> bool:
    return document == span.document and normalize(span.span) in normalize(" ".join(paragraphs))


def satisfies(document: str, paragraphs: Sequence[str], need: Span) -> bool:
    return any(contains(document, paragraphs, option) for option in need.options)


def unfindable_spans(cases: list[RetrievalCase], chunks: dict[str, list[Chunk]]) -> list[str]:
    """Spans no chunk contains: a typo, a heading, or text split across chunks."""
    problems = []
    for case in cases:
        for span in (option for need in case.relevant for option in need.options):
            if span.document not in chunks:
                problems.append(f"{case.id}: no document {span.document!r} in the corpus")
            elif not any(
                contains(span.document, c.paragraphs, span) for c in chunks[span.document]
            ):
                problems.append(
                    f"{case.id}: span not in any chunk of {span.document}: {span.span!r}"
                )
    return problems


# -- scoring ------------------------------------------------------------------------


@dataclass(slots=True)
class QueryResult:
    case_id: str
    kind: str
    mode: str
    first_rank: int | None
    recall: dict[int, float]
    reciprocal_rank: float
    top_vector_score: float | None
    elapsed_s: float
    embed_s: float
    top: list[str] = field(default_factory=list)
    """Headings of the top five hits, ``*`` marking relevant ones, for reading."""


def score(
    case: RetrievalCase,
    ranked: Sequence[tuple[str, Sequence[str]]],
    cutoffs: Sequence[int],
) -> tuple[int | None, dict[int, float]]:
    """(rank of the first relevant hit, recall at each cutoff) for one query.

    ``ranked`` is (document path, paragraphs) per hit, best first.
    """
    first: int | None = None
    found_at: dict[int, int] = {}  # span index -> first rank containing it
    for rank, (document, paragraphs) in enumerate(ranked, start=1):
        for i, span in enumerate(case.relevant):
            if i not in found_at and satisfies(document, paragraphs, span):
                found_at[i] = rank
                first = first or rank
    total = len(case.relevant)
    recall = {
        n: (sum(1 for r in found_at.values() if r <= n) / total if total else 0.0) for n in cutoffs
    }
    return first, recall


async def evaluate(  # noqa: PLR0917
    settings: Settings,
    store: CorpusStore,
    embeddings: Embeddings,
    index: Index,
    cases: list[RetrievalCase],
    modes: Sequence[Mode],
) -> list[QueryResult]:
    k = settings.rag.top_k
    cutoffs = sorted({5, k, DEPTH})
    results = []
    for mode in modes:
        for case in cases:
            retrieval = await retrieve(
                store,
                embeddings,
                TENANT,
                index.corpus_id,
                case.query,
                k=DEPTH,
                mode=mode,
                candidates=max(settings.rag.candidates, DEPTH),
                rrf_k=settings.rag.rrf_k,
            )
            ranked = [
                (index.titles[h.chunk.document_id], h.chunk.paragraphs) for h in retrieval.hits
            ]
            first, recall = score(case, ranked, cutoffs)
            vector_scores = [h.scores["vector"] for h in retrieval.hits if "vector" in h.scores]
            top = []
            for h, (doc, paras) in list(zip(retrieval.hits, ranked, strict=True))[:5]:
                hit = any(satisfies(doc, paras, s) for s in case.relevant)
                top.append(("* " if hit else "  ") + h.chunk.heading)
            results.append(
                QueryResult(
                    case_id=case.id,
                    kind=case.kind,
                    mode=mode,
                    first_rank=first,
                    recall=recall,
                    reciprocal_rank=1 / first if first else 0.0,
                    top_vector_score=max(vector_scores) if vector_scores else None,
                    elapsed_s=retrieval.elapsed_s,
                    embed_s=retrieval.embed_s,
                    top=top,
                )
            )
    return results


# -- scorecard ----------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 4)


def fingerprint(settings: Settings, dataset: Path, index_fp: str) -> str:
    parts = {
        "dataset": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "index": index_fp,
        "top_k": settings.rag.top_k,
        "candidates": settings.rag.candidates,
        "rrf_k": settings.rag.rrf_k,
        "depth": DEPTH,
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


def _mode_summary(rows: list[QueryResult], k: int) -> dict[str, Any]:
    answerable = [r for r in rows if r.kind != "no-answer"]
    by_kind: dict[str, list[QueryResult]] = defaultdict(list)
    for r in answerable:
        by_kind[r.kind].append(r)
    latencies = [r.elapsed_s for r in rows]
    found_top = [r.top_vector_score for r in answerable if r.top_vector_score is not None]
    none_top = [
        r.top_vector_score for r in rows if r.kind == "no-answer" and r.top_vector_score is not None
    ]
    return {
        "recall@5": _mean([r.recall[5] for r in answerable]),
        f"recall@{k}": _mean([r.recall[k] for r in answerable]),
        f"recall@{DEPTH}": _mean([r.recall[DEPTH] for r in answerable]),
        "mrr": _mean([r.reciprocal_rank for r in answerable]),
        "missed": sorted(r.case_id for r in answerable if r.first_rank is None),
        "by_kind": {
            kind: {
                "queries": len(items),
                f"recall@{k}": _mean([r.recall[k] for r in items]),
                "mrr": _mean([r.reciprocal_rank for r in items]),
            }
            for kind, items in sorted(by_kind.items())
        },
        "latency_s": {"p50": _pct(latencies, 0.5), "p95": _pct(latencies, 0.95)},
        "top_vector_score": {
            "answerable_mean": _mean(found_top),
            "answerable_min": round(min(found_top), 4) if found_top else None,
            "no_answer_mean": _mean(none_top),
            "no_answer_max": round(max(none_top), 4) if none_top else None,
        },
    }


def build_card(
    settings: Settings,
    *,
    corpus: Manifest,
    dataset: Path,
    reviewed: bool,
    index: Index,
    index_fp: str,
    cases: list[RetrievalCase],
    results: list[QueryResult],
    price_per_m: float | None,
) -> dict[str, Any]:
    k = settings.rag.top_k
    modes: dict[str, list[QueryResult]] = defaultdict(list)
    for r in results:
        modes[r.mode].append(r)
    kinds: dict[str, int] = defaultdict(int)
    for c in cases:
        kinds[c.kind] += 1
    return {
        "kind": "retrieval",
        "agent": "retrieval",
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "fingerprint": fingerprint(settings, dataset, index_fp),
        "git_sha": git_sha(),
        "corpus": {"name": corpus.name, "rev": corpus.rev, "index": index_fp},
        "dataset": dataset.name,
        "reviewed": reviewed,
        "embed_model": settings.rag.embeddings_model,
        "settings": {
            "dims": settings.rag.embeddings_dims,
            "chunk_tokens": settings.rag.chunk_tokens,
            "top_k": k,
            "candidates": settings.rag.candidates,
            "rrf_k": settings.rag.rrf_k,
            "depth": DEPTH,
        },
        "queries": dict(sorted(kinds.items())),
        "index": {
            "chunks": index.chunks,
            "reused": index.reused,
            "tokens_embedded": index.tokens,
            "cost_usd": (
                round(index.tokens * price_per_m / 1_000_000, 4)
                if price_per_m is not None
                else None
            ),
            "seconds": round(index.seconds, 1),
        },
        "modes": {mode: _mode_summary(rows, k) for mode, rows in modes.items()},
        "runs": [asdict(r) for r in results],
    }


def regressions(baseline: dict[str, Any], card: dict[str, Any]) -> list[str]:
    k = card["settings"]["top_k"]
    found = []
    for mode, base in baseline.get("modes", {}).items():
        now = card["modes"].get(mode)
        if now is None:
            continue
        for metric in (f"recall@{k}", "mrr"):
            b, c = base.get(metric), now.get(metric)
            if b is not None and c is not None and b - c > FLOOR:
                found.append(f"{mode} {metric}: {b:.3f} -> {c:.3f} (drop > {FLOOR})")
    return found


def render(card: dict[str, Any], found: list[str] | None = None) -> str:
    k = card["settings"]["top_k"]
    index = card["index"]
    cost = index["cost_usd"]
    lines = [
        "# retrieval scorecard",
        "",
        f"{card['date']} · fingerprint `{card['fingerprint']}`"
        + (f" · git `{card['git_sha']}`" if card.get("git_sha") else ""),
        f"Corpus `{card['corpus']['name']}` at `{card['corpus']['rev'][:7]}`, "
        f"{index['chunks']} chunks, `{card['embed_model']}` "
        f"({card['settings']['dims']} dims, {card['settings']['chunk_tokens']}-token chunks)",
        "Queries: "
        + ", ".join(f"{n} {kind}" for kind, n in card["queries"].items())
        + f" · dataset `{card['dataset']}`"
        + ("" if card["reviewed"] else " (UNREVIEWED: not for a baseline)"),
        "Index: "
        + (
            "reused"
            if index["reused"]
            else f"built in {index['seconds']}s, ~{index['tokens_embedded']} tokens"
            + (f", ${cost:.4f}" if cost is not None else "")
        ),
        "",
        f"| mode | recall@5 | recall@{k} | recall@{DEPTH} | MRR | p50 s | p95 s | missed |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mode, m in card["modes"].items():
        lines.append(
            f"| {mode} | {m['recall@5']} | {m[f'recall@{k}']} | {m[f'recall@{DEPTH}']} | "
            f"{m['mrr']} | {m['latency_s']['p50']} | {m['latency_s']['p95']} | {len(m['missed'])} |"
        )
    kinds = [kind for kind in card["queries"] if kind != "no-answer"]
    lines += [
        "",
        f"## By kind (recall@{k} / MRR)",
        "",
        "| mode | " + " | ".join(kinds) + " |",
        "|---" * (len(kinds) + 1) + "|",
    ]
    for mode, m in card["modes"].items():
        cells = []
        for kind in kinds:
            v = m["by_kind"].get(kind, {})
            cells.append(f"{v.get(f'recall@{k}')} / {v.get('mrr')}")
        lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    lines += ["", "## Best vector score: answerable vs no-answer", ""]
    for mode, m in card["modes"].items():
        t = m["top_vector_score"]
        if t["answerable_mean"] is not None:
            lines.append(
                f"- {mode}: answerable mean {t['answerable_mean']} (min {t['answerable_min']}); "
                f"no-answer mean {t['no_answer_mean']} (max {t['no_answer_max']})"
            )
    missed = {mode: m["missed"] for mode, m in card["modes"].items() if m["missed"]}
    if missed:
        lines += ["", f"## Not found in the top {DEPTH}", ""]
        lines += [f"- {mode}: {', '.join(ids)}" for mode, ids in missed.items()]
    if found is not None:
        lines += ["", "## Baseline gate", ""]
        lines += [f"- REGRESSION {r}" for r in found] or ["- no regressions"]
    return "\n".join(lines) + "\n"


def load_baseline() -> dict[str, Any] | None:
    path = BASELINES / "retrieval.json"
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# -- the command -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plan:
    """Everything a measurement needs, decided before anything is embedded."""

    settings: Settings
    manifest: Manifest
    files: dict[str, bytes]
    dataset: Path
    reviewed: bool
    cases: list[RetrievalCase]
    modes: list[Mode]
    fake: bool


def command(args: argparse.Namespace) -> int:
    """``python -m evals retrieval``. Exit 2 on unfindable spans, 1 on a regression."""
    settings = Settings(fake_model=True) if args.fake else Settings()
    if args.fake:  # label the card honestly: these are not the configured model's vectors
        fake_rag = settings.rag.model_copy(update={"embeddings_model": "fake:hash"})
        settings = settings.model_copy(update={"rag": fake_rag})
    dataset: Path | None = args.dataset or (DATASET if DATASET.exists() else None)
    if dataset is None:
        sys.exit(
            "No reviewed gold set yet (evals/datasets/retrieval.jsonl). Review "
            "retrieval.proposed.jsonl and rename it, or pass --dataset to measure an "
            "unreviewed one."
        )
    cases = [c for c in load_cases(dataset) if c.corpus == args.corpus]
    if args.case:
        cases = [c for c in cases if c.id in set(args.case)]
    if not cases:
        sys.exit(f"{dataset.name}: no cases for corpus {args.corpus!r}")
    manifest = load_manifest(args.corpus)
    files = read_corpus(manifest)
    problems = unfindable_spans(cases, chunk_corpus(files, settings.rag.chunk_tokens))
    for problem in problems:
        print(f"  {problem}")
    if problems:
        print(f"{len(problems)} span(s) cannot be found; fix them before measuring.")
        return 2
    print(
        f"{dataset.name}: {len(cases)} queries; every span is in a chunk of the "
        f"{len(files)} files at {manifest.rev[:7]}"
    )
    if args.check:
        return 0
    reviewed = dataset.resolve() == DATASET.resolve()
    if not reviewed:
        print("  warning: unreviewed gold set; this scorecard cannot become a baseline")
    plan = Plan(
        settings, manifest, files, dataset, reviewed, cases, args.mode or list(MODES), args.fake
    )
    card = run_async(measure(plan))
    found = _gate(card) if args.against_baseline else None

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = (args.out or RESULTS) / "retrieval" / f"{stamp}-{card['fingerprint']}"
    out.mkdir(parents=True, exist_ok=True)
    runs = card.pop("runs")
    (out / "scorecard.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    with (out / "queries.jsonl").open("w", encoding="utf-8") as fh:
        for row in runs:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = render(card, found)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"written to {out}")
    return 1 if found else 0


def _gate(card: dict[str, Any]) -> list[str] | None:
    baseline = load_baseline()
    if baseline is None:
        print("  no retrieval baseline; accept a scorecard first")
        return None
    if baseline.get("fingerprint") != card["fingerprint"]:
        print(
            f"  warning: configuration {card['fingerprint']} differs from "
            f"baseline {baseline.get('fingerprint')}"
        )
    return regressions(baseline, card)


async def measure(plan: Plan) -> dict[str, Any]:
    """Open the store, build or reuse the index, run every query in every mode."""
    settings = plan.settings
    db: Database | None = None
    store: CorpusStore
    embeddings: Embeddings
    if plan.fake:
        # Checks the harness only: hash vectors carry no meaning, and the
        # in-memory keyword search is a word count, not Postgres full text.
        store, embeddings = MemoryCorpusStore(), HashEmbeddings(settings.rag.embeddings_dims)
    else:
        configured = build_rag_embeddings(settings)
        if configured is None:
            sys.exit("RAG__EMBEDDINGS_MODEL is not set. Use --fake to exercise the harness.")
        embeddings = configured
        if settings.evals.database_url is None:
            sys.exit(
                "EVALS__DATABASE_URL is not set. Point it at a database of its own, e.g. "
                "`docker compose exec postgres createdb -U waypoint waypoint_evals` and "
                "EVALS__DATABASE_URL=postgresql://waypoint:waypoint@localhost:5432/waypoint_evals"
            )
        db = await Database(str(settings.evals.database_url), max_size=4).open()
        async with db.advisory_lock():
            await db.migrate(embedding_dims=settings.rag.embeddings_dims)
        await db.check_embedding_dims(settings.rag.embeddings_dims)
        store = PgCorpusStore(db)
    try:
        index_fp = index_fingerprint(settings, plan.manifest, plan.files)
        name = f"{plan.manifest.name}-{index_fp}"
        index = await build_index(settings, store, embeddings, name, plan.files)
        print(
            f"index {index_fp}: {index.chunks} chunks, "
            + ("reused" if index.reused else f"built in {index.seconds:.1f}s")
        )
        results = await evaluate(settings, store, embeddings, index, plan.cases, plan.modes)
    finally:
        if db is not None:
            await db.close()
    prices = tomllib.loads(PRICES.read_text(encoding="utf-8")).get("embeddings", {})
    return build_card(
        settings,
        corpus=plan.manifest,
        dataset=plan.dataset,
        reviewed=plan.reviewed,
        index=index,
        index_fp=index_fp,
        cases=plan.cases,
        results=results,
        price_per_m=prices.get(settings.rag.embeddings_model or ""),
    )


def add_parser(sub: Any) -> None:
    """The ``retrieval`` subcommand's arguments."""
    ret = sub.add_parser("retrieval", help="recall and MRR of corpus search, per mode")
    ret.add_argument("--corpus", default="waypoint-docs", help="evals/corpora/<name>.toml")
    ret.add_argument("--dataset", type=Path, default=None,
                     help="gold set (default: the reviewed datasets/retrieval.jsonl)")  # fmt: skip
    ret.add_argument("--mode", action="append", choices=["vector", "keyword", "hybrid"],
                     help="repeatable; default all three")  # fmt: skip
    ret.add_argument("--case", action="append", default=[], help="query id (repeatable)")
    ret.add_argument("--check", action="store_true",
                     help="only confirm every span is findable; no embedding, no database")  # fmt: skip
    ret.add_argument("--fake", action="store_true",
                     help="hash embeddings, in-memory store; tests the harness")  # fmt: skip
    ret.add_argument("--against-baseline", action="store_true",
                     help="exit 1 when recall@k or MRR drops beyond the floor")  # fmt: skip
    ret.add_argument("--out", type=Path, default=None, help="results directory")
    ret.set_defaults(fn=command)


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """psycopg cannot use Windows' default Proactor loop (see waypoint.loops)."""
    return asyncio.run(coro, loop_factory=selector_loop if os.name == "nt" else None)
