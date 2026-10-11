# Roadmap

The platform layer is solid: durable runs, exact SSE resume, interrupts, time
travel, tenant isolation, atomic limits. The agents on top of it are thin. Step 2
gave research structured output and cited web search; step 3 added evals and a
model per agent and role, so every later change is measured. This roadmap is
ordered to fix the agents first, then deepen the platform.

Each step lists what it is, why it comes where it does, and how we know it is
done.

## Done

### 1. Correctness and identity (2026-10-05)

* **Cross-tenant node cache.** A LangGraph cache key hashes the node's input
  only, never the runtime context. `investigate` answers were shared across
  tenants; `recall_memories` could be under caller-supplied message ids.
  `WorkerInput` now carries `tenant_id`; `recall_memories` is uncached.
  ARCHITECTURE.md §9.
* **Concurrency slot leak.** A failure between taking the slot and creating
  the run task held the slot until its lease expired. `RunManager._abandon`
  releases it.
* **One source of identity.** Tools read `AgentContext` via `get_runtime`;
  identity left `configurable`; `memory_namespace()` is defined once.
* **Embeddings fail fast.** `build_embeddings` validates provider, package,
  key and dimensions at boot instead of at the first `remember`. CONFIGURATION.md.
* **Streaming duplicates.** The UI no longer renders the last reply twice, and
  research streams only the briefing.

Tests: `test_isolation.py`, `test_embeddings.py`, `test_streaming.py`.

### 2. Structured output and grounded research (2026-10-05)

* **Structured output.** Planner returns `ResearchPlan` (question + why), critic
  returns `Critique` (per-finding confidence and issue, plus follow-ups). The
  line-splitting planner and the `"unsure" in answer` heuristic are gone.
* **Web search with citations.** Workers use Anthropic's server-side web search
  (`RESEARCH__WEB_SEARCH`, on by default). Sources are read from citation
  metadata, not prose (`agents/citations.py`). Tavily was not needed.
* **Bounded critic loop.** `critique` (deferred) scores every finding and sends
  short follow-up questions back to `investigate`, at most
  `RESEARCH__MAX_ROUNDS` waves; duplicates are dropped before they run.
* **Cited briefing.** Findings are numbered globally; the writer cites `[n]`
  and the source list is appended from metadata.
* **Hermetic tests.** `ScriptedChatModel.with_structured_output` plus
  citation-shaped worker replies; the critic is steerable per test.

Deviations from the plan, on purpose:
* Confidence is scored by the critic, which sees every finding and its sources,
  not self-reported by each worker.
* An uncited finding is capped at 0.3 rather than rejected, so a thin topic
  still produces a briefing that says how thin it is.
* Two string checks remain, neither on model prose: the approval resume value
  accepts `"yes"`/`"no"` strings, and a provider `BadRequestError` is matched by
  class name so the fake model never imports the Anthropic SDK.

Verified against the live API (claude-sonnet-5): two-question plan, one search
each, critic raised a contradiction between findings, round 2 resolved part of
it, briefing flagged the rest. About 70 seconds.

Tests: `test_research.py`.

### 3. Evals, with a model per agent and role (2026-10-07)

**Why third.** Step 2 changed behaviour a lot; evals are how we know it got
better and how every later prompt or model change is judged. The hermetic tests
prove the plumbing works, evals prove the agent does. Doing evals before step 2
would mostly measure a design we are about to replace.

Evaluation only pays off if it can answer "which model should each part of
each agent use". Today one `MODEL` serves every call: the research planner,
searcher, critic and writer, and chat. They differ in what they need (the
searcher needs web search, the critic needs judgement, the writer needs
prose) and in what they cost. So this step makes the model a per-agent,
per-role setting first, then builds evals that can compare configurations.

**Done when.**
* One command produces a scorecard per agent: programmatic checks, judge
  scores, tokens and latency, each attributed to the model that produced it.
* A deliberate prompt regression (the writer prompt with the `[n]` citation
  instruction removed) fails the baseline gate with a visible score drop.
* Two model configurations for the same agent can be compared head to head
  from one command.
* The judge agrees with a hand-labelled calibration set at or above a stated
  threshold, and that number is printed with every scorecard.
* The harness itself runs in the hermetic suite under `FAKE_MODEL`.

#### 3a. Model routing per agent, role and run

