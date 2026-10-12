# API

Base path `/api/v1`. Auth is `Authorization: Bearer ak_…` (or `X-API-Key`).
Every error is `application/problem+json` (RFC 9457) with a stable `type` slug
and the `request_id`, which also comes back in the `X-Request-ID` header.

```json
{
  "type": "https://errors.waypoint.local/rate-limited",
  "title": "Too Many Requests",
  "status": 429,
  "detail": "Rate limit of 60/min exceeded.",
  "request_id": "0f0e…",
  "retry_after": 0.86
}
```

Slugs: `bad-request`, `unauthorized`, `forbidden`, `not-found`, `conflict`,
`payload-too-large` (bodies over 8 MB), `rate-limited`, `over-budget` (a daily
spending budget is used up; `scope` is `tenant` or `server`), `validation-failed`,
`upstream-error`, `unavailable`, `internal-error`, `http-error`, and `error`
for anything unexpected. Every `429` carries a `Retry-After` header in whole
seconds as well as `retry_after` in the body. A `validation-failed` problem lists `errors`, each
with a `loc`: a body field by name (`["agent"]`), a query parameter with its
prefix (`["query", "limit"]`).

The OpenAPI document is served at `/api/v1/openapi.json`, with an interactive
page at `/api/v1/docs` (its **Authorize** button takes an API key). Both are
on in production on purpose: they describe the API, not the data, and every
call made from the page still needs a key.

## Health

| Method | Path | Scope | Notes |
|---|---|---|---|
| GET | `/healthz` | — | Liveness. Touches nothing. Use for the container healthcheck. |
| GET | `/readyz` | — | Readiness. Pings Postgres and Redis. `503` when either is `down`. |

```bash
curl -s localhost:8000/api/v1/readyz | jq
# {"status":"ready","checks":{"graphs":"ok","postgres":"ok","redis":"ok"}}
```

`off` means "not configured"; `down` means "configured and unreachable". Only
the second one fails readiness.

## Agents

| Method | Path | Scope |
|---|---|---|
| GET | `/agents` | — |

Returns the registry. The frontend builds its picker from this.

## Threads

| Method | Path | Scope | Notes |
|---|---|---|---|
| POST | `/threads` | `threads:write` | `{"agent":"chat","title":"…"}` → `201` |
| GET | `/threads` | `threads:read` | Yours, newest first. `?limit=` |
| GET | `/threads/<id>` | `threads:read` | |
| DELETE | `/threads/<id>` | `threads:write` | `204` |
| GET | `/threads/<id>/state` | `threads:read` | Head state, or `?checkpoint_id=` for a past one |
| GET | `/threads/<id>/history` | `threads:read` | Checkpoints newest first. `?limit=` (max 100) |
| POST | `/threads/<id>/state` | `threads:write` | Rewrite state — see **Time travel** |

A thread belonging to another tenant returns **404, not 403**: confirming that
an id exists is itself a leak.

## Runs

| Method | Path | Scope | Notes |
|---|---|---|---|
| POST | `/threads/<id>/runs` | `agents:run` | `202` with `run_id`, or `?wait=true` for a blocking `200` |
| GET | `/runs/<run_id>` | `threads:read` | Status record |
| GET | `/runs/<run_id>/events` | `threads:read` | SSE, resumable |
| POST | `/runs/<run_id>/cancel` | `agents:run` | `202`; `409` if already finished |

Request body — exactly one of these three shapes:

```jsonc
{"message": "what time is it in Chicago?"}   // wrapped into the agent's input_key
{"input": {"topic": "tidal power"}}          // raw graph state
{"resume": {"approved": true}}               // continue a graph paused on interrupt()
```

Optional alongside any of them:

```jsonc
{"checkpoint_id": "1f1a…",   // start from this checkpoint instead of the head
 "durability": "async",      // "sync" | "async" | "exit" — how eagerly to persist
 "models": {"critic": "claude-opus-4-5"}}  // optional; role -> model, "default" for the rest
```

`models` picks a model per role for this run (`GET /agents` lists each agent's
`model_roles`). Every name must be in `MODELS_ALLOWED`; an unknown role or a
model outside that list is a `400`. The resolved map for every role is on the
run record (`GET /runs/<id>` → `models`), in the `run.started` event and in
the `?wait=true` result. A resumed run uses its own `models`, not the ones from
the run that paused.

What each model produced is recorded with the output, in the checkpoint, so it
is still there after a reload:

| Where | Field |
|---|---|
| Every AI message in thread state | `model`: as the API reported it (a dated snapshot when the request named an alias), else the configured name |
| Research findings | `model`: the searcher that wrote the answer |
| The research approval interrupt | `value.models`: the run's role -> model map |
| The research briefing's state | `metadata.models` |

### Cost

Every run reports what it cost when it ends, or pauses at an interrupt:
`usage` on the run record (`GET /runs/<id>`), on the terminal event and in the
`?wait=true` result. Priced from `src/waypoint/prices.toml`, the table the
eval scorecards use.

