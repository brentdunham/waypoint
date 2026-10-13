# Configuration

Everything is read from the environment (or `.env`) by
`src/waypoint/settings.py`. Nested groups use a **double underscore**:
`LIMITS__RPM=120` sets `settings.limits.rpm`.

Types are enforced by pydantic at startup — a bad value fails the boot loudly
rather than at 3am under load.

Two parsing rules worth knowing, because both bite in Docker:

* **List fields are comma-separated, not JSON.** `CORS_ORIGINS=http://a,http://b`.
  (pydantic-settings would otherwise try to JSON-decode the value before any
  validator runs, and `http://a` is not JSON.)
* **An empty value means unset.** `ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}` in
  compose sends an empty string; the optional credential and DSN fields treat
  that as `None` rather than as the string `""`.

## Application

| Variable | Default | Notes |
|---|---|---|
| `ENV` | `development` | `development` \| `production` \| `test` |
| `DEBUG` | `false` | FastAPI debug mode. Error responses never carry tracebacks either way (the request middleware answers every unhandled error with a problem+json 500); keep it `false` in production. |
| `SECRET_KEY` | `dev-only-change-me` | Reserved for signing cookies or sessions. Unused today (the API is stateless, and since the move to FastAPI nothing reads it), but must be set, stable, and identical on every replica the moment you add a session. `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `LOG_LEVEL` | `INFO` | Root logger level. Each request is logged once, by `waypoint.access`, with request id, tenant and duration; successful `/healthz` and `/readyz` probes only at `DEBUG`. uvicorn's own access log is off. |
| `JSON_LOGS` | `false` | One JSON object per line. Turn on in production. |
| `CORS_ORIGINS` | `http://localhost:5173` | **Comma-separated, not JSON** — `http://a,http://b`. Only needed when the browser talks to the API cross-origin; the Vite proxy and the nginx `/api` route both make it same-origin, so in Docker this rarely matters. |

## Authentication

| Variable | Default | Notes |
|---|---|---|
| `AUTH_ENABLED` | `true` | `false` makes every request the `public/anonymous` principal with all scopes. Local experiments only. |
| `BOOTSTRAP_API_KEY` | — | Seeded on boot as tenant/user `bootstrap` with the `*` scope. This is how you get your first key. **Unset it in production** once you have minted real keys. |

There is no default and no placeholder, on purpose: a key printed in
`.env.example` would be a full-access credential that everyone who has read the
repository already knows. The app refuses to seed the old placeholder value and
says so at `ERROR` level. Generate your own:

```bash
python -c "import secrets; print(f'ak_{secrets.token_hex(6)}.{secrets.token_urlsafe(32)}')"
```

On boot the log names the `key_id` that is actually live —
`bootstrap API key active (key_id=16dd20a2540f)` — so
`docker compose logs api | grep bootstrap` answers "which key does this
deployment accept?" without going into the database. A `401` with
*Unknown or revoked API key* means the key you sent hashes to nothing: compare
that `key_id` against the one in the key you are pasting.

Keys are `ak_<key_id>.<secret>`. Only the SHA-256 of the whole string is
stored. Scopes are `agents:run`, `threads:read`, `threads:write`, `corpora:write`,
`keys:admin`, and `*`. Mint more with `POST /api/v1/keys`.

## Model

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Your outbound credential to the Anthropic API. Distinct from `SECRET_KEY` in every way: it leaves the process, it costs money, and it identifies your account. |
| `MODEL` | `claude-sonnet-4-5` | |
| `MODEL_TEMPERATURE` | *unset* | **Omitted from the request unless you set it.** Newer Anthropic models reject `temperature` with a `400 invalid_request_error`, and older ones already default to 0 — so a global default is all downside. Set it only when you want variety, or pass `temperature=` at the one call site that needs it. Range 0–1, validated at boot. |
| `MAX_TOKENS` | `4096` | |
| `STRUCTURED_OUTPUT` | `json_schema` | How the research planner and critic (and the eval judge) get typed output. `json_schema` is Anthropic structured outputs: decoding is constrained to the schema, so the reply always parses. `function_calling` is a forced tool call, for models without structured outputs; live runs on claude-sonnet-5 produced malformed tool input with it (a list sent as a string). |
| `FAKE_MODEL` | `false` | Swaps in `ScriptedChatModel`: deterministic, offline, free. The test suite runs on it; so can you, when working on plumbing rather than prompts. |

