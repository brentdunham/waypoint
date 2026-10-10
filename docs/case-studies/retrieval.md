# Case study: which retrieval techniques earned their place

October 2026. Phase 4 of the [RAG design](../design/rag.md) asked a narrow
question: of the techniques usually recommended for retrieval, which ones
improve search over this repository's own documentation, and at what cost?
Each one was measured against the same gold set and kept only if it beat
plain vector search by more than the noise. One did: a reranker. It is now
on by default. The others are switches in `RAG__*`, measured and left off.

Every number below comes from a scorecard in `evals/results/retrieval/`
(2026-10-10): the 17 files of `docs/` at commit `1777465`, 40 hand-reviewed
queries (34 answerable, 6 with no answer in the docs), voyage-4 at 1024
dimensions, 400-token chunks, Postgres with pgvector, unless a row says
otherwise.

## Summary

Vector search, top 20 per query:

| variant | recall@5 | recall@8 | recall@20 | MRR | p50 latency | extra cost |
|---|---|---|---|---|---|---|
| **baseline**: voyage-4, 400-token chunks | 0.88 | 0.93 | 0.97 | 0.80 | 0.18 s | |
| 250-token chunks | 0.87 | 0.91 | 0.97 | 0.73 | 0.19 s | |
| 800-token chunks | 0.91 | 0.94 | 0.99 | 0.80 | 0.18 s | |
| voyage-4-large | 0.87 | 0.93 | 0.99 | 0.80 | 0.20 s | 2x embedding price |
| voyage-context-4 | 0.87 | 0.90 | 0.99 | 0.75 | 0.21 s | 2x embedding price |
| contextual retrieval (Haiku preambles) | 0.84 | 0.93 | 0.97 | 0.75 | 0.19 s | $0.05 per index |
| **rerank-3** | **0.96** | **0.97** | 0.99 | **0.90** | 0.40 s | $0.0007 per query |
| rerank-3 + contextual | 0.97 | 0.97 | 0.99 | 0.91 | 0.40 s | both |
| rerank-3 + 800-token chunks | 0.96 | 0.97 | 0.99 | 0.90 | 0.39 s | $0.0008 per query |
| rerank-3 + 250-token chunks | 0.97 | 0.97 | 0.97 | 0.91 | 0.38 s | $0.0005 per query |

How big a difference has to be before it means anything: with 34 answerable
queries, one query moving from rank 1 to rank 2 changes MRR by 0.015, and one
query's span entering or leaving the top 5 changes recall@5 by up to 0.03.
Differences of 0.03 or less are one or two queries.

## 1. The reranker: the only clear win

`rerank-3` reads the query and each of the 50 nearest chunks together and
reorders them. Against plain vector search, query by query:

* 10 queries moved up, 3 moved down, 21 did not move. All three moves down
  were from rank 1 to rank 2.
* The moves up were large where it mattered: a paraphrased question about the
  research progress feed went from rank 16 to 5; one about a skipped
  verification from 8 to 3; one about `Last-Event-ID` from 5 to 1.
* Identifier and paraphrase queries gained most: MRR 0.67 to 0.88 on
  identifiers, where full-text search had been expected to be needed
  (phase 2), and 0.86 to 0.95 on paraphrases. Multi-hop queries stayed level
  (0.83 to 0.81): two of the three one-place drops were multi-hop.

It also makes the first-stage method nearly irrelevant: reranked keyword
search reached MRR 0.78 (from 0.41) and reranked hybrid 0.90 (from 0.62). So
the decision in phase 2 to default to vector stands, and hybrid stays
available without being needed.

Cost: rerank-3 is billed on query tokens times documents plus document
tokens, about 13,600 tokens per query here, or $0.0007 at $0.05 per million
(inside the 200M free tokens). A research run makes 5 to 8 worker queries,
so under a cent. Latency: about 0.2 s more per query (p50 0.18 s to 0.40 s),
against worker calls that take about 10 s each.

## 2. Contextual retrieval: helped keyword search, not vector search

Anthropic reported that contextual embeddings plus contextual BM25 cut
retrieval failures by about half on their corpora. Here, with the prompt from
their write-up and Haiku 5.5 writing a preamble for each of the 251 chunks
(the document cached, 1.4M cached input tokens read, about $0.05 and 28 s
for the index):

* **Vector search did not improve.** 3 queries moved up, 5 moved down; MRR
  fell from 0.80 to 0.75. The likely reason, not separately tested: every
  chunk here is already embedded with its heading path
  (`DEPLOY.md › 4. Caddy`), which carries most of what a preamble would say,
  and the preamble pulls each vector toward a summary of the whole document.