* **Roles are declared.** `AgentSpec.model_roles` names each model call site:
  chat `("assistant",)`; research `("planner", "searcher", "critic",
  "writer")`. Graph nodes ask for a model by role, never by name.
* **One resolver.** `models.resolve_models(settings, agent, overrides)`
  returns the full role-to-model map: run override, else role setting, else
  agent default, else `MODEL`. `build_model` stays the only place that names
  a provider.
* **Config.** `MODELS__<AGENT>__DEFAULT` and `MODELS__<AGENT>__<ROLE>`, e.g.
  `MODELS__RESEARCH__CRITIC=claude-opus-...`. Unset means today's behaviour,
  so this lands with no config change required.
* **Per run.** `POST /threads/<id>/runs` accepts `models: {role: name}`
  (`"default"` for every role of the agent). Names must be in
  `MODELS_ALLOWED`, which defaults to the models already named in config, so
  a caller can choose between approved models but not introduce an expensive
  one. Unknown role or disallowed model is a 400.
* **Resolved once, carried in context.** The run resolves the map at start
  and puts it in `AgentContext.models`; nodes read it there. Models are built
  lazily and cached by name, so a per-run choice costs one construction, not
  one per call. This replaces the unused `AgentContext.model` field.
* **Node cache stays correct.** A node cache key hashes input only, so a
  cached `investigate` answer from one model would be served to a run that
  asked for another. `WorkerInput` carries the searcher model, as it already
  carries `tenant_id`. `test_isolation.py` covers it.
* **Fail fast.** Boot rejects an unknown agent or role name in `MODELS__*`.
* **Visible.** The run record and the research metadata carry the resolved
  map, so a briefing and a scorecard row say which models made it.
* **Not in this step.** `max_tokens` stays global; per-tenant model routing
  (a tenant's default map stored with its key) waits for a tenant settings
  table.

#### 3b. Harness and programmatic checks

* **Layout.** `evals/` at the repo root, outside `src/waypoint`:
  `datasets/<agent>.jsonl`, `rubrics/<agent>.md`, `calibration/`,
  `baselines/<agent>.json`, `results/` (git-ignored if the repo becomes one).
* **Cases.** Each line: `id`, `input`, `checks` (programmatic expectations),
  `rubric` criteria to apply, optional `seed` (memories to pre-load),
  `tags`, and `reviewed` (date a human last confirmed it). Research topics are
  chosen so their core facts do not drift month to month.
* **Starting datasets.** Research, about 8 topics: one needing recent web
  facts, one contested, one narrow and technical, one with an explicit
  constraint in the topic ("since 2025", "EU only"), one with little
  available evidence (tests calibration), one declined at approval. Chat,
  about 12 cases: tool use when needed and not otherwise, `remember` on a
  stated fact, recall of a seeded memory, a multi-turn case.
* **Driving graphs.** In process, not over HTTP: each case builds the graph
  with an in-memory checkpointer and store and runs it with an
  `AgentContext` for an eval tenant, whose `models` map is the configuration
  under test. Interrupts are answered by the case
  (default approve). Every model call is captured by a callback: role, model,
  input/output tokens from `usage_metadata`, latency.
* **Programmatic checks**, deterministic and cheap, run before the judge:
  * research: every `[n]` in the briefing resolves to a listed source; every
    listed source is cited at least once; each finding has a source when web
    search is on; question count and rounds within configured bounds; no
    duplicate questions across rounds; a declined case runs no search.
  * chat: expected tool called (or not called); `remember` called with the
    stated fact; a seeded memory reaches the prompt and the reply.
* **Trials.** Agents are nondeterministic, so `--trials N` runs each case N
  times and the scorecard reports mean and spread, not a single sample.

#### 3c. LLM as judge

* **Cheapest judge that agrees with a human.** `EVALS__JUDGE_MODEL` defaults
  to `claude-haiku-4-5`, the lowest-cost current Claude model. Grading one
  criterion against a written rubric is a narrow task a small model does well,
  and the judge runs on every case, criterion and trial, so its price
  multiplies. Haiku is also a different model from the agents under test,
  which avoids self-preference. The harness warns if the judge is ever the
  same model as the role it grades.
* **Escalate per criterion, only where calibration says so.**
  `EVALS__JUDGE_ESCALATION_MODEL` (default unset; e.g. a Sonnet model).
  `evals calibrate` measures the judge on the hand-labelled set; any criterion
  where it falls below `EVALS__MIN_AGREEMENT` is routed to the escalation
  model, recorded in `evals/calibration/routing.json`. Expect "grounded" to be
  the likeliest to need it. If no escalation model is set, the criterion is
  reported as untrusted rather than silently scored.
* **Rubrics as criteria, not a single score.** Each criterion is judged
  separately with reasoning before the verdict, on an anchored scale
  (pass/fail or 1-4 with written anchors), via structured output
  (`JudgeVerdict`). Research briefing criteria:
  * *grounded:* every claim is supported by the cited finding text;
  * *covers:* addresses the topic's stated scope and constraints;
  * *calibrated:* flags what the critic rated weak instead of stating it flatly;
  * *useful:* a busy reader gets the answer in the first paragraph.
  Plan criteria (judged on the planner output): questions are distinct,
  answerable, and together cover the topic. Chat: correct, uses memory when
  relevant, does not invent tool results.
* **What the judge sees.** Topic, plan, findings with their sources' cited
  text, briefing. It does not browse; "grounded" means grounded in what the
  agent retrieved, not in the world. That limit is written into the rubric
  and the scorecard.
* **Calibration.** `evals/calibration/` holds a few hand-labelled outputs per
  criterion (good, bad, borderline), including deliberately broken ones
  (uncited claim, ignored constraint). `evals calibrate` reports judge
  agreement per criterion; a judge model or rubric change must keep it at or
  above the threshold before its scores are trusted.
* **Offline.** `JudgeVerdict` gets a structured script so the judge path runs
  under `FAKE_MODEL`.

#### 3d. Scorecards, baselines, comparison

* **CLI.** `uv run python -m evals run --agent research --trials 3`,
  `--model research.critic=<name>` to override a role for one run,
  `evals compare <config A> <config B>`, `evals calibrate`.
* **Scorecard.** JSON plus a short Markdown summary per agent: per-check pass
  rate, per-criterion judge score, tokens and latency per role, judge
  agreement. Identified by date and a config fingerprint (hash of prompts,
  resolved model per role, research settings), because the repo is not under
  git; the git SHA is added when it is.
* **Baseline gate.** `--against baseline` exits non-zero when a check's pass
  rate or a criterion's mean drops by more than its tolerance (set from the
  observed spread across trials, so noise does not fail the gate). Accepting
  a new baseline is an explicit command.
