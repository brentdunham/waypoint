# Design: observability and cost

Status: **phases 0 and 1 done (2026-10-11, live check passed on Bob); phase 2 (budgets) done 2026-10-12, checked on Bob; phase 3 (tracing) done 2026-10-12, a live research run read end to end in local Jaeger with cost on every model span; phase 4 next.** Written 2026-10-11 as roadmap item 4.
This note is the hand-off for the coding sessions: what to build, what is
decided, what is still open, and in what order. Read it with
[ARCHITECTURE.md](../ARCHITECTURE.md) and [rag.md](rag.md), which set the
format this one follows.

## Goal

Every run says **what it cost and where its time went**, and no tenant (or
the server as a whole) can spend more than its budget. The public demo made
this urgent: since 2026-10-11 any reviewer link can start research runs that
cost about $0.30 each on Brent's Anthropic and Voyage accounts, and nothing
caps the total.

Success looks like:

1. A finished run reports its cost, tokens and web searches by role
   (`GET /runs/<id>`, the `run.completed` event, and one line in the UI under
   the reply), priced from the same table the eval scorecards use.
2. A tenant over its daily budget, or any tenant once the server is over its
   global budget, gets a clean `429` with `Retry-After` when starting a run.
3. One research run can be followed end to end in a trace: HTTP request, run,
   each graph node, each model call with model, tokens and cost, and each
   retrieval, with tenant and run ids on every span. A log line carries the
   trace id, so logs and traces join.
4. A short case study (or a section in an existing one) with real numbers:
   where a research run's time and money go, by node and role.

Non-goals for v1: metrics dashboards (Prometheus, Grafana), alerting, billing
or invoices, per-user (rather than per-tenant) budgets, capturing prompt and
completion text in traces by default, mid-call cancellation when a budget runs
out.

## What exists today

The design builds on these; check them before changing anything.

* **`run_audit`** (`infra/db.py`): one row per run with ids, agent, status,
  error, `started_at`, `ended_at`. Written by `RunManager._audit_start` and
  `_audit_end` (`infra/runs.py`). No tokens, no cost.
* **`RunRecord`** (`infra/runs.py`): the run's status record, kept in Redis
  next to the event stream (`bus.set_meta`) and returned by `GET /runs/<id>`.
  Already carries `models` (role to model).
* **Role and model on every chat call.** `RoleModel.named`
  (`agents/models.py`) wraps each role's runnable with
  `with_config(metadata={"model_role": ..., "model_name": ...})`, so any
  callback handler sees which role and model a call belongs to without
  knowing node names.
* **A usage recorder that already works, in the wrong place.**
  `evals/driver.py: UsageRecorder` is an `AsyncCallbackHandler` that reads
  `usage_metadata` and the web-search count
  (`response_metadata["usage"]["server_tool_use"]["web_search_requests"]`)
  per call. `evals/scorecard.py: call_cost` prices a call, including the long
  context rates. Prices live in `evals/prices.toml`, which is **not in the API
  image** (the Dockerfile copies `src` and `docs` only).
* **Cache-aware token accounting.** `rag/contextualize.py: Usage.add` already
  splits `input_token_details` into cache reads and writes (including the
  per-TTL `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` keys) and
  notes that langchain's `input_tokens` is the total, cached parts included.
* **The limiter** (`infra/ratelimit.py`): a token bucket in Lua for requests
  per minute (`Limiter.take`, `enforce`, cost parameter already there) and a
  concurrency semaphore with a lease (`Limiter.slot`). Keys are
  `rl:{<tenant>}:...`. `RateLimited` maps to a `429` problem response.
* **Structured logs** (`log_config.py`): JSON in production, with
  `request_id` bound per request by `middleware.RequestContext`. The run task
  inherits that context, but `run_id` is never bound, so log lines from inside
  a run cannot be filtered by run.
* **Reviewers are tenants.** `deploy/reviewer-link.sh` mints each reviewer a
  key in their own tenant, `rev-<slug>`, so per-tenant budgets cap each
  reviewer, and a global budget caps all of them together.
* **Server**: gunicorn with two uvicorn workers (Dockerfile), each building
  its runtime in the app lifespan (`waypoint/__init__.py`). Bob has 4 CPUs and
  8 GB of RAM, with about 6 GB available and more demos planned.

