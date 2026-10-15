# Waypoint

Agent runs that checkpoint, pause for a person, resume days later and rewind.
A FastAPI + LangGraph service and a React 19 TypeScript frontend, on Python 3.14,
with Postgres and Redis. Runs are durable, streamable, resumable, cancellable
and rewindable; each agent role gets its own model, and an eval harness scores
the agents against a baseline.

```
.                    docs/ARCHITECTURE.md  — how and why it fits together
├── src/waypoint     FastAPI + LangGraph service
├── web/             React 19 + Vite + TypeScript frontend
├── tests/           pytest: a unit tier with no infra, an integration tier with
├── evals/           datasets, checks and an LLM judge that score the agents (TESTING.md §4)
├── .github/         scheduled eval workflow, weekly by default
├── compose.yaml     api + web + postgres(pgvector) + redis
└── compose.dev.yaml just postgres + redis, for host-side development
```

| Doc | What is in it |
|---|---|
| [docs/guide/](docs/guide/README.md) | Design guide: diagrams, definitions (evals, rubrics, judges, calibration), and a decision log with alternatives |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The design decisions and why each one is there |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Running it as a public demo on one host: Compose, Caddy, keys, backups |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every environment variable, what it does, what to set it to |
| [docs/API.md](docs/API.md) | Every endpoint, with curl |
| [docs/TESTING.md](docs/TESTING.md) | How to run and extend each test tier |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is done, what is next, and why in that order |
| [docs/NEXT-STEPS.md](docs/NEXT-STEPS.md) | Follow-up fixes, verification, and remaining deployment acceptance |
| [docs/case-studies/grounded.md](docs/case-studies/grounded.md) | Raising citation quality with the eval loop: measure, calibrate the judge, fix, re-measure |
| [docs/case-studies/cost.md](docs/case-studies/cost.md) | Cutting a research run's cost by 38%: where the money goes, three one-trial experiments, a three-trial confirmation, and a check that was counting honest sentences as failures |
| [docs/case-studies/retrieval.md](docs/case-studies/retrieval.md) | Which retrieval techniques earned their place: reranking, contextual retrieval, embedding models, chunk size, top-k, measured on a reviewed gold set |

## What it does

Three agents ship in the registry, chosen to exercise the hard parts:

* **chat** — a tool-using assistant with long-term memory that persists across
  threads, scoped to tenant and user.
* **research** — plans a topic into questions, **stops and waits for a human**
  to approve or edit the plan, fans the approved questions out to parallel
  workers, joins them, and writes a briefing.
* **workspace** — chat over your documents: check the corpora for the thread
  (global ones and your own, uploaded from the sidebar within daily limits)
  and it answers from them through a `search_documents` tool, with `[n]`
  citations that the research agent's verifier checks before you see them.
  Shares chat's memory. Uses tools from configured MCP servers, for keys
  with that server's scope, and pauses for your approval before any call that
  could change something ([design](docs/design/tools.md)). The demo ships a
  notes server for it to use (`waypoint/mcp_demo/notes.py`).

Around them:

* **Runs are not requests.** `POST` a run, get a `run_id` back immediately,
  read the output from an SSE stream — possibly from a different worker,
  possibly after a reconnect. Redis Streams make `Last-Event-ID` exact.
* **Human-in-the-loop.** `interrupt()` checkpoints the graph mid-node. Resume
  it in a second HTTP request, five minutes or five days later.
* **Time travel.** Every super-step leaves a checkpoint. List them, read state
  at any one, rewrite it, and continue down the new branch.
* **Cancellation that reaches the model.** `POST /runs/<id>/cancel` stops
  the graph across workers through Redis. Closing the browser detaches its
  event stream; the run continues.
* **Worker recovery.** A dead worker's run is reclaimed and continued from
  its checkpoint. Shutdown drains, then hands unfinished runs to another
  worker. `sync` durability is the default. One active run is admitted per
  thread; overlapping submissions return `409`.
* **Multi-tenancy that holds.** API keys resolve to a `{tenant, user, scopes}`
  principal; threads are owned; another tenant's thread returns 404, not 403.
* **Limits that hold across workers.** A Lua token bucket and a Lua counting
  semaphore in Redis, so two workers cannot hand out the same last slot.

## Quickstart — everything in Docker

```bash
cp .env.example .env
```

Then fill in the credentials. They are unrelated to each other:

| | Required? | What it is | Where it comes from |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Your **outbound** credential to the Anthropic API. Leaves the process, costs money. | <https://console.anthropic.com> → API keys |
| `BOOTSTRAP_API_KEY` | Yes | This service's **inbound** credential: how *you* authenticate to *it*. | You generate it. See below. |
| `OPENAI_API_KEY` | Only if `EMBEDDINGS_MODEL` is an `openai:` model | Your **outbound** credential to the OpenAI API, used for memory embeddings. Leaves the process, costs money. Startup refuses to run without it when needed. | <https://platform.openai.com/settings/organization/api-keys> |

```bash
# Generate a bootstrap key and paste the output into .env
python -c "import secrets; print(f'ak_{secrets.token_hex(6)}.{secrets.token_urlsafe(32)}')"
```

```bash
docker compose up --build
```

* frontend → <http://localhost:8080>
* API → <http://localhost:8000/api/v1>

Paste the same `BOOTSTRAP_API_KEY` string into the key box in the top right of
the UI. It is stored in that browser's `localStorage`, so you only do it once
per browser.

### About the bootstrap key

Minting a key needs the `keys:admin` scope, so something has to break the
cycle — that is all this is. On boot it is seeded into the `api_keys` table as
tenant/user `bootstrap` with the `*` scope.

There is **no default and no placeholder**: a key printed in `.env.example`
would be a full-access credential known to everyone who has read this
repository, so the app refuses to seed the old placeholder value and logs an
error instead. Only the SHA-256 of your key is stored.

Check which key a running deployment actually accepts — `key_id` is the public
half, the bit between `ak_` and the dot:

```bash
docker compose logs api | grep bootstrap
# bootstrap API key active (key_id=16dd20a2540f) -- unset BOOTSTRAP_API_KEY in production
```

**`401 Unknown or revoked API key`** means the key you sent hashes to nothing.
Compare that `key_id` against the one you are pasting; the usual cause is that
`.env` and the browser hold different keys. Changing `BOOTSTRAP_API_KEY` seeds
an *additional* key rather than replacing the old one, so restart with
`docker compose up -d --build api` and remember the previous key still works
until you revoke it.

### Trading it for real keys

The bootstrap key is a `*`-scope credential sitting in a file. Use it once to
mint scoped keys, then unset it:

```bash
KEY=$(grep '^BOOTSTRAP_API_KEY=' .env | cut -d= -f2-)

curl -s -X POST http://localhost:8000/api/v1/keys \
  -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"tenant_id":"acme","user_id":"alice",
       "scopes":["agents:run","threads:read","threads:write"],
       "label":"alice laptop"}'
```

The response is the only time that key's plaintext exists outside the caller —
store it now. Revoke one with `DELETE /api/v1/keys/<key_id>`.

Just poking around locally and want none of this? Set `AUTH_ENABLED=false` and
every request becomes an anonymous principal with all scopes.

## Quickstart — host development

```powershell
uv self update                  # 0.9+ handles Python 3.14 properly
uv python install 3.14
uv venv --python 3.14 --clear
uv sync
copy .env.example .env

docker compose -f compose.dev.yaml up -d    # postgres + redis only
uv run python -m waypoint                 # API on :8000
# In a second terminal, while Redis is configured:
uv run python -m waypoint.worker          # executes queued runs

cd web; npm install; npm run dev            # UI on :5173, proxies /api
```

On Windows, `python -m waypoint` runs uvicorn on a selector event loop,
because psycopg's async mode cannot use Windows' default one. Running uvicorn
yourself, pass `--loop waypoint.loops:selector_loop` for the same reason.
API docs: <http://localhost:8000/api/v1/docs>.

No Postgres or Redis to hand? It still boots: the checkpointer and store fall
back to memory and the event bus to a single process. You lose durability,
cross-worker streaming and rate limits, and the logs say so at startup.

## Try it without the UI

```bash
KEY='paste-your-api-key-here'
BASE=http://localhost:8000/api/v1

TID=$(curl -s -X POST $BASE/threads -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"agent":"research"}' | jq -r .thread_id)

# Runs until it needs a human, then stops.
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"message":"grid-scale storage"}' | jq

# Approve (optionally editing the plan) and let the fan-out run.
curl -s -X POST "$BASE/threads/$TID/runs?wait=true" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"resume":{"approved":true}}' | jq .result.report
```

## Quality gates

```bash
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                     # strict, clean
uv run pytest                   # unit tier, no infra needed
cd web && npm run build         # tsc --strict then vite build
```

## Adding an agent

1. Write `src/waypoint/agents/graphs/<name>.py` with a
   `build(settings, **compile_kwargs) -> Graph`.
2. Add one `AgentSpec` to `AGENTS` in `src/waypoint/agents/registry.py`.

Routing, the `/agents` endpoint and the frontend picker all read that table,
so there is no second place to update.