* **Pairwise compare.** For model or prompt choices, the judge sees both
  configurations' outputs for the same case in random order and picks one per
  criterion; reports win rate alongside the cost difference. This is how a
  per-role model choice gets made.
* **Docs.** CONFIGURATION.md (`MODELS__*`, `EVALS__*`), ARCHITECTURE.md
  (model routing, and why the judge is a separate model), TESTING.md (live
  evals versus hermetic tests).

Deliberately not in this step: a nightly scheduled run (waits until the repo
is under git, so results can be tied to a commit), evaluating production runs
online (needs the traces from step 4), cost budgets (step 4), and judging
against live web truth.

**What the first live runs found** (claude-sonnet-5 under test, judge
claude-haiku-4-5, escalation claude-sonnet-4-5):

* **A real agent bug.** With forced-tool-call structured output, the planner
  and critic sometimes got malformed input from the model (a list sent as a
  JSON string, a stray `<parameter>` tag). The run failed outright, because
  LangGraph's default retry policy never retries a `ValueError` and a
  pydantic `ValidationError` is one. Fixed twice over: `STRUCTURED_OUTPUT`
  now defaults to `json_schema` (constrained decoding), and planner and
  critic retry on parse and validation errors. After the fix every research
  case completed.
* **The gate works.** Baseline on three research cases, then the writer prompt
  with the citation instruction removed: `sources_cited` dropped from 1.00 to
  0.00 and `--against-baseline` exited 1. The judge's *grounded* score did not
  move reliably (one uncited briefing still got a 4 despite the rubric capping
  it at 2): the programmatic check is what caught it, which is why both exist.
* **Chat.** 12/12 cases pass every check; judge mean 3.9-4.0 on all criteria.
* **Calibration.** Haiku agrees with the labels on every chat criterion and on
  research *plan_quality*: those stay on Haiku. *grounded* escalates to
  Sonnet. *covers*, *calibrated* and *useful* are untrusted: Haiku and Sonnet
  agree with each other and against three borderline labels (`covers-thin`,
  `calibrated-vague`, `useful-late`). With four examples per criterion one
  disagreement is 25 points. Next: review those labels and grow the set to
  about ten per criterion.