## Decisions

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| O1 | **One usage meter, shared by the service and the evals.** Move the recorder and pricing into `src/waypoint/usage.py` and the price table to `src/waypoint/prices.toml`; `evals/` imports them. | The service needs prices at runtime and the image has no `evals/`. Two copies of the pricing rules would drift, and the scorecard's numbers are the ones already checked against real bills. | Keep evals' copy and write a second one in the service. |
| O2 | **Budgets are in US dollars, not tokens.** A tenant's budget is USD per day; usage is priced per call with the cache and long-context rates. | A Sonnet token costs 20 times a Haiku token here (`prices.toml`), and web searches cost per request. A token budget would be wrong by an order of magnitude depending on routing. | A raw token budget, as ROADMAP item 4 first said. |
| O3 | **Admit, then charge.** Starting a run checks that the tenant's and the global balance are positive; each model call's actual cost is charged when it ends. A run already going is never stopped for budget. | Cost is known only after a call. Stopping mid-run leaves a half-written briefing and a failed run, for at most one run's worth of saving. Overshoot is bounded: at most `concurrent_runs` runs per tenant past zero, each already capped by `max_rounds`, `max_searches` and `run_timeout_s`. | Reserving an estimated cost at admission (estimates are poor for research runs); cancelling runs mid-flight. |
| O4 | **A continuous bucket in the existing Lua limiter.** Capacity is the daily budget, refilling at budget/86 400 per second; the balance may go negative after a charge, and admission needs it above zero. `Retry-After` is the time until it is. Keys `rl:{<tenant>}:usd` and `rl:{global}:usd`. | Same atomic, clock-skew-safe pattern as RPM (`Limiter._now` uses Redis time), so it holds across workers. A refilling bucket has no midnight cliff where every tenant's budget resets at once. | A fixed UTC-day counter (`INCRBYFLOAT` per day key): simpler to read in a dashboard, but resets in a burst. |
| O5 | **Totals in Postgres, detail in traces.** `run_audit` gains the run's cost and token totals and a per-role breakdown (`jsonb`). Per-call rows are not stored in Postgres. | Totals answer "what did tenant X spend this week" with SQL and survive the Redis TTL. Per-call detail is what traces are for. | A `run_calls` table: thousands of rows per day for a question traces already answer. |
| O6 | **OpenTelemetry, exported over OTLP/HTTP; no vendor SDK in the request path.** The exporter endpoint is the standard `OTEL_EXPORTER_OTLP_ENDPOINT`; no endpoint means tracing off (no-op provider). | Any backend works (Jaeger, Grafana Tempo, Langfuse, Honeycomb), and switching is config. Langfuse accepts OTLP over HTTP at `/api/public/otel` (gRPC not supported), so HTTP is the protocol that reaches every candidate. | LangSmith or Langfuse SDKs as the primary path: one vendor's model of a trace, and a second client in every worker. |
| O7 | **Our own LangChain callback handler makes the graph and model spans.** One span per graph node (`on_chain_start` with `langgraph_node` in metadata) and per chat model call, parented by `parent_run_id`, with GenAI attributes (`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) plus `waypoint.*` attributes (role, cost, cache tokens, searches, tenant, run, agent). | The meter (O1) is already a callback handler; spans and usage come from the same events. We choose the attributes, so no prompt text leaves the process by accident. The GenAI conventions are still "Development" status in the OTel registry (mid-2026), so keeping the mapping in one module makes a rename one edit. | OpenLLMetry's `opentelemetry-instrumentation-langchain`: monkeypatches, and captures prompt content unless told not to. |
| O8 | **A run is its own trace, linked to the request that started it.** The run span is a new root with a span link to the `POST /runs` request span; a resume starts a new run trace linked the same way, and both carry `waypoint.thread_id`. | A run outlives its request by minutes and may be resumed hours later from another worker. Parenting it under the request would show a 50 ms request with a 4-minute child. | One trace per thread (unbounded), or runs as children of the request. |
| O9 | **No content in spans by default.** Prompts, completions, retrieved passages and document text stay out unless `TRACING__CONTENT=true`, which is for local debugging only. | Tenant documents and questions are tenant data; a trace backend is another place to secure and another processor. | Capturing content by default, as most LLM tracing tools do. |

## Decisions settled in phase 0

Brent settled these on 2026-10-11.

| # | Decision | Why |
|---|---|---|
| D1 | **Budgets: `BUDGET__TENANT_USD_PER_DAY=0.35`, `BUDGET__GLOBAL_USD_PER_DAY=0.70`**, both as the per-day bucket in O4. Brent's own tenant is exempt from both through `BUDGET__TENANTS`. Unset means off in development and is a boot error in production, like `SECRET_KEY`. | Brent's limit for the public demo is $20 a month. A bucket refills continuously, so what it caps over any 30 days is its capacity plus 30 days of refill. At $0.70 a day that is about $21. A bucket holding $20 and refilling over 30 days would allow about $40 in the same window. $0.35 is about one research run (about $0.30) per reviewer per day; $0.70 is about two across all reviewers. Exempting Brent's tenant keeps his own use from spending the reviewers' allowance. |
| D2 | **Production trace backend: decided in phase 4.** Development uses Jaeger in `compose.dev.yaml`. The lean is Jaeger v2 on Bob behind Caddy. | Phases 1 to 3 do not depend on it. Jaeger keeps trace data on the server and has no free-tier limits to watch; self-hosted Langfuse stays rejected as too heavy for Bob. |
| D3 | **A chat model missing from `prices.toml` is a boot error when budgets are on**, and a warning when they are off. | An unpriced model would spend money without being charged against the budget. |
| D4 | **Reviewers see their own spend, not the server's.** Every key sees its runs' cost and `GET /usage` (the tenant's balance and today's spend). The global balance is not exposed; a 429 for the global cap says only that the server is over budget. | A run's cost is the tenant's own data. The global balance would tell one reviewer what the others spend. |

## Data model

```sql
alter table run_audit
    add column if not exists cost_usd      numeric(12, 6),
    add column if not exists input_tokens  bigint,
    add column if not exists output_tokens bigint,
    add column if not exists searches      int,
    add column if not exists usage         jsonb,   -- per role, see below
    add column if not exists trace_id      text;
create index if not exists run_audit_tenant_idx on run_audit (tenant_id, started_at desc);
```

Idempotent, in `SCHEMA` under the migration lock like the rest. Old rows keep
nulls, which the API reports as "not recorded" rather than zero.

`usage` per role, the same shape the scorecard already prints:

```json
{"writer": {"model": "claude-sonnet-5", "calls": 1,
            "input_tokens": 41200, "output_tokens": 2100,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "searches": 0, "cost_usd": 0.1034},
 "searcher": {"...": "..."},
 "rerank": {"model": "rerank-3", "tokens": 18400, "cost_usd": 0.0009}}
```

`RunRecord` gains `usage` (the same summary plus totals) and `trace_id`, so
`GET /runs/<id>` and the `run.completed` / `run.failed` / `run.interrupted`
events carry them while the run is in Redis; afterwards `run_audit` has them.

## Components

| Module | Role |
|---|---|
| `waypoint/usage.py` (new) | `Price` and `load_prices()` (from `prices.toml`), `call_cost()` (moved from the scorecard, extended with cache read 0.1x and 5-minute write 1.25x of the input rate; 1-hour writes 2x), `Meter` (an `AsyncCallbackHandler` that accumulates per-role usage, and has `add(role, model, tokens=..., usd=...)` for non-chat costs), and `METER: ContextVar[Meter | None]` so code outside LangChain (the reranker, the indexer) can report into the current run. |
| `waypoint/prices.toml` (moved) | The table from `evals/prices.toml`, unchanged in content; package data. |
| `infra/runs.py` | `_drive` creates a `Meter`, puts it in `config["callbacks"]` and in `METER`, binds `run_id` (and `trace_id`) into the log context, and on every exit path writes the summary to the record, the terminal event and `run_audit`. Charges the budget per call through the meter's hook. |
| `infra/ratelimit.py` | `Limiter.admit(tenant)` and `Limiter.charge(tenant, usd)`: two Lua scripts over the bucket in O4, each touching the tenant and global keys in one call. `RateLimited` gains a budget variant with `Retry-After`. |
| `api/runs.py` | Calls `admit` before `RunManager.start` (after the RPM check, before the slot), so an over-budget caller gets a 429 from the POST. Same for ingest uploads in `api/corpora.py`. |
| `api/usage.py` (new) | `GET /usage`: the tenant's balance, budget and spend over a window from `run_audit`. Scope `threads:read`. |
| `waypoint/tracing.py` (new) | `setup_tracing(settings)` in the lifespan (per gunicorn worker, after fork), a no-op provider when no endpoint is set, the ASGI instrumentation (probes excluded), the run span, `TraceHandler` (O7), and spans around retrieval (`rag.retrieve` with mode, k, candidates, rerank time and tokens). |
| `rag/retrieve.py`, `rag/index.py` | Report rerank tokens and embedding tokens to `METER` when one is set; no change when it is not (evals, tests). |
| `evals/driver.py`, `evals/scorecard.py` | Use `waypoint.usage` instead of their own copies. Scorecards must print the same costs as before for the same recorded calls (see below). |
| `web/` | One line under each finished reply: cost, tokens in and out, searches, duration; the ApprovalCard keeps its model line. Types in `api/types.ts`. |

## Verification plan

Hermetic, like everything else (`FAKE_MODEL`, no network, no key):

* **Pricing.** Unit tests for `call_cost` with plain, long-context, cache read
  and cache write usage, and web searches. The moved `call_cost` must give the
  same numbers as the old one on the old inputs.
* **Scorecard parity.** Price the same recorded `Call` lists with the old
  and the moved `call_cost` and `usage_summary`; they must agree to the cent,
  and a `--trials 1` replay run of the research eval must print the same cost
  as before the move. This is the check that O1 changed no reported number.
* **Meter.** `ScriptedChatModel` learns to attach a scripted `usage_metadata`
  (and a web-search count), so a fake research run produces known usage; the
  test asserts the per-role totals, `run_audit` columns and the
  `run.completed` payload.
* **Budget.** Against the test Redis (`tests/conftest.py: TEST_REDIS_URL`):
  admission at zero balance gives 429 with `Retry-After`; a charge can take
  the balance negative; refill restores admission; tenant and global keys
  both apply; two concurrent admissions at the edge are both allowed (O3's
  bound) and the overshoot is charged.
* **Tracing.** With an in-memory span exporter: one run yields a run span
  linked to its request span, node spans under it, model spans under the
  nodes with the GenAI and `waypoint.*` attributes, and no attribute
  containing prompt text when `TRACING__CONTENT` is off. Tracing off means no
  provider and no measurable overhead.
* **Live check after deploy** (about $0.30): one research run on Bob shows its
  cost in the UI, the same number in `run_audit` and in the trace, and the
  number is within a few percent of the Anthropic console for that minute.

## Phases

Each phase ends green: `uv run python -m pytest`, ruff, mypy, `npm test`,
`npm run build`.

| Phase | Scope | Done when |
|---|---|---|
| 0 | Settle the open decisions; move `prices.toml` and `call_cost` into `waypoint/usage.py`; evals import them. | Scorecard parity test passes; evals print the same costs. |
| 1 | `Meter` in every run; usage on `RunRecord`, events and `run_audit`; rerank and embedding tokens reported; `run_id` in the log context; UI cost line; `GET /usage`. | A fake run reports scripted usage end to end; a live run on Bob shows its cost in the UI. |
| 2 | Budgets: `admit` / `charge` Lua, settings, 429 with `Retry-After` on runs and uploads; DEPLOY.md and `.env.production.example` updated. | A tenant over budget gets a clean 429; the global cap applies across reviewer tenants. Deployed to Bob. |
| 3 | Tracing: `setup_tracing`, ASGI spans, run spans with links, `TraceHandler`, retrieval spans, `trace_id` in logs and `run_audit`; Jaeger in `compose.dev.yaml`. | One research run reads end to end in local Jaeger with cost on every model span. |
| 4 | Production backend (D2: choose it here); Caddy route or exporter secrets; the case study with a real run's breakdown by node and role. | A reviewer's run on Bob can be followed in the production backend; case study written. |

Phases 1 and 2 are the urgent part (they protect the public demo) and do not
depend on tracing. Phase 3 can be built and reviewed without touching budgets.

Then: prompt caching (`cache_control` on system prompts and tool definitions,
a smaller ROADMAP item), which this work makes measurable per run.

## Risks

* **Usage that callbacks never see.** Server-side web search is billed per
  request and reported in `response_metadata`, not `usage_metadata`; the eval
  recorder handles it and the shared meter must keep doing so. Calls made
  outside LangChain (Voyage) are only counted if the code reports them to
  `METER`; phase 1's test covers each such call site.
* **Callbacks swallowing errors.** LangChain logs and ignores exceptions in
  handlers unless `raise_error` is set. The meter must never break a run, so
  leave it off, but log at WARNING, and test that a broken exporter or Redis
  charge failure leaves the run completing.
* **Charging when Redis is down.** A failed charge must not fail the run;
  `run_audit` still records the cost, so spend is never lost, only not
  enforced for that call. Admission depends on Redis exactly as the RPM
  check already does, so a Redis outage changes nothing new there.
* **Prices go stale.** `prices.toml` has an `as_of`; the live check in the
  verification plan compares against the console. A wrong price under-charges
  the budget silently, so keep the table's source and date current.
* **Span volume.** A research run makes roughly 20 to 40 model calls and as
  many node spans. Check that against the chosen backend's limits in phase 4
  (D2); sample if traffic grows. SSE stream reads (`XREAD` every 5 s per
  open stream) are not traced; Redis and Postgres instrumentation stays off in
  v1 for the same reason.
* **Context across tasks.** OTel context lives in contextvars; the run runs in
  its own `asyncio` task, and LangChain callbacks fire on that task. Parent
  spans explicitly by `parent_run_id` rather than relying on the current
  context, so node and model spans nest correctly under concurrent workers.

## Implementation log

### Phase 0: shared pricing (2026-10-11)

* Open decisions settled: D1 to D4 above.
* `evals/prices.toml` moved to `src/waypoint/prices.toml` (`git mv`; only the
  header comments changed). It is package data: the built wheel contains
  `waypoint/prices.toml`, checked with `uv build --wheel`.
* `src/waypoint/usage.py`: `Price` (a frozen dataclass, so a malformed row
  fails when the table loads, not when a call is priced), `PriceTable`,
  `parse_prices`, `load_prices` (read once per process through
  `importlib.resources`) and `call_cost`, moved from `evals/scorecard.py`
  with the same rules.
* `evals/scorecard.py` and `evals/retrieval.py` import them; the eval copies
  and their `PRICES` path are gone.
* `tests/test_usage.py`: `call_cost` cases (long rate, threshold boundary,
  searches), table loading, and the parity tests. The pre-move `call_cost` is
  kept in the test as the oracle; the moved one must agree with it for every
  priced model at every rate boundary, and `usage_summary` must give the same
  total and per-run cost on a research-shaped run.
* Parity on real data: every saved transcript under `evals/results/` (27
  result sets, 2,843 calls) re-priced with `usage_summary` before and after the
  move gives identical summaries, and the 18 sets that have a `scorecard.json`
  match the cost stored in it exactly ($122.63 across all 27). This replaces
  the `--trials 1` replay run in the verification plan: it checks the same
  thing on more runs and costs nothing.

Deviation: `call_cost` does not yet price cache reads and writes. It moves to
phase 1 with the `Meter`, which is the first caller that sees cached tokens.
langchain's `input_tokens` includes the cached parts, so the cache rates need
the split `rag/contextualize.py: Usage.add` already does, and the meter's tests
are where that gets checked. `evals/retrieval.py` keeps its own
`CACHE_READ` / `CACHE_WRITE` multipliers until then.

### Phase 1: cost per run (2026-10-11)

* `waypoint/usage.py` gained `CallUsage` and `usage_of` (one reply's usage,
  with cached input split out of langchain's total), `Meter`, `METER`,
  `report_embedding` and `report_rerank`. `call_cost` now prices cache reads
  (0.1x), 5-minute writes (1.25x) and 1-hour writes (2x); the long-context
  threshold counts cached input too. With no cache tokens it gives the same
  numbers as before: the phase 0 parity tests still pass unchanged.
* `RunManager._drive` attaches a `Meter` to every run (callbacks and
  `METER`), binds `run_id`, `agent` and `tenant_id` into the log context, and
  writes `usage` to the run record, to every terminal event (`run.completed`,
  `run.interrupted`, `run.failed`, `run.cancelled`) and to `run_audit`. A
  failed audit write is logged at WARNING with the usage, not swallowed.
* Schema: the `run_audit` columns and tenant index from "Data model" above,
  idempotent in `SCHEMA`. `trace_id` is added now and stays null until phase 3,
  so phase 3 needs no migration.
* `Retriever.search` reports the query embedding and rerank tokens;
  `index_document` reports document embedding tokens (so an ingest run shows
  what indexing cost). The contextualizer's model carries
  `model_role="contextualize"`, so its calls during ingestion get a role of
  their own.
* `GET /usage` (`api/usage.py`): the tenant's spend over 1 to 31 days, summed
  from `run_audit`. Schema models `RunUsage`, `RoleUsage`, `TenantUsage`.
* UI: one line under the transcript when a run ends, with cost, tokens in and
  out, searches and duration, and a per-role breakdown on hover
  (`web/src/usage.ts`). A paused research run's line is labelled "Plan:".
* `evals/driver.py: UsageRecorder` reads usage with `usage_of`. It keeps
  counting all input at the input rate, as scorecards always have.
* Tests (`tests/test_meter.py`): cache pricing, the cache split, per-role
  sums, an unpriced model making the total `null`, a broken reply not
  breaking the meter, embedding and rerank reports, and a scripted chat run
  and research run (both legs) whose reported costs match prices computed by
  hand. `ScriptedChatModel.usage_script` gives the fake runs known usage. One
  integration test checks `run_audit` and `GET /usage` against Postgres.

Live check on Bob (2026-10-11, commit `3aac965`): one research run over
`waypoint-docs` with web search, in a fresh reviewer tenant (`rev-livecheck`),
compared with the Claude Console's Usage page for the `waypoint-demo` key in
the 20:00 UTC hour. Our side is `run_audit` (query in DEPLOY.md); the plan was
made twice (two `planner` rows) before one was approved.

| | `run_audit` | Console | |
|---|---|---|---|
| Sonnet 5, in + out (planner x2, critic, writer) | 34,561 | 34,561 | exact |
| Haiku 5.5, in + out (searcher, verifier) | 281,590 | 281,590 | exact |
| Tokens in | 286,897 | 286,897 | exact |
| Tokens out | 29,254 | 29,254 | exact |
| Web searches | 15 | 15 | exact |

Console figures are the day's totals minus the 18:00 UTC hour, which was a
run from before the deploy (its `run_audit` row has no cost, as designed).
The Console's Cost page is daily and has no API key filter, so dollars were
not compared directly; with tokens and searches exact, the dollars follow from
the published rates in `prices.toml`.

What the approved run cost, from its `usage` (total $0.3087, plus $0.0082 for
the plan): searcher $0.1833 (59%; $0.15 of it is 15 web searches at $0.01),
critic $0.0599, writer $0.0556, rerank $0.0064 (127,241 tokens), verifier
$0.0036, query embeddings under $0.0001. Web searches, not tokens, are the
largest single cost of a research run. The case study in phase 4 starts here.

One more row is worth noting: a second plan at 20:24 was followed by a run
that completed with no model calls and an empty `usage` (`cost_usd` 0). That
is what a declined plan records: the graph ends without calling a model, so
zero is the right answer rather than a missed meter.

Deviations:

* **The cost line is under the transcript, for the latest run, not under
  every reply.** Messages in a checkpoint do not say which run wrote them, so
  after a reload there is no run to look up for an old reply. The line shows
  for the run just finished; a past run's cost is in `GET /runs/<id>` while
  Redis keeps it, and in `run_audit` after.
* **Embedding tokens are estimated** (characters / 4, the chunker's
  estimate). langchain's `Embeddings` returns vectors only. At $0.06 per
  million tokens for voyage-4 the error cannot move a run's cost.
* **Not metered:** memory-store embeddings (the chat agent's `remember` and
  memory search go through LangGraph's store, which calls the embeddings model
  itself; text-embedding-3-small at $0.02 per million tokens), the boot-time
  embedding and rerank probes, and the `waypoint-docs` seed (it runs outside
  any run, in the system tenant). All three are fractions of a cent.
* Unpriced models make a run's `cost_usd` `null`, like the scorecards. Phase
  2 needs a number to charge; D3 (boot error when budgets are on) is what
  guarantees one.

### Phase 2: budgets (2026-10-12)

* `settings.BudgetSettings` (`BUDGET__TENANT_USD_PER_DAY`,
  `BUDGET__GLOBAL_USD_PER_DAY`, `BUDGET__TENANTS`, `BUDGET__ENABLED`), with
  D1's validation: half a budget is an error, and `ENV=production` refuses to
  boot without a decision. `null` in `BUDGET__TENANTS` exempts a tenant from
  its own and the global budget; a number overrides its own.
* `infra/ratelimit.py`: one Lua script over the O4 buckets, read-only for
  `Limiter.admit` and subtracting for `Limiter.charge`, touching the tenant's
  key and `rl:{global}:usd` in one call; `Limiter.balance` for `GET /usage`.
  Balances refill at budget / 86,400 per second, cap at the budget, may go
  negative, and keys expire once they would have refilled anyway.
* Admission in `POST /threads/<id>/runs` (after the RPM check, before the
  slot) and in corpus uploads, before any document is stored. `OverBudget` is
  a `RateLimited` with slug `over-budget` and `scope` `tenant` or `server`;
  the server variant names no amounts (D4).
* Charging: `Meter` takes a `charge` callback; each chat model call's cost is
  charged when the call ends, with any embedding or rerank cost reported since,
  and `RunManager._drive` flushes the rest when the run ends. The amount is
  counted before the await, so concurrent calls never charge twice. A failed
  charge is logged at WARNING and the run carries on; `run_audit` still has
  the spend.
* D3: `usage.check_prices` at app creation. With budgets on, a model the
  service may call (`MODEL`, `MODELS__*`, `MODELS_ALLOWED`, and the
  contextualizer's when on) missing from `prices.toml` is a boot error;
  otherwise a warning.
* `GET /usage` gains `budget_usd_per_day`, `balance_usd` and `exempt`.
* Deploy: `compose.prod.yaml` sets `BUDGET__ENABLED=true` and requires both
  amounts (`${...:?}`); `deploy/.env.production.example` has D1's numbers and
  how to find the tenant to exempt.
* Tests (`tests/test_budget.py`): settings, D3, the 429's shape, the meter
  charging each cost once and surviving a failed charge, and against Redis:
  admission at zero, refill (by ageing the bucket's timestamp), the global
  cap across tenants including a raised one, an exempt tenant untouched, two
  admissions at the edge both passing with both runs charged (O3's bound),
  and end to end, a chat run that spends a tiny budget and a second request
  refused with `Retry-After`, `GET /usage` showing the negative balance.

Deviations:

* **`BUDGET__ENABLED`**, not in the design. `compose.yaml` sets
  `ENV=production` for the local Docker stack too, so "required in
  production" alone would have stopped that stack on the next pull. It
  defaults to `false` in `compose.yaml` and is forced to `true` in
  `compose.prod.yaml`, which is the production that matters.
* **`Retry-After` on every 429.** The RPM limit only had `retry_after` in the
  body; the error handler now sets the header for any problem that carries
  one, rounded up to whole seconds.
* **One Redis, not a cluster.** The script touches a tenant's key and the
  global key together, which a cluster refuses across hash slots. Bob runs one
  Redis; a cluster would need the two charges as separate calls.
* **The production check uses a tiny override, not the real budget.** Proving
  the global cap live would refuse every reviewer for hours; the Redis tests
  cover it.

Production check on Bob (2026-10-12, `b62cbaf`): budgets at D1's numbers,
`brent` exempt, and a throwaway tenant `rev-budgetcheck` overridden to
$0.0001 a day. One chat message was admitted (the bucket started full) and
charged as its model call ended. `GET /usage` with that key then reported:

| Field | Value | Check |
|---|---|---|
| `runs`, `cost_usd` | 1, $0.002874 | 867 in and 114 out on Sonnet 5 at $2 / $10 per million: 0.001734 + 0.00114 |
| `budget_usd_per_day` | 0.0001 | the override |
| `balance_usd` | -0.002774 | 0.0001 - 0.002874: the whole cost charged once, below zero as O3 allows |
| `exempt` | false | |

With the balance below zero, the next message was refused with
`429 over-budget`, and no second run was recorded. The key was revoked and
the override removed afterwards.

### Phase 3: tracing (2026-10-12)

* `waypoint/tracing.py`: `setup_tracing` (a `TracerProvider` with a batch
  OTLP/HTTP exporter to `OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces`, or the no-op
  `NOOP` when it is unset), `start_run_span` / `end_run_span`,
  `TraceHandler`, `TRACE` and `child_span`. OpenTelemetry 1.45 (SDK, OTLP
  HTTP exporter) and the ASGI instrumentation 0.66b1 are new dependencies.
* Requests: the ASGI middleware is outermost, probes excluded, and
  `RequestContext` binds the request's trace id into the log context.
* Runs: `RunManager.start` opens the run span as a new root linked to the
  request span (O8; `api/runs.py` and the corpus upload pass the link), so
  the trace id is on the record and the `run.started` event from the start
  and goes into `run_audit.trace_id` with the start row. `_drive` makes the
  span current for the run's task, binds `trace_id` into the log context,
  adds `TraceHandler` to the callbacks beside the meter, and ends the span
  with status, cost, tokens and searches.
* `TraceHandler`: a span per graph node (a chain whose name is its
  `langgraph_node`; inner runnables inherit that metadata but not the name),
  per chat model call (`gen_ai.*` model and tokens, `waypoint.role`,
  `waypoint.cost_usd` priced from the same table, cache tokens, searches) and
  per tool call. Parents come from `parent_run_id`, walking up to the nearest
  run that has a span, never from the current context. A `GraphInterrupt` (the
  approval pause) ends its node span without an error.
* Retrieval: `Retriever.search` opens `rag.retrieve` with `child_span`, which
  finds the running node through LangChain's `var_child_runnable_config`
  and the handler in `TRACE`, so it nests under the worker that searched.
* `TRACING__CONTENT` (O9) adds prompts, completions and tool inputs, up to
  8,000 characters each; off, no span holds any.
* `compose.dev.yaml` has Jaeger 2.20.0 (UI `:16686`, OTLP `:4318`);
  `compose.yaml` passes `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`
  and `TRACING__CONTENT` through, empty by default, so phase 4 is a `.env`
  change on Bob.
* Tests (`tests/test_tracing.py`, in-memory exporter): off without an
  endpoint; a chat run's run span is a root linked to its request, nodes
  under it, the model span under its node with tokens, cost and role; no
  attribute anywhere contains the prompt, unless content is on; probes make
  no spans; the trace id is in `run.started`; a research plan and its resume
  are two traces on one thread, the approval pause is not an error, every
  `rag.retrieve` and searcher call sits under `node investigate`; and the
  handler on its own nests interleaved calls by parent, closes spans a
  cancelled run left open, and marks a failed model call.

Deviations:

* **The provider is built when the app is created, not in the lifespan.**
  Same effect: gunicorn (no `--preload`) imports the app in each worker after
  forking. It also lets the ASGI middleware be added at creation. It is not
  registered as the global provider, so test apps each have their own.
* **`receive` and `send` ASGI spans are off.** With them, an SSE stream makes
  one span per event sent.
* **Tool calls get spans too** (`execute_tool <name>`); the design listed
  nodes and model calls only. Cheap, and the chat agent's memory tools show.
* **`gen_ai.usage.cost` is set beside `waypoint.cost_usd`**: Langfuse reads
  it, which keeps that option open for phase 4 at no cost.
* **Request spans are named by route** (`POST /api/v1/threads/{thread_id}/runs`).
  The ASGI middleware names a span from the raw path before routing, which
  puts ids in span names and makes every request its own operation in a
  backend. It now starts with the method alone, and `RequestContext` renames
  it from the matched route when the response starts; an unmatched path (a
  404) keeps the method only.

Local check (2026-10-12, Windows dev machine): Jaeger 2.20.0 from
`compose.dev.yaml`, the API on the host with `FAKE_MODEL=true`, no Postgres or
Redis, and `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`. A document
upload, then a research run over it, plan and resume. Jaeger's API returned
the briefing run's trace with 15 spans: `run research` as root; `node approve`,
three `node investigate`, `node critique`, `node synthesize` and `node verify`
under it; under each worker its `rag.retrieve` (vector mode, 1 hit) and its
`chat claude-haiku-5-5` (role searcher); under `node synthesize` the writer's
`chat claude-sonnet-5`. The run span's link resolved to a separate trace
holding `POST /api/v1/threads/{thread_id}/runs`. The scripted model reports no
usage, so cost and tokens were zero on every model span.

Live check, same setup with real models (Sonnet 5, searcher and verifier on
Haiku 5.5, Voyage embeddings and reranking), one research run over a small
uploaded document, plan approved. The briefing run reported $0.310202
(225,891 tokens in, 25,660 out, 17 web searches) on its record. Its trace in
Jaeger had 36 spans: `run research`; `node approve`, eight `node investigate`,
two `node critique`, `node synthesize`,
`node verify`; eight `rag.retrieve` under the workers; fourteen model spans
(ten Haiku, four Sonnet). Every model span carried a cost. Summed over the
trace, the model spans give $0.310136, the same tokens in and out and the same
17 searches; with the $0.000020 of query embeddings and $0.000046 of
reranking, which have no model span, that is the run's $0.310202 exactly. The
trace also shows where the 115 seconds went: workers 97.8 s in total (they run
in parallel within a round), critique 43.5 s, verification 24.0 s, writing
13.7 s. The plan run cost $0.006062 and the document's ingestion $0.000004.

## References

* [OpenTelemetry semantic conventions for generative AI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
  (all `gen_ai.*` attributes still "Development" status as of mid-2026, per
  [this summary](https://dev.to/azena-ai/opentelemetrys-genai-semantic-conventions-are-not-stable-yet-heres-what-actually-shipped-in-2026-3mke)).
* [Langfuse: OpenTelemetry ingestion](https://langfuse.com/integrations/native/opentelemetry):
  OTLP over HTTP at `/api/public/otel`, basic auth, gRPC not supported;
  reads `gen_ai.*` model and usage attributes, and cost from
  `gen_ai.usage.cost` or `langfuse.observation.cost_details`.
* [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing),
  the source of `prices.toml`, including prompt-cache multipliers.
* [LangChain `UsageMetadata`](https://reference.langchain.com/python/langchain-core/messages/ai/UsageMetadata):
  `input_tokens` is the sum of all input token types.
