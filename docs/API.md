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
spending budget is used up; `scope` is `tenant` or `server`), `over-quota` (a
daily upload limit is used up; see **Corpora**), `validation-failed`,
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

### Workspace over your documents

A workspace thread searches up to five corpora at once, your own and public
ones, in one search (`WORKSPACE__MAX_CORPORA`). A run sends the selection as
`corpus_ids`; the thread keeps it for later turns:

```bash
TID=$(curl -s -X POST "$BASE/threads" -H "$AUTH" -d '{"agent":"workspace"}' | jq -r .thread_id)
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "$AUTH" \
  -d '{"message":"How does Waypoint recover a dropped event stream?","input":{"corpus_ids":["'$CORPUS'","'$MINE'"]}}' \
  | jq -r .result.report
# Clients reconnect with Last-Event-ID and the server replays from there [1]. ...
#
# Sources
# [1] docs/ARCHITECTURE.md › 2. A run is not an HTTP request (document)
```

Send `"input":{"corpus_ids":[]}` to stop searching documents. The older
single `"corpus_id"` is still accepted as a list of one. The same corpus rules
as research apply to each: the tenant's own or a public one, and
`RAG__EMBEDDINGS_MODEL` set, or the run fails before the model is called; so
does selecting more than the limit. The thread's state has `corpus_ids`,
`corpora` (`[{corpus_id, name}]`), `report` (the last
reply as shown, with its `Sources` list), `findings` (one per search the reply
drew on) and `metadata` (`searches`, `document_sources_cited`,
`verification`).

### Approving tool calls

A Workspace reply that calls an MCP tool which could change something pauses
before the call (design T4: everything except a trusted server's read-only
tools, unless `MCP__SERVERS[].approval` says otherwise). The run ends
`interrupted`, and the thread's state has the pending interrupt:

```json
{"kind": "approve_tools", "prompt": "Allow these tool calls?",
 "calls": [{"id": "toolu_01...", "tool": "notes_add", "server": "notes", "remote_tool": "add",
            "description": "Save a new note for the user.", "args": {"text": "deposit by Friday"},
            "trusted": true}]}
```

Nothing has run yet. Resume with a decision per call, or one for all:

```bash
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "$AUTH" \
  -d '{"resume":{"tools":{"toolu_01...":true}}}' | jq .status
# or {"resume":{"approved":false}} to decline everything
```

A call the answer does not mention is declined. A declined call is not made;
the model gets "Not run: the user declined this call" as its result and says
so. The pause lives in the checkpoint, so it survives a reload or a restart,
and any device with the key can answer it. Read-only calls in the same reply
run after the answer, with the approved ones.

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
| POST | `/corpora` | `corpora:write` | `{"name":"my-docs"}`; `409` if the tenant has one by that name, or already owns `UPLOADS__MAX_CORPORA` (3) |
| GET | `/corpora` | `threads:read` | Your corpora, then public ones, with document and chunk counts |
| GET | `/corpora/usage` | `threads:read` | Today's uploads against the daily limits, the corpus cap, and `resets_at` (next midnight UTC). `limited: false` for admin keys |
| GET | `/corpora/<id>` | `threads:read` | |
| DELETE | `/corpora/<id>` | `corpora:write` | Deletes its documents and chunks. Public corpora: `404` |
| POST | `/corpora/<id>/documents` | `corpora:write` + `threads:write` + `agents:run` | Multipart `files` (`.md`, `.html`, `.pdf`, `.txt`). `202` with an `ingest` run |
| GET | `/corpora/<id>/documents` | `threads:read` | Each with `status` (`pending`, `ready`, `failed`), `error` and `chunks` |
| POST | `/corpora/<id>/visibility` | `keys:admin` | `{"visibility":"public"}` publishes one of your own corpora to every tenant; `"private"` takes it back. Another tenant's corpus: `404` |

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
corpora without adding to them. The demo's reviewer keys have it, within
limits ([design](design/tools.md), T13).