```jsonc
{"cost_usd": 0.2534,          // null if any call was by a model not in prices.toml
 "input_tokens": 123456,      // all input, cached parts included
 "output_tokens": 7890,
 "searches": 12,              // server-side web searches, billed per request
 "roles": {
   "searcher": {"model": "claude-haiku-5-5", "calls": 6, "input_tokens": 98000,
                "output_tokens": 4100, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "searches": 12, "cost_usd": 0.1302},
   "writer":   {"model": "claude-sonnet-5", "calls": 1, "...": "...", "cost_usd": 0.1231},
   "embed":    {"model": "voyageai:voyage-4", "tokens": 40, "cost_usd": 0.0000024},
   "rerank":   {"model": "rerank-3", "tokens": 18400, "cost_usd": 0.00092}}}
```

Each run is priced on its own: a research run's plan (paused at approval) and
its briefing (the resumed run) are two runs with two costs. `embed` tokens are
estimated (characters / 4); langchain's embeddings interface does not return
the provider's count. Rerank tokens are Voyage's own count.

`?wait=true` is a convenience for scripts and tests. Real clients POST, take
the `run_id`, and stream.

### Streaming

```bash
curl -N -H "Authorization: Bearer $KEY" \
     -H 'Last-Event-ID: 0' \
     localhost:8000/api/v1/runs/$RUN/events
```

```
id: 1788197803467-0
event: run.started
data: {"run_id":"…","thread_id":"…","agent":"research"}

event: token
data: {"text":"Grid ","node":"model"}

event: node
data: {"node":"plan","update":{"plan":["…"]}}

event: run.interrupted
data: {"interrupts":[{"id":"6ac8…","value":{"kind":"approve_plan","plan":["…"]}}],"next":["approve"]}
```

| Event | Meaning |
|---|---|
| `run.started` | Accepted and executing; carries the run's `models` map and, with tracing on, its `trace_id` |
| `token` | An LLM token, with the node that produced it |
| `node` | A super-step finished; carries that node's state update |
| `custom` | Whatever a node wrote via `get_stream_writer()`. The research agent writes `{"kind":"research","phase":…}` as each step *starts*: `planning`, `searching` (once per worker, with `question` and `round`), `reviewing` (with `round`), `writing` and `verifying` (the citation check; absent when `RESEARCH__VERIFY_CITATIONS=false`). `node` events say when each finished; the UI's research panel is built from both. |
| `heartbeat` | Keep-alive; ignore |
| `run.interrupted` | Paused for a human — **terminal for this run** |
| `run.completed` / `run.failed` / `run.cancelled` | Terminal |

Every terminal event carries the run's `usage` (see Cost above).

Every frame carries an `id`. Send it back as `Last-Event-ID` (header or
`?last_event_id=`) and the server replays the Redis Stream from exactly there —
no gap, no duplicates. `EventSource` cannot send an auth header, so the
frontend reads the stream with `fetch` instead (`web/src/api/sse.ts`).

## Human-in-the-loop

```bash
# 1. Run. It plans, then stops.
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "$AUTH" \
  -d '{"message":"tidal power"}' | jq .interrupts[0].value

# 2. The pause is durable — read it from anywhere, any time.
curl -s "$BASE/threads/$TID/state" -H "$AUTH" | jq '{next, interrupts}'

# 3. Resume, approving and editing the plan in one go.
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "$AUTH" \
  -d '{"resume":{"approved":true,"plan":["What does it cost?","Who is deploying it?"]}}' | jq .result
```

The value you pass as `resume` is what `interrupt()` returns inside the node.

### Research over your documents

Start a research run with a corpus (see **Corpora**) and each question also
searches it:

```bash
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "$AUTH" \
  -d '{"input":{"topic":"how Waypoint recovers a dropped event stream","corpus_id":"'$CORPUS'"}}' \
  | jq .interrupts[0].value.corpus
# {"corpus_id":"…","name":"waypoint-docs"}
```

The corpus must be the tenant's own or a public one; otherwise, or when
`RAG__EMBEDDINGS_MODEL` is unset, the run fails before planning. The approval
interrupt shows it as `value.corpus`; resume with `"documents": false` to
research the web only. In the briefing's `sources`, a passage has
`"kind": "document"`, a `url` of `doc://<document_id>#<ordinal>` and its
heading as `title`; web sources have no `kind`. The briefing's
`metadata.corpus` names the corpus searched.

## Time travel

```bash
# Every super-step left a checkpoint.
curl -s "$BASE/threads/$TID/history?limit=10" -H "$AUTH" \
  | jq '.checkpoints[] | {checkpoint_id, step: .metadata.step, next}'

# Read state as it was.
curl -s "$BASE/threads/$TID/state?checkpoint_id=$CP" -H "$AUTH" | jq .values

# Rewrite that moment. Returns a new checkpoint on a new branch.
NEW=$(curl -s -X POST "$BASE/threads/$TID/state" -H "$AUTH" -d '{
  "checkpoint_id":"'$CP'",
  "values":{"messages":[{"role":"user","content":"ask it differently"}]}
}' | jq -r .checkpoint_id)

# Continue down the branch.
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "$AUTH" \
  -d '{"input":{},"checkpoint_id":"'$NEW'"}' | jq .result
```

