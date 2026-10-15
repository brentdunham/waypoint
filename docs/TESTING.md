# Testing

Three suites, three costs, plus evals (section 4), which measure the agents
rather than the code. Run them in this order; each one only exists
because the cheaper one cannot catch what it catches.

## 1. Unit tier — no infrastructure, no network, ~2 seconds

```bash
uv run pytest
```

The app boots with the in-memory checkpointer, in-memory store,
single-process event bus and in-memory run queue (the API process drives its
own runs, through the same worker code as production), and `FAKE_MODEL=true` swaps the LLM for
`ScriptedChatModel`. That last part is the whole trick: **the graphs are
testable because the model is deterministic**. No API key, no cost, no flake,
and the assertions can be exact rather than "contains something plausible".

The scripted model shapes its reply to the prompt, so the planner still returns
several questions and the worker still returns an answer — the real code paths
run, only the intelligence is fake. If a prompt contains
`[[tool:name({"arg":1})]]` it emits that tool call, which is how tool-loop
tests force a specific branch.

Covers: routing and validation, problem+json shapes, the interrupt/resume
cycle, fan-out and the `merge_findings` reducer, checkpoint history and
forking, memory round-trips, serialization, the citation verifier, and stream
resilience.

**MCP tools** (`tests/test_mcp.py`) run against an in-memory FastMCP server:
the catalog's `connect` hook hands it a client for the server object instead
of an HTTP URL, so discovery, scopes, the approval policy, the call cap,
timeouts, server errors and the tenant in `_meta` are tested with no network.
Tool approval is tested there too: a write pauses with nothing run, an
approval runs it, a decline never reaches the server, calls are decided one
by one, and an untrusted server's read-only tool still asks. The integration
tier adds that a pause survives a restart: a second app object resumes it from
Postgres. The HTTP transport itself was checked by hand against a local
FastMCP server (design log, phase 3).

The demo notes server's service parts are tested there too: its SQLite store
across a restart, and the shared-token check.

**Eval cases with tools** (`notes` in a workspace case) run the demo notes
server (`waypoint.mcp_demo.notes`) in process for that case alone. Its write
tools pause like any other, and the case's `resume` list answers them in
order: `{"approved": true}` allows every pending call, `{"approved": false}`
declines them, `{"tools": {"<call id>": true}}` decides one by one.

## 2. Integration tier — real Postgres and Redis

```bash
docker compose -f compose.dev.yaml up -d

export TEST_DATABASE_URL=postgresql://waypoint:waypoint@localhost:5432/waypoint_test
export TEST_REDIS_URL=redis://localhost:6379/15     # scratch index — it gets FLUSHDB'd
createdb -h localhost -U waypoint waypoint_test         # once

uv run pytest -m integration
```

Skipped automatically when those two variables are unset, so the default run
stays hermetic.

**Not on Windows.** The test client runs the app on a Proactor event loop,
which psycopg refuses, so every Postgres test fails there with
`PoolTimeout` after 30 s per test. Run this tier on Linux or macOS (CI, WSL,
or a container). Postgres needs pgvector 0.8 or later: the vector search sets
`hnsw.iterative_scan`, which 0.6 rejects. Found 2026-10-14, when the tier had
been skipped on the Windows dev machine without anyone noticing.

These exist for the things that are *only* true with real infrastructure:

* a thread paused on an interrupt is resumed by a **different app object**,
  proving the pause lives in Postgres and not in the process;
* an SSE stream replays identically from `Last-Event-ID`, and starting after
  event *n* drops exactly *n* events;
* the Lua token bucket and the concurrency semaphore actually refuse work;
* a reviewer key's corpus cap and daily upload limits hold in Redis: the
  extra file gets `429 over-quota` with `Retry-After`, and a document over
  the chunk limit fails alone (`test_rag_pg.py`; the unit tier covers the
  same rules on the in-process counters in `test_uploads.py`);
* API keys resolve, scope, revoke, and isolate tenants;
* `readyz` reports both backends as `ok`;
* the API only queues runs and workers drive them (`test_queue.py`): with no
  worker a run waits and `readyz` says `workers: unavailable`; two workers
  with `QUEUE__CONCURRENCY=1` and a slowed scripted model both take runs; a
  run cancelled while queued ends `cancelled` without its graph running, and
  its slot comes back.