`src/waypoint/agents/models.py::build_model` is the only place in the codebase that
names a provider. Change it there and every graph follows.

### A model per agent and role

Each agent declares its model call sites (`GET /agents` lists them as
`model_roles`): chat has `assistant`; research has `planner`, `searcher`,
`critic` and `writer`. Each role can have its own model.

| Variable | Default | Notes |
|---|---|---|
| `MODELS__<AGENT>__DEFAULT` | *unset* | Every role of that agent not named below. |
| `MODELS__<AGENT>__<ROLE>` | *unset* | One role, e.g. `MODELS__RESEARCH__CRITIC=claude-opus-4-5`. |
| `MODELS_ALLOWED` | *every model named above* | Comma list of models a run request may choose with `models` (API.md). Empty means `MODEL` plus every `MODELS__*` value, so callers choose between approved models and cannot introduce a new, more expensive one. |

Resolution, most specific first: the run's choice for the role, the run's
`default`, `MODELS__<AGENT>__<ROLE>`, `MODELS__<AGENT>__DEFAULT`, `MODEL`.
Names are case-insensitive. A typo in an agent or role name stops the boot
with the list of valid names. The searcher needs a model with server-side web
search (Claude 4.6 and later, see `RESEARCH__WEB_SEARCH`).

`.env.example` sets `MODELS__RESEARCH__SEARCHER=claude-haiku-5-5`. The searcher
re-reads every search result as input, so it was about 84% of a research run's
cost on claude-sonnet-5 (2026-10-08 evals: 4.6M of 5.6M input tokens per
8-case, 3-trial run). Haiku 5.5 is $0.10 / $0.50 per million input / output
tokens for prompts up to 100k tokens, against $2 / $10 for Sonnet 5. Planner,
critic and writer stay on `MODEL`; they judge and write, which is where the
larger model earns its price. It also sets `MODELS__RESEARCH__VERIFIER=claude-haiku-5-5`:
the verifier sorts each briefing sentence into supported, wrong source, needs
a citation or unsupported, against short source quotes, and Haiku does that.
For the same reason it sets `MODELS__WORKSPACE__VERIFIER=claude-haiku-5-5`,
the workspace agent's verifier. Workspace's other role, `assistant`, answers
and rewrites unsupported sentences, and falls back to `MODEL`.

The resolved map is fixed when a run starts and recorded on the run
(`GET /runs/<id>` → `models`) and in the research briefing's metadata.

## Infrastructure

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Postgres DSN. Unset → in-memory checkpointer and store; state dies with the process. |
| `REDIS_URL` | — | Unset → single-process event bus, no rate limits, no shared node cache. |
| `REDIS_SOCKET_TIMEOUT_S` | `30` | Read timeout for every Redis command, including the blocking `XREAD` behind SSE. Must exceed `STREAM__BLOCK_MS` by at least 1s; startup refuses otherwise. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `1` / `10` | One pool serves the checkpointer, the store and this service's own tables. Size it for `max_connections` divided by replicas. |

The Postgres image must be `pgvector/pgvector`. Since the RAG tables
(2026-10-10), every boot runs `create extension if not exists vector`, so plain
`postgres` no longer boots. The migration also creates a cluster-wide role,
`waypoint_tenant` (`nologin`), so the service's database user needs
`CREATEROLE` or superuser on first boot; the compose images' user is a
superuser.

## Long-term memory

| Variable | Default | Notes |
|---|---|---|
| `EMBEDDINGS_MODEL` | — | e.g. `openai:text-embedding-3-small`. Unset → the store still works for exact gets, puts and namespace listing, but `asearch(query=…)` degrades to no semantic ranking. |
| `EMBEDDINGS_DIMS` | `1536` | Must match the model. Changing it after data exists means reindexing. |
| `OPENAI_API_KEY` | — | Required when `EMBEDDINGS_MODEL` starts with `openai:`; ignored otherwise. Outbound credential, from <https://platform.openai.com/api-keys>. |

Startup checks the embeddings config rather than waiting for the first
`remember`: a missing key, an uninstalled provider package or a bad
`provider:model` string stops the boot with a message naming the fix. With a
real provider it also embeds one string and compares the length to
`EMBEDDINGS_DIMS`; a mismatch or a rejected key (401/403) stops the boot, a
network error is only logged. The `vector` extension and the `store_vectors`
table are created by `store.setup()` on first boot, so there is nothing to do
in Postgres beyond using the `pgvector/pgvector` image.