**Upload limits.** For every key without `keys:admin`, per tenant per UTC day
(`UPLOADS__*`, CONFIGURATION.md): 10 files, 5 MB and 2,000 chunks, and at
most 3 corpora of its own. Files and megabytes are counted when an upload
arrives, before anything is stored; a request that would pass either limit
stores nothing and gets `429 over-quota`:

```json
{
  "type": "https://errors.waypoint.local/over-quota",
  "status": 429,
  "detail": "This key may upload 10 files a day; 9 are used today and this upload has 2. The limit resets at midnight UTC.",
  "limit": "files",
  "limit_per_day": 10,
  "used": 9,
  "requested": 2,
  "retry_after": 20412.7
}
```

`Retry-After` is the time to the next midnight UTC. A duplicate file is given
back. Chunks are counted by the ingest run once a document is chunked and
before it is embedded: a document that would pass the chunk limit is marked
`failed` with the reason, costs nothing, and the other documents in the
upload still index. The dollar budget applies on top. Admin keys are not
limited, so they can seed global corpora of any size.

Public corpora (`visibility: "public"`) are
listed for every key and searchable by every research and workspace run,
and only the admin who published one can change it (below). The demo's `waypoint-docs` is one: this repository's `docs/`,
kept indexed by the service itself (`RAG__SEED_DIR`, CONFIGURATION.md).

**Global corpora.** An admin makes a corpus every tenant can select by
creating it, uploading while it is private, then publishing it:

```bash
GLOBAL=$(curl -s -X POST $BASE/corpora -H "$ADMIN" -d '{"name":"handbook"}' | jq -r .corpus_id)
curl -s -X POST $BASE/corpora/$GLOBAL/documents -H "$ADMIN" -F "files=@handbook.md"
curl -s -X POST $BASE/corpora/$GLOBAL/visibility -H "$ADMIN" -d '{"visibility":"public"}'
```

A public corpus refuses uploads and deletes through the API; to change it,
set it back to `private`, change it, and publish it again. Publishing runs as
the tables' owner and only for the admin's own tenant, so no tenant role ever
writes a public row ([design](design/tools.md), T12).

## Run timeline

| Method | Path | Scope | Notes |
|---|---|---|---|
| GET | `/runs/<run_id>/trace` | `threads:read` | The run's steps with time, model, tokens and cost. Only with `TRACING__PANEL` on (else 404) |

```jsonc
{"status": "ready", "run_id": "…", "trace_id": "60f9…", "duration_ms": 115230.4, "cost_usd": 0.310202,
 "steps": [
   {"id": "a1…", "parent": null, "kind": "node", "name": "node investigate", "node": "investigate",
    "start_ms": 12.3, "duration_ms": 21544.0, "error": false},
   {"id": "b2…", "parent": "a1…", "kind": "retrieval", "name": "rag.retrieve",
    "start_ms": 18.0, "duration_ms": 380.2, "mode": "hybrid", "hits": 8, "rerank_tokens": 926},
   {"id": "c3…", "parent": "a1…", "kind": "model", "name": "chat claude-haiku-5-5", "role": "searcher",
    "model": "claude-haiku-5-5", "input_tokens": 24489, "output_tokens": 1990, "searches": 1, "cost_usd": 0.0129, "…": "…"}]}
```

`kind` is `node` (a graph step), `model`, `tool` or `retrieval`; a step's
`parent` is the node it ran inside. `start_ms` counts from the start of the
run. Read from the trace backend (Jaeger), which receives spans in batches:
right after a run ends the answer is `{"status": "pending", ...}`, so poll
every couple of seconds. Only the caller's tenant's runs, found through the
run record or `run_audit` (a run is readable as long as its trace is kept, a
week on the demo), and only whitelisted fields: no prompt or document text,
whatever the spans hold. `503` when the trace backend cannot be read.

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

**Tool scopes.** `tools:<server>` lets a key's Workspace runs use the tools of
the MCP server of that name (`MCP__SERVERS`, CONFIGURATION.md); `*` allows
every server. A key without it gets none of that server's tools, so existing
keys get no external tools until granted. `/readyz` lists each server as
`mcp:<name>` (`ok` or `unavailable`); an unavailable tool server never makes
the service unready.