With Redis the API process drives no runs, so every integration app gets a
worker: `TestApp` starts one worker runtime (`start_runtime(role="worker")`)
on the app's loop, with its own pools, as the separate process would have.
`make_app(workers=0)` starts none, `workers=2` two, and `app.start_worker()`
adds one mid-test. Tests that poke a runtime the worker uses, such as the MCP
catalog, reach it through `app.workers`.

A `clean_redis` autouse fixture flushes the scratch index before every test.
Without it a burst test poisons the next test's rate-limit budget — which is
exactly the kind of failure that only appears in CI, at 40% of runs.

Everything together:

```bash
TEST_DATABASE_URL=… TEST_REDIS_URL=… uv run pytest
```

## 3. Frontend

```bash
cd web
npm run build      # tsc --strict (noUncheckedIndexedAccess, exactOptionalPropertyTypes) then vite build
```

The types in `web/src/api/types.ts` mirror `waypoint/schemas.py` and
`waypoint/serialization.py` by hand. That is deliberate — it is the contract, and
you want a compile error when the backend shape moves, not a blank pane. When
you change a response shape, change both.

## 4. Evals — real model, real cost, scores not assertions

The tiers above prove the plumbing works. Evals measure whether the agents do
their job, and which models should play which role. They call the real API
and cost money; they are not part of `pytest`. The harness itself is tested
offline in `tests/test_evals.py`.

```bash
uv run python -m evals run --agent research                 # 1 trial, replayed where it can be
uv run python -m evals run --agent research --trials 3      # a scorecard you can accept
uv run python -m evals run --agent research --fresh         # resample every model call
uv run python -m evals run --agent all --no-judge           # programmatic checks only
uv run python -m evals run --agent chat --case arithmetic   # one case
uv run python -m evals run --agent research --model critic=claude-opus-4-5
uv run python -m evals compare --agent research --b critic=claude-opus-4-5   # A = config
uv run python -m evals calibrate --agent all                # measure the judge
uv run python -m evals accept --agent research evals/results/research/<run>/scorecard.json
uv run python -m evals run --agent research --against-baseline   # exit 1 on regression
uv run python -m evals run --fake --agent all               # harness only, no API calls
```

How it fits together (`evals/`):

* **Datasets** — `datasets/<agent>.jsonl`, one case per line: input, interrupt
  answers, seeded memories, programmatic checks, rubric criteria, what a good
  answer covers, and the date someone last reviewed it. A research case with
  `"corpus": "waypoint-docs"` also searches that corpus, and a workspace case
  with it selects that corpus for the thread (`datasets/workspace.jsonl`: seven
  document cases, including a follow-up turn, one the docs do not cover and one
  that needs no documents, plus three memory and tool cases): the driver indexes
  the pinned corpus (see Retrieval eval) in memory once per run, so it needs
  `RAG__EMBEDDINGS_MODEL`, `VOYAGE_API_KEY` and `RAG__MODE=vector`, but no
  database. Under `--fake` it uses hash vectors. Document ids in that index
  are derived from the names, so the `doc://` sources in worker prompts, and
  with them the recorded calls, are the same in every process.
* **Driver** — runs each case in process on the same `build()` the service
  registers, with its own in-memory store, and records every model call with
  role, model, tokens, web searches and latency.
* **Checks** — deterministic, run first: `[n]` citations resolve, sources are
  used, every finding has sources (`findings_sourced`) and they came from
  citations rather than the search-results fallback (`findings_cited`), at
  least 80% of the briefing's claim-length sentences carry an `[n]`, not
  counting uncited statements about the research itself (`claims_cited`), a corpus case cites a document (`cites_documents`) or a
  web page as well (`cites_web`, for cases that need both),
  rounds and plan size stay in bounds, a declined plan searches nothing;
  for chat, the right tool was (or was not) called, facts were remembered and
  seeded memories recalled.
* **Judge** — `rubrics/<agent>.toml`, one call per criterion, reasoning before
  a 1-4 score with written anchors, 3+ passes. It sees the findings the
  briefing was written from, each source with its `[n]` and the text the
  search API says the answer quoted from it (`cited_text`, up to three
  excerpts). It cannot browse: *grounded* means grounded in what the agent
  retrieved, and it judges support and attribution, not source quality.
