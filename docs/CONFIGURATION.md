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
stored. Scopes are `agents:run`, `threads:read`, `threads:write`,
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

The resolved map is fixed when a run starts and recorded on the run
(`GET /runs/<id>` → `models`) and in the research briefing's metadata.

## Infrastructure

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | Postgres DSN. Unset → in-memory checkpointer and store; state dies with the process. |
| `REDIS_URL` | — | Unset → single-process event bus, no rate limits, no shared node cache. |
| `REDIS_SOCKET_TIMEOUT_S` | `30` | Read timeout for every Redis command, including the blocking `XREAD` behind SSE. Must exceed `STREAM__BLOCK_MS` by at least 1s; startup refuses otherwise. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `1` / `10` | One pool serves the checkpointer, the store and this service's own tables. Size it for `max_connections` divided by replicas. |

The Postgres image must be `pgvector/pgvector` if you set `EMBEDDINGS_MODEL`;
plain `postgres` is fine otherwise.

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

Only `openai:` ships installed (`langchain-openai`). Another provider means
adding its package, e.g. `uv add langchain-voyageai` for `voyageai:voyage-3.5`.
Changing model or dims after memories exist requires re-embedding them: the
stored vectors were made by the old model.

## Research agent

| Variable | Default | Notes |
|---|---|---|
| `RESEARCH__WEB_SEARCH` | `true` | Workers use Anthropic's server-side web search (`web_search_20250305`) and cite results. Must be enabled for the organization in the Claude Console, and the model must support it (Claude 4.6 and later). A rejected request fails the run with a message pointing here. `false` -> answers from the model alone, no sources, confidence capped at 0.3. |
| `RESEARCH__MAX_SEARCHES` | `2` | Web searches per worker call (the tool's `max_uses`). Each search's results are input again for every later search in the same call: in the 2026-10-08 evals, calls that made a third search averaged 48k input tokens against 21k for two. |
| `RESEARCH__MAX_ROUNDS` | `2` | Investigation waves, counting the first. `1` disables critic follow-ups. |
| `RESEARCH__MAX_FOLLOW_UPS` | `3` | New questions the critic may add per round. |
| `RESEARCH__VERIFY_CITATIONS` | `true` | After the writer, check every sentence of the briefing against the sources' quoted text. A wrong or missing `[n]` is replaced in code; sentences no source supports go back to the writer's model once, and the revision is kept only if every number it cites is a source. One verifier call per run, plus one writer call when something is unsupported. The briefing's `metadata.verification` counts what changed. |

Cost bound per run: at most `(5 + MAX_FOLLOW_UPS x (MAX_ROUNDS - 1)) x
MAX_SEARCHES` searches, 16 with the defaults. Web search is billed per search
($10 per 1,000 at the time of writing) on top of tokens, and search results
count as input tokens, which is usually the larger cost. A two-question run
with one search each and one follow-up took about 70 seconds.

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

Changing `EVALS__JUDGE_MODEL` or a rubric invalidates its calibration; rerun
`evals calibrate`.

The scheduled eval workflow does not read `.env`: it takes these values from
its own `env` block and API keys from repository secrets (TESTING.md §4,
Scheduled runs).

## Tracing

| Variable | Default | Notes |
|---|---|---|
| `LANGSMITH_TRACING` | `false` | |
| `LANGSMITH_PROJECT` | `waypoint` | |
| `LANGSMITH_API_KEY` | — | Read by LangSmith directly from the environment. |

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
- [ ] Proxy buffering **off** on `/api/`, read timeout above the longest run (see `web/nginx.conf`)
- [ ] Schema migrations run at boot under an advisory lock, so parallel workers are safe; for large fleets run them as a one-shot step instead
- [ ] Redis `maxmemory-policy noeviction` — evicting a run stream mid-stream looks like a hang