`openai:` and `voyageai:` ship installed (`langchain-openai`,
`langchain-voyageai`). Another provider means adding its package. Changing
model or dims after memories exist requires re-embedding them: the stored
vectors were made by the old model.

## Document retrieval (RAG)

Corpora of tenant documents, ingested by the `ingest` agent and searched by
the research agent when a run names a `corpus_id` (API.md).
The research eval's corpus cases need `RAG__EMBEDDINGS_MODEL` and
`VOYAGE_API_KEY` too, and `RAG__MODE=vector`: they index in memory, where
only vector search exists. Design: [design/rag.md](design/rag.md).
Configured apart from long-term memory, so the two can use different embedders
and neither change forces re-embedding the other.

| Variable | Default | Notes |
|---|---|---|
| `RAG__EMBEDDINGS_MODEL` | — | The chosen model is `voyageai:voyage-4` (commented in `.env.example`, like memory's). Unset → RAG is off: `/corpora` answers 503, and a research run that names a corpus fails. |
| `RAG__EMBEDDINGS_DIMS` | `1024` | Width of `chunks.embedding`. Voyage 4 offers 256, 512, 1024 and 2048; pgvector's HNSW index stops at 2000 for `vector`, so 2048 is out. |
| `VOYAGE_API_KEY` | — | Required when either embeddings model starts with `voyageai:`, and whenever RAG is on with `RAG__RERANK` (the default). From <https://dashboard.voyageai.com/>. |
| `RAG__CHUNK_TOKENS` | `400` | Target chunk size, estimated as characters / 4. Takes effect for documents ingested afterwards. |
| `RAG__MAX_UPLOAD_MB` | `5` | Per file. A whole upload request is also capped at 8 MB by the middleware. |
| `RAG__TOP_K` | `8` | Passages each research worker gets for its question. Each is input on every web search in that worker call; the design measures 5, 8, 12 and 20. |
| `RAG__MODE` | `vector` | `vector`, `keyword` (Postgres full text) or `hybrid` (both, fused with reciprocal rank fusion). `vector` because it measured best: on the retrieval baseline (2026-10-10) recall@8 was 0.93 for vector, 0.79 for hybrid and 0.59 for keyword, MRR 0.80, 0.62 and 0.41 (design/rag.md, Phase 2). |
| `RAG__CANDIDATES` | `50` | Chunks each method ranks before fusion, and the chunks the reranker reorders. |
| `RAG__RRF_K` | `60` | RRF constant: a chunk at rank r scores 1/(60 + r) per method that found it. 60 is the value from the original RRF paper and the design. |
| `RAG__RERANK` | `true` | Rerank the first stage's top `RAG__CANDIDATES` with Voyage before taking `RAG__TOP_K`. Needs `VOYAGE_API_KEY`; a rejected key stops the boot. On because it measured best: vector MRR 0.80 to 0.90, recall@5 0.88 to 0.96, for about 13.6k billed tokens ($0.0007) and 0.2 s per query (design/rag.md, Phase 4; [case study](case-studies/retrieval.md)). |
| `RAG__RERANK_MODEL` | `rerank-3` | $0.05 per million tokens, 200M free (docs.voyageai.com/docs/pricing, 2026-10-10). `rerank-3-lite` is $0.02. |
| `RAG__CONTEXTUAL` | `false` | Contextual retrieval: `RAG__CONTEXT_MODEL` writes a one or two sentence preamble per chunk, with the document cached, before embedding. Off: it did not beat plain vector search here. Applies to documents ingested afterwards. |
| `RAG__CONTEXT_MODEL` | `claude-haiku-5-5` | About $0.05 to contextualize this repository's `docs/` (251 chunks). |
| `RAG__SEED_DIR` | *unset* | A directory kept indexed as a **public** corpus, searchable by every key and changeable by none. Reconciled in the background at every boot, Postgres only: new and changed files are indexed, removed ones deleted, unchanged ones left alone. The API image contains `docs/`, and production sets `docs`. Titles are paths relative to the working directory (`docs/DEPLOY.md`). `python -m waypoint.seed` runs it in the foreground. |
| `RAG__SEED_CORPUS` | `waypoint-docs` | Name of that corpus. Owned by the system tenant `waypoint`. |

Why `voyage-4` at 1024 (decided 2026-10-10, prices from
<https://docs.voyageai.com/docs/pricing> that day): Anthropic has no embedding
model and points to Voyage. voyage-4-large is $0.12, voyage-4 $0.06 and
voyage-4-lite $0.02 per million tokens, each with 200M free tokens. This
repository's `docs/` is about 52k tokens (236 chunks at 400 tokens), so
embedding it costs well under a cent with any of them and price does not
decide. voyage-4 is the general-purpose model; large and the contextualized
`voyage-context-4` were phase 4 ablations and neither beat voyage-4 (both
work: `RAG__EMBEDDINGS_MODEL=voyageai:voyage-context-4` embeds each chunk
with the rest of its document). The 4 series shares one embedding space.
1024 rather than 2048 keeps the HNSW index on plain `vector`.

Startup checks run as for memory: a missing key, a width Voyage does not
offer, or a provider that returns a different width stops the boot, and so
does a `chunks.embedding` column whose width differs from
`RAG__EMBEDDINGS_DIMS`. With `RAG__RERANK` on, a missing `VOYAGE_API_KEY` or
a key the reranker rejects (one tiny rerank call) stops it too. The column is
sized when the table is first created.
To change the width afterwards, drop `chunks` (documents keep their content)
and re-ingest. Each corpus records the model its chunks were embedded with;
ingesting into a corpus made with another model fails that document rather
than mixing vectors from two models.

## Research agent

| Variable | Default | Notes |
|---|---|---|
| `RESEARCH__WEB_SEARCH` | `true` | Workers use Anthropic's server-side web search (`web_search_20250305`) and cite results. Must be enabled for the organization in the Claude Console, and the model must support it (Claude 4.6 and later). A rejected request fails the run with a message pointing here. `false` -> answers from the model alone, no sources, confidence capped at 0.3. |
| `RESEARCH__MAX_SEARCHES` | `2` | Web searches per worker call (the tool's `max_uses`). Each search's results are input again for every later search in the same call: in the 2026-10-08 evals, calls that made a third search averaged 48k input tokens against 21k for two. |
| `RESEARCH__MAX_ROUNDS` | `1` | Investigation waves, counting the first. `1` disables critic follow-ups. The default was `2` until 2026-10-12; one round cost 38% less per run, with `covers` 0.11 lower ([case study](case-studies/cost.md)). Set `2` when coverage matters more than cost. |
| `RESEARCH__MAX_FOLLOW_UPS` | `3` | New questions the critic may add per round. |
| `RESEARCH__VERIFY_CITATIONS` | `true` | After the writer, check every sentence of the briefing against the sources' quoted text. A wrong or missing `[n]` is replaced in code; sentences no source supports go back to the writer's model once, and the revision is kept only if every number it cites is a source. One verifier call per run, plus one writer call when something is unsupported. The briefing's `metadata.verification` counts what changed. |

Cost bound per run: at most `(5 + MAX_FOLLOW_UPS x (MAX_ROUNDS - 1)) x
MAX_SEARCHES` searches, 10 with the defaults (16 with two rounds). Web search is billed per search
($10 per 1,000 at the time of writing) on top of tokens, and search results
count as input tokens, which is usually the larger cost. A two-question run
with one search each and one follow-up took about 70 seconds.

## Workspace agent

Chat over a thread's documents ([design](design/tools.md)). The corpus is
chosen per thread, by run input, not by a setting; retrieval uses the
`RAG__*` settings above, and the agent's models are `MODELS__WORKSPACE__*`.

| Variable | Default | Notes |
|---|---|---|
| `WORKSPACE__VERIFY_CITATIONS` | `true` | Check every reply that cites documents with the research verifier: wrong or missing `[n]` fixed in code, unsupported sentences rewritten once by the assistant's model. One verifier call per citing reply (about $0.003 on Haiku 5.5, 5 to 15 s); replies that cite nothing are not checked. `metadata.verification` counts what changed. |

## Caching

| Variable | Default | Notes |
|---|---|---|
| `NODE_CACHE_TTL_S` | `300` | Redis-backed LangGraph node cache. `0` disables it. Only nodes given a `CachePolicy` participate. |

## Limits (per tenant, enforced in Redis)

| Variable | Default | Notes |
|---|---|---|
| `LIMITS__RPM` | `60` | Token bucket: capacity `RPM`, refill `RPM/60` per second. Allows a burst, then smooths. |
| `LIMITS__CONCURRENT_RUNS` | `4` | Counting semaphore with a lease, so a crashed worker cannot leak a slot. Taken *before* the POST returns, so over-quota callers get a clean 429. |
| `LIMITS__MAX_INPUT_CHARS` | `100000` | Checked before the model is called. |
| `LIMITS__RECURSION_LIMIT` | `40` | LangGraph super-steps per run. The backstop against a looping agent. |
| `LIMITS__RUN_TIMEOUT_S` | `300` | Wall clock per run. |

## Budgets (US dollars per day, enforced in Redis)

| Variable | Default | Notes |
|---|---|---|
| `BUDGET__TENANT_USD_PER_DAY` | unset | Each tenant's daily budget. The demo uses `0.35`, about one research run per reviewer a day. |
| `BUDGET__GLOBAL_USD_PER_DAY` | unset | All non-exempt tenants together. The demo uses `0.70`: any 30 days cost at most about $21. |
| `BUDGET__TENANTS` | `{}` | JSON overrides. A number raises (or lowers) one tenant's budget; `null` exempts the tenant from its own and the global budget: `'{"brent": null, "acme": 2.0}'`. |
| `BUDGET__ENABLED` | unset | Unset: on when both amounts are set. `false`: off, even in production. `true`: on, both amounts required. |

Each budget is a bucket that holds a day's amount and refills continuously
(budget / 86,400 per second), so there is no midnight reset where every
tenant's budget returns at once. Starting a run (or uploading documents)
needs the tenant's balance and the global one above zero; otherwise the
request gets `429` with slug `over-budget`, `scope` (`tenant` or `server`) and
a `Retry-After` header. Each model call's cost is charged when the call ends,
priced from `src/waypoint/prices.toml`. A run already going is never stopped,
so a balance can go below zero by what in-flight runs spend: at most
`LIMITS__CONCURRENT_RUNS` runs per tenant, each bounded by the research
limits and `LIMITS__RUN_TIMEOUT_S`.

Why dollars rather than tokens: a Sonnet token costs 20 times a Haiku token
and a web search costs a cent whatever its tokens, so a token budget would be
wrong by an order of magnitude depending on routing (observability design,
O2). Why not stop a run mid-way: its cost is known only as it goes, and
stopping leaves a half-written briefing for at most one run's saving (O3).

Unset in development means off. `ENV=production` refuses to boot until both
amounts are set or `BUDGET__ENABLED=false` says otherwise; `compose.prod.yaml`
turns budgets on and requires the amounts. With budgets on, a model the
service may call that `prices.toml` does not price is a boot error, since its
calls would never be charged. Budgets need `REDIS_URL`; without Redis nothing
is enforced.

## Streaming

| Variable | Default | Notes |
|---|---|---|
| `STREAM__MAXLEN` | `2000` | Events retained per run in the Redis Stream. Older ones are trimmed, so a very long run cannot be replayed from the very beginning. |
| `STREAM__TTL_S` | `3600` | How long a finished run stays readable. |
| `STREAM__HEARTBEAT_S` | `15` | SSE keep-alive comment interval. Keep it below your proxy's idle timeout. |
| `STREAM__BLOCK_MS` | `5000` | `XREAD BLOCK` timeout. Lower = faster shutdown detection, more Redis chatter. Must be at least 1s below `REDIS_SOCKET_TIMEOUT_S`. |

## Evals

Read by `python -m evals` only (TESTING.md), never by the service.

| Variable | Default | Notes |
|---|---|---|
| `EVALS__JUDGE_MODEL` | `claude-haiku-4-5` | Grades each rubric criterion. The cheapest current Claude model, because the judge runs once per case, criterion and trial. Whether it can be trusted on a criterion is measured, not assumed: see `evals calibrate`. |
| `EVALS__JUDGE_ESCALATION_MODEL` | *unset* | Grades the criteria where calibration found the judge below `EVALS__MIN_AGREEMENT`, if it does better. Unset → those criteria are reported as untrusted and left ungraded. A Sonnet model is the usual choice. |
| `EVALS__JUDGE_TEMPERATURE` | `0` | Makes grades repeatable. At the API default Haiku scored one briefing 3, 2, 2, 2, 2 across five identical calls; at 0 it gave 2 every time. Haiku 4.5 and Sonnet 4.5 accept it; leave it empty for a judge model that rejects `temperature`. |
| `EVALS__MIN_AGREEMENT` | `0.8` | Share of hand-labelled examples where judge and label must agree on pass/fail. |
| `EVALS__DATABASE_URL` | *unset* | Postgres for the retrieval eval's index (`evals retrieval`), so it measures the SQL the service runs. A database of its own (`waypoint_evals`), never the service's. Not needed for `--check` or `--fake`. |

Changing `EVALS__JUDGE_MODEL` or a rubric invalidates its calibration; rerun
`evals calibrate`.

The scheduled eval workflow does not read `.env`: it takes these values from
its own `env` block and API keys from repository secrets (TESTING.md §4,
Scheduled runs).

## Tracing

| Variable | Default | Notes |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP base URL; spans go to `<url>/v1/traces`. Unset: no tracing and no overhead. `http://localhost:4318` with `compose.dev.yaml`'s Jaeger (UI on `:16686`). |
| `OTEL_EXPORTER_OTLP_HEADERS` | unset | Read by the OpenTelemetry SDK, for backends that need auth (`Authorization=Basic ...`). Other standard `OTEL_*` variables work too. |
| `TRACING__CONTENT` | `false` | `true` puts prompts, completions and tool inputs on spans, up to 8,000 characters each. Local debugging only. |
| `TRACING__PANEL` | `false` | Serve `GET /runs/<id>/trace`, the "How this run went" timeline the UI shows under a finished run. Needs `TRACING__QUERY_URL`. Off: the endpoint is a 404 and the UI shows no timeline. |
| `TRACING__QUERY_URL` | unset | Jaeger's query service, read for the timeline: `http://jaeger:16686` in `compose.prod.yaml`, `http://localhost:16686` with `compose.dev.yaml`. |
| `LANGSMITH_TRACING` | `false` | |
| `LANGSMITH_PROJECT` | `waypoint` | |
| `LANGSMITH_API_KEY` | — | Read by LangSmith directly from the environment. |

Each run is its own trace: a root span `run <agent>`, linked to the
`POST /runs` request span rather than parented by it (a run outlives its
request, and a resume may come hours later from another worker). Under it,
one span per graph node (`node <name>`), and under each node its model calls
(`chat <model>`, with `gen_ai.*` model and token attributes plus
`waypoint.role`, `waypoint.cost_usd`, cache tokens and searches), tool calls
and document retrieval (`rag.retrieve`, with mode, k, hits and rerank time and
tokens). Every span carries `waypoint.run_id`, `waypoint.tenant_id` and
`waypoint.agent`. The run's trace id is on the run record, the `run.started`
event, `run_audit.trace_id` and every log line the run writes, so a log line
leads to its trace. Requests are traced too, health probes excepted, and their
log lines carry the request's trace id.

Spans hold ids, names, numbers and timings, never prompt or document text,
unless `TRACING__CONTENT=true`: a trace backend is one more place tenant data
would have to be secured (observability design, O9). LangSmith tracing is
separate and unchanged.

## Tests

| Variable | Notes |
|---|---|
| `TEST_DATABASE_URL` | Integration tier. Point it at a **scratch database**. |
| `TEST_REDIS_URL` | Integration tier. Point it at a **scratch database index** (`/15`); the fixtures call `FLUSHDB` between tests. |

## Frontend

Built by Vite, so these are baked in at build time.

| Variable | Default | Notes |
|---|---|---|
| `VITE_API_BASE` | `/api/v1` | Leave it relative and let the dev proxy / nginx handle routing. Set it only for a cross-origin deployment, and then set `CORS_ORIGINS` to match. |
| `API_ORIGIN` | `http://127.0.0.1:8000` | Where the Vite dev proxy forwards `/api`. Dev only. |

## Production checklist

- [ ] `ENV=production`, `DEBUG=false`, `JSON_LOGS=true`
- [ ] `SECRET_KEY` generated, stable, shared across replicas
- [ ] `BOOTSTRAP_API_KEY` **unset**; real keys minted via `POST /keys`
- [ ] `DATABASE_URL` and `REDIS_URL` both set — without them you have no durability and no limits
- [ ] `LIMITS__*` sized for your tenants; `LIMITS__RUN_TIMEOUT_S` below your proxy's read timeout
- [ ] `BUDGET__TENANT_USD_PER_DAY` and `BUDGET__GLOBAL_USD_PER_DAY` set, your own tenant exempted in `BUDGET__TENANTS`
- [ ] Proxy buffering **off** on `/api/`, read timeout above the longest run (see `web/nginx.conf`)
- [ ] Schema migrations run at boot under an advisory lock, so parallel workers are safe; for large fleets run them as a one-shot step instead
- [ ] Redis `maxmemory-policy noeviction` — evicting a run stream mid-stream looks like a hang