* **Calibration** — `calibration/<agent>.jsonl` holds hand-labelled outputs.
  `calibrate` measures judge agreement per criterion, routes failing criteria
  to `EVALS__JUDGE_ESCALATION_MODEL` if it does better, and otherwise marks
  them untrusted (`calibration/routing.json`). Calibration is tied to the
  judge model and rubric it was measured with; change either and rerun it.
  Review the labels: they are the ground truth for the judge.
* **Scorecards** — `results/<agent>/<time>-<fingerprint>/`: `scorecard.json`,
  `summary.md`, `transcripts.jsonl`. The fingerprint hashes prompts, models per
  role, agent settings (for research, the `RAG__*` settings a run reads too),
  judge, rubric and dataset, so two scorecards with the
  same fingerprint measured the same thing. Settings only the server reads at
  boot (`RAG__SEED_DIR`, `RAG__SEED_CORPUS`, the upload cap) are left out.
  Cost uses `src/waypoint/prices.toml` through `waypoint.usage`; a model not
  listed there shows tokens only. "Cost per run" is what the run was billed,
  which replay makes small; "cost per run if every call ran live" adds the
  replayed calls at the usage recorded with them, and is the figure to compare
  configurations by. Calls recorded before usage was stored with them
  (2026-10-12) recall it only if their answer is a message, so for older
  stores that figure is marked as a floor.
* **Baseline gate** — `baselines/<agent>.json`. A metric fails when it drops
  by more than twice the baseline's spread across trials, and never less than
  0.10 (check pass rate) or 0.25 (criterion mean). `accept` refuses a
  scorecard with fewer than 3 trials (`--min-trials` overrides), because one
  trial has no spread. Iterate on one trial; run three only to accept.
* **Replay** — `results/replay.sqlite`, or beside `--out` if you pass one: a
  new `--out` directory starts an empty store and pays for every call. Every model call is
  stored under its case, trial, role, model, bound setup (tools, `max_uses`,
  output schema, `max_tokens`) and input messages, with today's date masked.
  A rerun answers stored calls from the file and pays only for calls whose
  key changed, and for everything downstream of them. See below.
* **Compare** — runs two model configurations on the same cases and asks the
  judge which output is better per criterion, in shuffled order. Reports win
  rates next to cost and check pass rates.

### Cost, and what replay saves

On 2026-10-08 one research run of 8 cases x 3 trials, every role on
claude-sonnet-5, cost about $18.50, and the searcher was about $15.60 of it:
4.6M input tokens, because each web search's results are input again for the
next search in the same call, plus 372 searches at $0.01. Five such runs were
most of a $100 bill. Three changes bring it down:

* The searcher runs on `claude-haiku-5-5` (`MODELS__RESEARCH__SEARCHER`), at
  $0.10 / $0.50 per million tokens against $2 / $10.
* `RESEARCH__MAX_SEARCHES=2`: calls that made a third search averaged 48k
  input tokens against 21k for two.
* Replay. A writer-prompt change replays the planner, every search and the
  critic, and pays for the writer alone, about $0.90 for 8 cases x 3 trials.
  A critic change replays the planner and the first wave of searches, then
  runs the critic, its new follow-up searches and the writer live.

What a key covers is exactly what reaches the API, so a prompt, model,
`max_uses` or schema change can never be answered from a stale call. What it
cannot see is the outside world: replayed searches are the web as it was when
recorded. `--fresh` resamples everything and records the new answers; use it
when the question is "has something changed out there", and before accepting
a baseline you mean to keep for a while. Replayed calls are not billed and
not in the usage table; the summary lists how many there were per role.
Delete `replay.sqlite` to start over.

`compare` shares the store too: when A and B differ only in the writer, both
sides replay the same planner, searches and critic.

Checks read only the saved transcript, so a corrected check costs nothing to
apply to old runs: `uv run python -m evals recheck <results dir> [...]`
re-runs them with each run's own research settings and rewrites
`scorecard.json` and `summary.md`, keeping grades and usage. Recheck a
baseline's run before re-accepting it.

### Scheduled runs

