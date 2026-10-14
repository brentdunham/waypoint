"""Retrieval (rag/retrieve.py) and the retrieval eval's harness. Unit tier.

The in-memory store's searches are stand-ins, so these tests check plumbing
and arithmetic: fusion, ranks, scoring, span checks. What Postgres actually
returns is covered in test_rag_pg.py; how well it ranks is the eval's job.
"""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals import corpus
from evals import retrieval as rv
from waypoint.agents.models import HashEmbeddings
from waypoint.rag.chunk import Chunk
from waypoint.rag.retrieve import Retriever, retrieve, rrf
from waypoint.rag.store import MemoryCorpusStore, NewDocument
from waypoint.settings import RagSettings


def test_rrf_sums_reciprocal_ranks_across_methods() -> None:
    fused = rrf({"vector": ["a", "b", "c"], "keyword": ["c", "a"]}, k=60)
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert fused["b"] == pytest.approx(1 / 62)
    # Found by both beats found once, even at a worse rank.
    assert sorted(fused, key=lambda x: -fused[x]) == ["a", "c", "b"]


async def _corpus() -> tuple[MemoryCorpusStore, HashEmbeddings, str]:
    store, embeddings = MemoryCorpusStore(), HashEmbeddings(dims=16)
    corpus = await store.create_corpus("acme", "c", "m")
    [added] = await store.add_documents(
        "acme", corpus.corpus_id, [NewDocument("a.md", "text/markdown", b"x")]
    )
    texts = [
        "Caddy terminates TLS for the demo host.",
        "Set STREAM__BLOCK_MS below the socket timeout.",
        "Reconnect with Last-Event-ID to resume a stream.",
    ]
    chunks = [Chunk(i, f"a.md > {i}", (t,)) for i, t in enumerate(texts)]
    vectors = embeddings.embed_documents([c.text for c in chunks])
    await store.write_chunks("acme", added.document.document_id, chunks, vectors)
    return store, embeddings, corpus.corpus_id


def test_hybrid_reports_each_methods_rank_and_fuses_them() -> None:
    async def scenario() -> None:
        store, embeddings, corpus_id = await _corpus()
        # The query is a chunk's exact embedded text: vector rank 1 by construction.
        query = "a.md > 2\n\nReconnect with Last-Event-ID to resume a stream."
        result = await retrieve(
            store, embeddings, "acme", corpus_id, query, k=2, mode="hybrid", candidates=10
        )
        assert result.mode == "hybrid"
        assert len(result.hits) == 2
        top = result.hits[0]
        assert top.chunk.ordinal == 2
        assert top.ranks == {"vector": 1, "keyword": 1}
        assert top.score == pytest.approx(2 / 61)
        assert top.source.endswith("#2")
        assert result.embed_s >= 0

        keyword = await retrieve(
            store, None, "acme", corpus_id, "STREAM__BLOCK_MS", k=3, mode="keyword"
        )
        assert [h.chunk.ordinal for h in keyword.hits] == [1]
        assert keyword.embed_s == 0

        # Another tenant's corpus is invisible to every mode.
        for mode in ("vector", "keyword", "hybrid"):
            other = await retrieve(store, embeddings, "evil", corpus_id, query, k=3, mode=mode)  # type: ignore[arg-type]
            assert other.hits == []

    asyncio.run(scenario())


def test_vector_modes_need_an_embedder() -> None:
    async def scenario() -> None:
        store, _, corpus_id = await _corpus()
        with pytest.raises(ValueError, match="embeddings"):
            await retrieve(store, None, "acme", corpus_id, "q", k=1, mode="vector")

    asyncio.run(scenario())


def test_retriever_uses_the_configured_defaults() -> None:
    async def scenario() -> None:
        store, embeddings, corpus_id = await _corpus()
        retriever = Retriever(store, embeddings, RagSettings(top_k=1, mode="keyword"))
        result = await retriever.search("acme", corpus_id, "Caddy TLS")
        assert result.mode == "keyword"
        assert [h.chunk.ordinal for h in result.hits] == [0]

    asyncio.run(scenario())


def test_several_corpora_are_one_search_and_one_corpus_is_unchanged() -> None:
    async def scenario() -> None:
        store, embeddings, first = await _corpus()
        second = (await store.create_corpus("acme", "c2", "m")).corpus_id
        [added] = await store.add_documents(
            "acme", second, [NewDocument("b.md", "text/markdown", b"y")]
        )
        chunk = Chunk(0, "b.md > 0", ("Budgets cap a tenant's daily spend in dollars.",))
        await store.write_chunks(
            "acme", added.document.document_id, [chunk], embeddings.embed_documents([chunk.text])
        )
        query = "b.md > 0\n\nBudgets cap a tenant's daily spend in dollars."
        both = await retrieve(store, embeddings, "acme", [first, second], query, k=4, mode="vector")
        assert both.hits[0].chunk.heading == "b.md > 0"  # the other corpus's chunk wins
        assert {h.chunk.heading.split(" ")[0] for h in both.hits} == {"a.md", "b.md"}

        one = await retrieve(store, embeddings, "acme", first, query, k=3, mode="hybrid")
        listed = await retrieve(store, embeddings, "acme", [first], query, k=3, mode="hybrid")
        assert [(h.source, h.score) for h in one.hits] == [(h.source, h.score) for h in listed.hits]

        # A corpus the tenant cannot read adds nothing, and hides nothing.
        other = (await store.create_corpus("evil", "x", "m")).corpus_id
        guarded = await retrieve(
            store, embeddings, "acme", [first, other], query, k=3, mode="vector"
        )
        assert all(h.chunk.heading.startswith("a.md") for h in guarded.hits)

    asyncio.run(scenario())