* **Cost.** On claude-sonnet-5 the searcher was about 84% of a run: 4.6M
  input tokens and 372 searches per 8-case, 3-trial run, about $18.50 in all,
  and five such runs came to most of $100 (2026-10-08). Cut by moving the
  searcher to claude-haiku-5-5, capping `max_uses` at 2, recording and
  replaying model calls so an experiment pays only for what it changed, and
  requiring 3 trials only to accept a baseline (TESTING.md §4, Cost).
  `prices.toml` now prices every model in use.

Deviations from the plan, on purpose:
* `sources_cited` passes at 30% of listed sources cited, not all of them: live
  briefings list 30-50 sources and good ones cite about half.
* Scheduled runs waited for git (Brent, 2026-10-07). Added once the repo was
  on GitHub: weekly by default, nightly or off through a repository variable
  (`.github/workflows/evals.yml`, TESTING.md §4).
* A tenant-level default model map is not built; per-run choice is.

Tests: `test_model_routing.py`, `test_evals.py`, and a retry test in
`test_research.py`.

### Flask to FastAPI (2026-10-08)

Done before step 4 because OpenTelemetry instrumentation is per framework.
The API contract did not change: same paths, status codes, problem+json and
SSE frames, so the web UI needed nothing. What went: `aio.py` (a daemon
thread owning the event loop, sync views hopping onto it, and a sync bridge
for SSE), `wsgi.py`, `flask[async]` and `asgiref`. What came: the runtime
built in the lifespan on the server's loop, async handlers, scopes as
dependencies, response models for every endpoint, and the OpenAPI page at
`/api/v1/docs`. Deliberate changes: a malformed query parameter is a 422, not
a 500; bodies over 8 MB are a 413 `payload-too-large`; `/healthz` no longer
reports the background loop it no longer has (ARCHITECTURE.md §1).

### Retrieval over tenant documents (RAG) (2026-10-10 to 2026-10-11)

**What.** Tenants upload documents into corpora; the research agent searches a
chosen corpus alongside the web and cites passages with the same `[n]`
machinery. Hybrid retrieval (pgvector plus Postgres full-text, fused with
RRF) in the existing database, row-level security per tenant, passages handed
to the model as `search_result` blocks so citations come back with the quoted
text. Measured by a retrieval eval (recall@k, MRR) before any answer eval.

**Why here.** It is the most-asked-for agent capability, it reuses the
citation verifier and the eval harness, and it can be done before step 4
without depending on it. Full design, decisions and phases:
[design/rag.md](design/rag.md).

**Done when.** A briefing over this repo's docs cites passages; a cross-tenant
query returns nothing; the case study reports the ablations.