`.github/workflows/evals.yml` runs `evals run --agent all --trials 1
--against-baseline --no-replay` on `main`, weekly by default, against live
models. One trial should cost roughly $2-3 (an estimate from the Sonnet token
counts; measure it on the first run). It never replays: its job is to notice the
live models and the live web moving. The
workflow wakes daily and a small first job decides whether today is a run
day, so the cadence is a repository variable, not a code change:

| Repository variable | Default | |
|---|---|---|
| `EVALS_CADENCE` | `weekly` | `weekly`, `nightly` or `off` |
| `EVALS_WEEKDAY` | `1` | Run day when weekly, Monday = 1 |
| `EVALS_LANGSMITH_TRACING` | `false` | `true` sends traces to the `waypoint-evals` project |

Secrets: `ANTHROPIC_API_KEY`, and `OPENAI_API_KEY` while `EMBEDDINGS_MODEL`
is `openai:*` (chat memory recall), and `VOYAGE_API_KEY` for the research
cases that search a corpus. Those cases also need `RAG__EMBEDDINGS_MODEL` in
the workflow's `env` and a full clone (`fetch-depth: 0`), because the corpus
is read at a pinned commit. `LANGSMITH_API_KEY` only with tracing on.
Set them under Settings > Secrets and variables > Actions; nothing comes from
`.env`, which is not in the repo.

The run fails, and GitHub emails it, when a metric drops past the baseline's
tolerance. Each run's summary appears on the run page and the scorecards and
transcripts are kept for 90 days as an artifact. Scorecards carry the commit
SHA. **Run workflow** on the Actions tab starts one by hand, for one agent or
all, with a chosen trial count; 3 trials gives a scorecard `accept` takes.

The workflow's `env` block repeats the non-secret configuration the
baselines were accepted with (`MODEL`, `MODELS__RESEARCH__SEARCHER`,
`RESEARCH__MAX_SEARCHES`, `MAX_TOKENS`, `EVALS__*`). If the two
drift, the summary says the configuration differs from the baseline: either
bring the workflow in line or accept a new baseline.

A failure on a commit that changed nothing usually means the outside world
moved: web search results, a model alias pointing at a new snapshot, the
provider's tool behaviour, or a dataset case whose "recent" facts went stale.
Read the transcripts before deciding between a fix and a new baseline. Pinning
dated model snapshots removes the alias cause.

### Retrieval eval

Measures corpus search on its own, before any model writes an answer from it
(design: [design/rag.md](design/rag.md), decision R7). No LLM at query time,
so a run costs only the query embeddings once the index exists.

```bash
# once: a database of its own, never the service's
docker compose exec postgres createdb -U waypoint waypoint_evals
# in .env: EVALS__DATABASE_URL=postgresql://waypoint:waypoint@localhost:5432/waypoint_evals

uv run python -m evals retrieval --check            # every gold span findable? no DB, no cost
uv run python -m evals retrieval                     # vector, keyword and hybrid
uv run python -m evals retrieval --mode hybrid --case q17-payload-too-large
uv run python -m evals retrieval --fake --dataset evals/datasets/retrieval.proposed.jsonl
uv run python -m evals accept --agent retrieval evals/results/retrieval/<run>/scorecard.json
uv run python -m evals retrieval --against-baseline  # exit 1 on a drop > 0.05

# ablations: any RAG__ setting, for one run (PowerShell: $env:RAG__RERANK="false")
RAG__RERANK=false uv run python -m evals retrieval --mode vector
RAG__CHUNK_TOKENS=800 uv run python -m evals retrieval
RAG__CONTEXTUAL=true uv run python -m evals retrieval   # ~$0.05 of Haiku preambles
RAG__EMBEDDINGS_MODEL=voyageai:voyage-context-4 uv run python -m evals retrieval --mode vector
```

* **Corpus** — `corpora/waypoint-docs.toml` names `docs/**/*.md` at a pinned
  commit, read with `git cat-file`, so the gold set's spans stay true while
  the docs change. Moving the pin means `--check` and a new baseline.
