# Design: observability and cost

Status: **not started.** Written 2026-10-11 as roadmap item 4.
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

## Open decisions (settle in phase 0)

1. **Budget numbers.** Proposed: `BUDGET__TENANT_USD_PER_DAY=2.00` (about six
   research runs), `BUDGET__GLOBAL_USD_PER_DAY=10.00`, and an override map
   `BUDGET__TENANTS` (JSON, like `MODELS__*`) for Brent's own tenant. Brent
   decides the numbers and which tenant is exempt or raised. Also decide
   whether the budget is off when unset (proposed: off in development, required
   in production, like `SECRET_KEY`).
2. **Trace backend in production.** Candidates:
   * *Jaeger v2 all-in-one* on Bob behind Caddy with basic auth: one small
     container, OTLP in, a good waterfall view, no LLM-specific views. Confirm
     the image tag and a persistent storage option at implementation time.
   * *Langfuse Cloud* (free tier): LLM-aware views with cost per generation
     out of the box; data leaves the server (ids and numbers only, per O9).
     Confirm current free-tier limits and region.
   * *Self-hosted Langfuse*: needs Postgres, ClickHouse, Redis and object
     storage plus web and worker containers; too heavy for Bob next to future
     demos. Rejected unless Bob grows.

   Development uses Jaeger in `compose.dev.yaml` either way.
3. **Price table ownership.** Moving `prices.toml` into the package means the
   service reads it at boot. Decide whether a model missing from the table is a
   boot error in production (proposed: yes, when budgets are on, since an
   unpriced model would spend without being charged) or a warning.
4. **What the reviewer sees.** Proposed: cost per run is shown to every key
   (it is the tenant's own spend), and a `GET /usage` endpoint returns the
   tenant's balance and today's spend. Decide whether reviewers see the global
   balance (proposed: no, only that the server is over budget, in the 429).

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
| 4 | Production backend from open decision 2; Caddy route or exporter secrets; the case study with a real run's breakdown by node and role. | A reviewer's run on Bob can be followed in the production backend; case study written. |

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
  many node spans. Check that against the chosen backend's limits in phase 0
  (open decision 2); sample if traffic grows. SSE stream reads (`XREAD` every 5 s per
  open stream) are not traced; Redis and Postgres instrumentation stays off in
  v1 for the same reason.
* **Context across tasks.** OTel context lives in contextvars; the run runs in
  its own `asyncio` task, and LangChain callbacks fire on that task. Parent
  spans explicitly by `parent_run_id` rather than relying on the current
  context, so node and model spans nest correctly under concurrent workers.

## Implementation log

Nothing built yet.

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