`as_node` in the body makes the write look as though that node produced it,
which controls where execution picks up.

## Memories

| Method | Path | Scope | Notes |
|---|---|---|---|
| GET | `/memories` | `threads:read` | `?q=` semantic search when `EMBEDDINGS_MODEL` is set |
| POST | `/memories` | `threads:write` | `{"fact":"prefers metric units"}` |
| DELETE | `/memories/<key>` | `threads:write` | |

Namespaced `("memories", tenant_id, user_id)` and cross-thread: what the agent
learns in one conversation is loaded into the next.

## Corpora

Named sets of a tenant's documents for retrieval (design:
[design/rag.md](design/rag.md)). `503` on every route when
`RAG__EMBEDDINGS_MODEL` is unset.

| Method | Path | Scope | Notes |
|---|---|---|---|
| POST | `/corpora` | `corpora:write` | `{"name":"my-docs"}`; `409` if the tenant has one by that name |
| GET | `/corpora` | `threads:read` | Your corpora, then public ones, with document and chunk counts |
| GET | `/corpora/<id>` | `threads:read` | |
| DELETE | `/corpora/<id>` | `corpora:write` | Deletes its documents and chunks. Public corpora: `404` |
| POST | `/corpora/<id>/documents` | `corpora:write` + `threads:write` + `agents:run` | Multipart `files` (`.md`, `.html`, `.pdf`, `.txt`). `202` with an `ingest` run |
| GET | `/corpora/<id>/documents` | `threads:read` | Each with `status` (`pending`, `ready`, `failed`), `error` and `chunks` |

```bash
CORPUS=$(curl -s -X POST $BASE/corpora -H "$AUTH" -d '{"name":"my-docs"}' | jq -r .corpus_id)

# The filename as sent is the document's title and the start of every chunk's
# heading ("docs/DEPLOY.md › 4. Caddy"), so send the path you want shown.
args=(); for f in $(git ls-files 'docs/*.md'); do args+=(-F "files=@$f;filename=$f"); done
curl -s -X POST $BASE/corpora/$CORPUS/documents -H "$AUTH" "${args[@]}" | jq '{run_id, events_url}'
```

Follow the run on its `events_url`: one `custom` event per document and stage
(`parsed`, `chunked`, `embedded`, `stored`, or `failed` with the reason), then
`run.completed` with a summary in `result.metadata`. A file whose bytes are
already in the corpus is listed with `queued: false` and not ingested again;
if every file was a duplicate, `run_id` is `null`. Another tenant's corpus is
`404` on every route. Limits: `RAG__MAX_UPLOAD_MB` per file (`413`), 8 MB per
request, 100 files per request.

`corpora:write` is separate from `threads:write` so that a key can search
corpora without adding to them: the demo's reviewer keys have the research
scopes but not this one. Public corpora (`visibility: "public"`) are
listed for every key and searchable by every research run, and no key can
change them. The demo's `waypoint-docs` is one: this repository's `docs/`,
kept indexed by the service itself (`RAG__SEED_DIR`, CONFIGURATION.md).

## Usage

| Method | Path | Scope | Notes |
|---|---|---|---|
| GET | `/usage?days=1` | `threads:read` | The caller's tenant's spend over the last `days` days (1 to 31, default 1: the last 24 hours) |

```jsonc
{"tenant_id": "acme", "days": 1, "since": "2026-10-10T20:00:00+00:00",
 "runs": 4, "cost_usd": 0.61, "unpriced_runs": 0,
 "input_tokens": 410233, "output_tokens": 23110, "searches": 31,
 "by_agent": {"chat": {"runs": 1, "cost_usd": 0.002},
              "research": {"runs": 3, "cost_usd": 0.608}}}
```

Summed from `run_audit` over runs that started in the window and have ended.
`unpriced_runs` counts runs with no recorded cost: one used a model missing
from the price table, or ran before metering existed. Only the caller's own
tenant: no key sees the server's total or another tenant's spend.

With budgets on (CONFIGURATION.md, Budgets) the response also has the
tenant's `budget_usd_per_day` and `balance_usd`, what is left of it now. The
balance refills continuously and can be below zero after runs that were
already going when it ran out. Both are `null` with budgets off; an exempt
tenant has `"exempt": true` and no budget. Starting a run or uploading
documents needs the balance above zero (and the server's, which no key can
read), else `429 over-budget` with `Retry-After`.

## Keys

| Method | Path | Scope | Notes |
|---|---|---|---|
| POST | `/keys` | `keys:admin` | `201` with the plaintext key — the only time it exists outside the caller |
| DELETE | `/keys/<key_id>` | `keys:admin` | Revoke |

```bash
curl -s -X POST $BASE/keys -H "$AUTH" -d '{
  "tenant_id":"acme","user_id":"alice",
  "scopes":["agents:run","threads:read","threads:write"],
  "label":"alice laptop"
}' | jq -r .api_key
```

Revocation clears the 30-second in-process key cache on the worker that
handled it; other workers pick it up within the TTL.