* **Gold set** — `datasets/retrieval.jsonl`: per query a `kind`
  (`paraphrase`, `identifier`, `multi-hop`, `no-answer`) and the exact
  `span`s of text that answer it. A retrieved chunk counts when it comes from
  the span's document and contains the span, whitespace-normalised, so labels
  survive any change of chunking. A span may list `alternatives`, other
  passages that say the same thing: any one satisfies it. Separate spans are
  all needed (multi-hop). Claude drafts into
  `retrieval.proposed.jsonl`; a person reviews it and renames it. Scorecards
  from the proposed file are marked unreviewed and `accept` refuses them.
  `tests/test_retrieval.py` fails when any span stops being findable.
* **Index** — built by `rag/index.py`, the ingest agent's own code, under
  tenant `evals`, in a corpus named after a fingerprint of the files, the
  embedding model and width, the chunk size, the preamble model when
  `RAG__CONTEXTUAL` is on, and the `rag/` indexing source.
  A rerun with the same fingerprint reuses it; anything else builds a new one
  (about 60k tokens for `docs/`, $0.004 at voyage-4's price, inside the free
  tier; contextual preambles add about $0.05 and 30 s).
* **Metrics** — per mode: recall@5, @8, @12, @20 (and @`RAG__TOP_K`) and MRR
  over answerable queries, the same by kind, p50/p95 latency (query embedding,
  SQL and reranking), billed rerank tokens per query, and for no-answer
  queries the best vector score next to the answerable ones', to set a
  "nothing relevant" threshold from data. Reranking applies to every mode
  when `RAG__RERANK` is on (the default).
* **Gate** — retrieval is deterministic for a fixed index, so there is no
  trial spread: a drop of more than 0.05 in recall@k or MRR fails, and one
  run is enough to accept.
* **`--fake`** — hash vectors and the in-memory store's word-count search:
  it tests the harness, and its numbers mean nothing.

## Manual end-to-end

```bash
docker compose up --build
open http://localhost:8080
```

Worth doing by hand at least once, because it is the only way to check the
parts that are about *behaviour over time*:

1. Pick **Research**, send a topic, watch `node` chips appear as the plan node
   runs. The run stops with an approval card.
2. **Reload the page.** The card is still there — it was read back from
   Postgres, not held in React state.
3. Edit a question, approve, watch several workers stream at once.
4. Start another run and hit **Cancel** mid-stream. The status flips to
   `cancelled` and, in the API logs, the graph task really stops.
5. Open **Checkpoints**, pick one a few steps back, and "resume from here".
   The conversation continues down a new branch.
6. In devtools, throttle to offline for a few seconds and back. The stream
   reconnects with `Last-Event-ID` and no tokens are lost or repeated.

## Writing new tests

Helpers live in `tests/conftest.py`:

| Helper | Use |
|---|---|
| `client` | App on in-memory everything, auth off |
| `infra_client` + `auth` | App on real Postgres/Redis, auth on, limits raised |
| `make_app(**overrides)` | An app configured however you like — use this for limit tests |
| `new_thread(client, agent)` | Create a thread, assert 201, return the id |
| `run_and_wait(client, tid, body)` | Run synchronously, assert 200, return the result |
| `read_sse(client, url)` | Consume an SSE response, return the ordered event names |

Two habits worth keeping:

* Assert on **event order**, not just the final value. `["run.started", …,
  "run.completed"]` catches a stream that silently stops emitting progress.
* When testing limits, give the app its own `limits={...}` rather than tuning
  the shared fixture, and let `clean_redis` isolate it.

## Continuous integration

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    env: { POSTGRES_USER: waypoint, POSTGRES_PASSWORD: waypoint, POSTGRES_DB: waypoint_test }
    options: >-
      --health-cmd "pg_isready -U waypoint" --health-interval 5s --health-retries 20
    ports: ["5432:5432"]
  redis:
    image: redis:8-alpine
    options: >-
      --health-cmd "redis-cli ping" --health-interval 5s --health-retries 20
    ports: ["6379:6379"]

steps:
  - run: uv sync --locked
  - run: uv run ruff check src tests evals
  - run: uv run ruff format --check src tests evals
  - run: uv run mypy
  - run: uv run pytest
    env:
      TEST_DATABASE_URL: postgresql://waypoint:waypoint@localhost:5432/waypoint_test
      TEST_REDIS_URL: redis://localhost:6379/15
  - run: npm ci --prefix web && npm run build --prefix web
```
