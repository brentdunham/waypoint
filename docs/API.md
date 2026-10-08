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
`rate-limited`, `validation-failed`, `upstream-error`, `unavailable`,
`internal-error`, `http-error`.

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
run record (`GET /runs/<id>` → `models`). A resumed run uses its own `models`,
not the ones from the run that paused.

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
| `run.started` | Accepted and executing |
| `token` | An LLM token, with the node that produced it |
| `node` | A super-step finished; carries that node's state update |
| `custom` | Whatever a node wrote via `get_stream_writer()` |
| `heartbeat` | Keep-alive; ignore |
| `run.interrupted` | Paused for a human — **terminal for this run** |
| `run.completed` / `run.failed` / `run.cancelled` | Terminal |

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