**Progress.** Phase 0 (2026-10-10): `voyageai:voyage-4` at 1024 dims, in its
own `RAG__*` settings; the boot probe passes against the real provider.
Phase 1 (2026-10-10): corpora, documents and chunks with row-level security
through a `waypoint_tenant` role, Markdown/HTML/PDF parsing, structure-aware
chunking, the `ingest` agent and the `/corpora` API.
Uploading `docs/` indexes all 17 files (251 chunks, live with voyage-4 in
1.1 s), and cross-tenant reads
and writes return nothing in the store, in raw SQL and through the API. The
deviations from the design, with reasons, are in
[design/rag.md](design/rag.md#implementation-log). Phase 2 (2026-10-10):
vector, keyword and hybrid search and `python -m evals retrieval`, with a
reviewed 40-query gold set and a baseline: recall@8 / MRR 0.93 / 0.80 for
vector, 0.79 / 0.62 for hybrid, 0.59 / 0.41 for keyword. Hybrid lost, so
`RAG__MODE` defaults to `vector`. Phase 3 (2026-10-10): a research run with
a `corpus_id` gives each worker its top passages as `search_result` blocks, document and web sources
share one `[n]` numbering, the approval step can turn documents off, the UI
has a Documents picker, and five research eval cases search the docs (three
from the docs alone, two that need the web too). On the accepted 3-trial
baseline every corpus case cites a document, `grounded` holds at 3.75 (3.87 on
the corpus cases) and `claims_cited` at 0.92. Phase 4 (2026-10-10): of
contextual retrieval, voyage-4-large, voyage-context-4, a reranker, chunk
size and top-k, only the reranker earned its place (vector MRR 0.80 to 0.90,
recall@5 0.88 to 0.96, $0.0007 and 0.2 s per query) and is on by default.
At the answer level the bottleneck is now the searcher attaching citations,
not retrieval ([case study](case-studies/retrieval.md)). Fixed the same day:
a worker whose reply cites nothing is asked once more, which took uncited
findings from 20 of 277 to 1 of 281 and `grounded` from 3.67 to 3.83.
Phase 5 deployed 2026-10-11: the public `waypoint-docs` corpus is seeded from
`docs/` at boot, reviewer links can research it, and only the admin key can
create or upload to corpora. The RAG design is complete.

## Next

### 4. Observability and cost

**What.** OpenTelemetry spans for HTTP request, run, node and model call,
with tenant and run ids as attributes; optional LangSmith or Langfuse export.
Capture `usage_metadata` per run into `run_audit` (input/output tokens, model).
A per-tenant token budget in the existing Lua limiter, alongside RPM. Cost per
run in the UI.

**Why here.** With real tools and a critic loop, a run makes many more model
calls and can loop. Before opening it to more tenants we need to see where time
and tokens go and cap them per tenant.

**Done when.** A single run can be followed end to end in a trace; a tenant
over its token budget gets a clean 429.

**Design.** [design/observability.md](design/observability.md), written
2026-10-11. It changes one thing here: budgets are in US dollars, not
tokens, because a Sonnet token costs 20 times a Haiku token. Phases 1 and 2
(cost per run, budgets) come before tracing, since the public demo has no
spending cap today.

**Progress.** Phase 0 done 2026-10-11: budgets set to $0.35 per tenant and
$0.70 overall per day (Brent's $20 a month), and `prices.toml` and `call_cost`
moved into `src/waypoint/usage.py` with a parity test. Phase 1 built
2026-10-11: every run reports its cost per role on the run record, the
terminal event, `run_audit` and a line in the UI, and `GET /usage` sums a
tenant's spend. Its live check on Bob waits for a deploy; budgets (phase 2)
next.

### 5. MCP tools and tool-level approval

**What.** Load tools from MCP servers listed in config via
`langchain-mcp-adapters`, filtered by API-key scopes. A `requires_approval`
flag on tools and a pre-tool node that `interrupt()`s before such a call runs.
A demo agent over existing MCP servers (match data, Sentry).

**Why here.** It generalises the research agent's one-off approval node into a
platform feature and makes adding capability a config change rather than code.
It comes after observability because external tools are where cost and
failures get unpredictable.

**Done when.** Adding an MCP server to config gives an agent its tools; a
sensitive tool call pauses for approval and survives a reload like the plan
approval does.

### 6. Durable run queue

**What.** Move run execution from "the worker that accepted the POST" to a
Redis Streams consumer group. Workers claim runs; `XAUTOCLAIM` reassigns runs
from a dead worker, which resumes from the last checkpoint. Make `durability`
a real per-run choice.

**Why last.** It is the biggest infrastructure change and only matters once
runs are long and expensive enough that losing one to a deploy hurts, which
steps 2 and 5 make true. ARCHITECTURE.md "What is deliberately missing" already
names it.

**Done when.** Killing a worker mid-run leaves the run to finish on another
worker without client-visible loss.

## Smaller open items

* **Context window management.** Chat threads grow without bound. Add
  `trim_messages` or a summarisation node that keeps a running summary in state.
* **Memory consolidation.** `remember` appends; facts accumulate and
  contradict. Extract-then-insert/update/delete after each turn, and treat
  memory writes as untrusted input.
* **Generated frontend types.** `web/src/api/types.ts` mirrors the Python
  schemas by hand. Generate OpenAPI from Pydantic and run `openapi-typescript`
  in CI.
* **Prompt caching.** `cache_control` on system prompts and tool definitions.
* **Optimistic user message.** The UI shows the user's message only after the
  run settles and the thread reloads.
* ~~**Render markdown in the transcript.**~~ Done 2026-10-09: assistant
  messages render as GitHub-flavoured Markdown (react-markdown, raw HTML
  shown as text), with single newlines kept for the briefing's source list.
* **Research critic loop cost.** Follow-ups re-search from scratch; passing the
  critic's issue into the follow-up worker's prompt would make round 2 sharper.