* **Keyword and hybrid search did improve**, as the recipe predicts for
  BM25-style search: keyword MRR 0.41 to 0.46, hybrid 0.62 to 0.67 (13
  queries up, 3 down). A preamble supplies words a chunk lacks.
* **On top of the reranker it added two queries' worth** (MRR 0.90 to 0.91,
  two queries up one rank, none down). Not enough to pay for a model call per
  chunk on every upload.

It stays available (`RAG__CONTEXTUAL=true`) for corpora whose chunks do not
carry their context: PDFs without headings, chat logs, tickets.

## 3. Embedding models: no gain

* **voyage-4-large**, at twice the price, matched voyage-4 (MRR 0.80 both,
  recall@8 0.93 both).
* **voyage-context-4**, which embeds each chunk with the rest of its
  document, did slightly worse (MRR 0.75), for the same reason as the
  preambles, probably: the headings already provide the context.

Neither earns its price here. Both work (`RAG__EMBEDDINGS_MODEL`); the second
needed its own client (`rag/voyage.py`) because langchain-voyageai does not
wrap the contextualized endpoint.

## 4. Chunk size and top-k

* **Chunk size.** 250-token chunks lost MRR (0.73) without the reranker;
  800-token chunks gained a little (recall@5 0.91, 4 queries up, 1 down).
  With the reranker the three sizes are within one query of each other (MRR
  0.90 to 0.91). Bigger chunks are also more input on every web search in a
  worker call, so 400 stays.
* **Top-k.** With the reranker, recall is 0.96 at 5, 0.97 at 8 and 12, and
  0.99 at 20. Each step is about half a span across all 34 queries (one half
  of a two-part question): 8 to 5 would lose that much, 8 to 20 would gain
  it. Each passage is about 400 input tokens on a Haiku call, so cost does
  not decide either way. 8 stays. The answer-level side of this ablation
  (briefing quality at 5, 8, 12 and 20 passages) was not run; at about $1 per
  setting it is the next thing to try if a corpus case shows missing facts.

## 5. What the briefings did with it

Turning the reranker on changes which passages each research worker sees, so
the five corpus cases of the research eval ran live again (3 trials, $3.04;
the web cases replayed). Corpus cases only, scorecards `1e4e092a685f` (before)
and `279fd17627bc` (reranked):

| | before | reranked |
|---|---|---|
| cites a document | 15 / 15 | 15 / 15 |
| `grounded` | 3.87 | 3.67 |
| `useful` | 3.87 | 3.87 |
| `calibrated` | 3.13 | 3.00 |
| `covers` | 3.60 | 3.60 |
| findings with no citation attached | 7 of 108 | 14 of 109 |

The better retrieval did not show up in the briefings, and `grounded` fell.
The judge's reasons for every corpus briefing scored 3 name the same thing:
citations to sources marked `retrieved_only`, which come from findings where
the searcher answered without attaching a citation. Those doubled. In 2 of
the 14 the searcher typed markers like `[3-1]` into its prose; in the rest it
answered from the passages with no citation at all. Over all 13 cases the
gate passed (`grounded` 3.75 to 3.67, `claims_cited` 0.92 to 0.94).

What this does and does not show. 15 runs is small: 0.2 in `grounded` is three
briefings scored 3 instead of 4, and uncited findings went from 6% to 13%.
It does not show that better passages make worse briefings. It does show that
the briefings are not limited by retrieval any more: on the gold set the
right passage was already in the top 8 for most queries (recall@8 0.93 before
reranking), and what decides `grounded` is whether Haiku attaches citations
to what it read. That is the next thing to fix, and the retrieval eval will
not see it.

## How to reproduce

```bash
uv run python -m evals retrieval                       # the default: reranked
RAG__RERANK=false uv run python -m evals retrieval     # the old baseline
RAG__CONTEXTUAL=true uv run python -m evals retrieval  # ~$0.05 of Haiku
RAG__EMBEDDINGS_MODEL=voyageai:voyage-context-4 uv run python -m evals retrieval --mode vector
RAG__CHUNK_TOKENS=800 RAG__RERANK=false uv run python -m evals retrieval
```

Each run reuses a stored index when its fingerprint matches, so after the
first build a run costs only query embeddings and rerank calls. The ten
configurations above took under ten minutes and cost about $0.05 beyond
Voyage's free tokens, all of it the contextual preambles.