# -- the eval's arithmetic ----------------------------------------------------------


def _case(*spans: tuple[str, str], kind: str = "paraphrase") -> rv.RetrievalCase:
    return rv.RetrievalCase(
        id="q",
        corpus="c",
        kind=kind,  # type: ignore[arg-type]
        query="question",
        relevant=[rv.Span(document=d, span=s) for d, s in spans],
    )


def test_a_hit_is_relevant_when_its_document_holds_the_span() -> None:
    case = _case(("a.md", "the  answer\nis here"), ("b.md", "second half of it"))
    ranked = [
        ("a.md", ("nothing useful",)),
        ("b.md", ("the answer is here",)),  # right words, wrong document
        ("a.md", ("so: the answer is here.",)),  # whitespace differs, still a match
        ("x.md", ("filler",)),
        ("x.md", ("filler",)),
        ("b.md", ("and the second half of it",)),
    ]
    first, recall = rv.score(case, ranked, [1, 3, 5, 6])
    assert first == 3
    assert recall == {1: 0.0, 3: 0.5, 5: 0.5, 6: 1.0}


def test_an_alternative_span_satisfies_its_need_and_only_its_need() -> None:
    need = rv.Span(
        document="a.md",
        span="the first wording",
        alternatives=[rv.SpanRef(document="b.md", span="the same point, elsewhere")],
    )
    other = rv.Span(document="c.md", span="a second fact entirely")
    case = rv.RetrievalCase(
        id="q", corpus="c", kind="multi-hop", query="question", relevant=[need, other]
    )
    ranked = [
        ("b.md", ("... the same point, elsewhere ...",)),
        ("c.md", ("a second fact entirely",)),
    ]
    first, recall = rv.score(case, ranked, [1, 2])
    assert first == 1
    assert recall == {1: 0.5, 2: 1.0}  # alternatives are OR; separate needs are AND


def test_no_answer_cases_have_no_spans_and_everything_else_needs_one() -> None:
    with pytest.raises(ValueError, match="no-answer"):
        _case(kind="paraphrase")
    with pytest.raises(ValueError, match="no-answer"):
        _case(("a.md", "some span text"), kind="no-answer")
    assert rv.score(_case(kind="no-answer"), [("a.md", ("x",))], [5]) == (None, {5: 0.0})


def test_unfindable_spans_are_reported() -> None:
    chunks = {"a.md": [Chunk(0, "a.md", ("first paragraph", "second paragraph"))]}
    good = _case(("a.md", "first paragraph"))
    missing = _case(("a.md", "not in the text at all"))
    other = _case(("b.md", "first paragraph"))
    problems = rv.unfindable_spans([good, missing, other], chunks)
    assert len(problems) == 2
    assert "span not in any chunk" in problems[0]
    assert "no document 'b.md'" in problems[1]


def test_regressions_gate_on_recall_at_k_and_mrr() -> None:
    base = {"modes": {"hybrid": {"recall@8": 0.9, "mrr": 0.7}}}
    card = {"settings": {"top_k": 8}, "modes": {"hybrid": {"recall@8": 0.87, "mrr": 0.6}}}
    found = rv.regressions(base, card)
    assert len(found) == 1
    assert found[0].startswith("hybrid mrr")


# -- the gold set ---------------------------------------------------------------------


def _git_has(rev: str) -> bool:
    if shutil.which("git") is None:
        return False
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}^{{commit}}"],
        cwd=corpus.REPO,
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.parametrize("dataset", [rv.DATASET, rv.PROPOSED], ids=lambda p: Path(p).name)
def test_every_gold_span_is_findable_in_the_pinned_corpus(dataset: Path) -> None:
    """A span that no chunk contains can never be found, so it would only lower
    recall. Caught here, before anyone pays for a measurement."""
    if not dataset.exists():
        pytest.skip(f"{dataset.name} does not exist yet")
    manifest = corpus.load_manifest("waypoint-docs")
    if not _git_has(manifest.rev):
        pytest.skip("git or the pinned commit is unavailable")
    cases = rv.load_cases(dataset)
    chunks = rv.chunk_corpus(corpus.read_corpus(manifest), RagSettings().chunk_tokens)
    assert rv.unfindable_spans(cases, chunks) == []
    kinds = {c.kind for c in cases}
    assert kinds == {"paraphrase", "identifier", "multi-hop", "no-answer"}


def test_the_harness_runs_end_to_end_offline(tmp_path: Path) -> None:
    from evals.__main__ import main

    if not _git_has(corpus.load_manifest("waypoint-docs").rev):
        pytest.skip("git or the pinned commit is unavailable")
    if not rv.DATASET.exists():
        pytest.skip("no reviewed gold set")
    code = main(
        ["retrieval", "--fake", "--case", "q17-payload-too-large", "--case", "q35-kubernetes",
         "--out", str(tmp_path)]
    )  # fmt: skip
    assert code == 0
    [card_path] = tmp_path.glob("retrieval/*/scorecard.json")
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["embed_model"] == "fake:hash"
    assert card["dataset"] == "retrieval.jsonl"
    assert set(card["modes"]) == {"vector", "keyword", "hybrid"}
    assert card["queries"] == {"identifier": 1, "no-answer": 1}
