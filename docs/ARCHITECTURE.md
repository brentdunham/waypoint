# Architecture

The long form, with diagrams, definitions and a decision log, is the
[design guide](guide/README.md).

Every non-obvious thing in this codebase is here, with the reason it exists.
If a decision below looks over-engineered, the paragraph under it is the
failure it prevents.

## The shape

```
browser ──► nginx ──► gunicorn (N workers, UvicornWorker)
                          │
                          ├─ FastAPI, async handlers, one event loop
                          │  per worker; runs are tasks on it
                          │
                          ├──────────────────────────────┐
                          ▼                              ▼
                    Postgres                          Redis
             checkpoints, long-term store,     run event streams, cancel
             threads, api_keys, run_audit      pub/sub, limits, node cache
```

## 1. One event loop per process, owned by the server

`src/waypoint/__init__.py` (the lifespan), `src/waypoint/agents/runtime.py`.

A psycopg pool, an `AsyncPostgresSaver`, a Redis connection and a running
graph all bind to the event loop they were created on. So everything is
created on one loop and stays there: the server's. FastAPI's lifespan builds
the runtime (`start_runtime`) before the first request, every handler is
`async def` and awaits it directly, and every run is an `asyncio.Task` on the
same loop. Shutdown cancels runs in flight and closes the pools, bounded at
20 seconds (`stop_runtime`); gunicorn's graceful timeout is 30.

Until 2026-10 this was Flask, which is WSGI: `flask[async]` runs each
`async def` view in a fresh event loop, which breaks every loop-bound object
above. The workaround was a daemon thread owning one loop per process, sync
views hopping onto it, and a bridge that turned an async generator into a
sync one for SSE. It worked, and it was all plumbing; with an ASGI framework
none of it is needed.

Request plumbing is one plain ASGI middleware (`src/waypoint/middleware.py`):
request id, the access log line, the 8 MB body limit, and a last-resort 500
that still carries `X-Request-ID`. Not Starlette's `BaseHTTPMiddleware`: it
runs the endpoint in a separate task, which loses the log ContextVar bound
here and interferes with streaming responses.

Keep worker counts low (2 is the default). Runs are I/O bound; concurrency
comes from the loop, not from forking.

## 2. A run is not an HTTP request

`src/waypoint/infra/runs.py`.

The naive design ties a graph run to the POST that started it. Then a
four-minute run needs a four-minute request, a dropped connection loses the
work, a proxy timeout kills it, and a second tab cannot watch.

Instead: `POST` starts an `asyncio.Task` on the worker's loop and returns `202`
with a `run_id`. The task writes every event to a **Redis Stream**. Clients
attach separately over SSE — from any worker, any number of times, after any
reconnect.

Redis Streams rather than pub/sub, specifically because `XADD` gives every
event a monotonic id and `XREAD` can start from any id. That is exactly the
`Last-Event-ID` contract, so reconnection is *exact*: no replay of what you
already saw, no gap for what you missed. Pub/sub would have made a dropped
connection lossy. `MAXLEN ~` bounds the backlog and a TTL retires finished
runs.

Two rules keep a *quiet* run's stream alive:

* **The socket timeout must outlast the block.** Followers park in
  `XREAD BLOCK STREAM__BLOCK_MS`. redis-py applies its socket timeout to that
  read as well, and its default (5s) equalled the default block, so any run
  that went 5s without an event, such as research workers searching the web,
  had its stream time out client-side. `REDIS_SOCKET_TIMEOUT_S` is now explicit
  (30s) and settings refuse a block that is not at least 1s shorter.
* **Losing the stream is not losing the run.** If following fails, the SSE
  handler ends the response without a terminal event; it never invents
  `run.failed`. The client treats a close without a terminal event as a drop
  and resumes with `Last-Event-ID`. It reports an error only after its retries
  run out, and even then says the run may still be going.

`tests/test_stream_resilience.py` covers both.

Cancellation crosses workers over pub/sub, which is the right tool there:
`POST /runs/<id>/cancel` publishes the id, every worker's listener checks its
local task table, and whichever one owns it calls `.cancel()`.

## 3. Interrupts are the point

`src/waypoint/agents/graphs/research.py`.

`interrupt()` checkpoints the graph mid-node and raises out. The run reaches a
terminal state — `run.interrupted` — with the payload the node asked about.
Nothing is held in memory anywhere.

That is what makes the approval card in the UI survive a page reload, a
deploy, or a week. Resuming is an ordinary second request carrying
`{"resume": {...}}`, which becomes the return value of `interrupt()` when the
node is re-entered from the top.

Two consequences worth internalising:

* **The node runs again from its first line.** Everything before the
  `interrupt()` call re-executes on resume. Keep side effects after it.
* **The resume value is user input.** It is validated like any other body.

## 4. Reducers, `Send`, `defer`, and a bounded loop

```
plan -> approve (interrupt) -> investigate x N (Send) -> critique (defer)
                                    ^                        |
                                    +---- follow-ups --------+--> synthesize -> verify
```

`Send("investigate", payload)` fans out one worker per question, concurrently.
They all write `findings`, so `ResearchState` gives it a reducer. Without one,
parallel writes to the same key are last-write-wins and you silently keep one
answer out of five. The reducer is `merge_findings`, an upsert by question,
not `operator.add`: the critic writes the same findings back with a confidence
and a note, and appending would duplicate every one of them.

`critique` is `defer=True`, so it runs once per wave, after every worker has
landed. It scores each finding and may send up to `RESEARCH__MAX_FOLLOW_UPS`
new questions back out through the same `Send` path. The loop is bounded twice:
`RESEARCH__MAX_ROUNDS` waves at most, and on the last round the critic is told
to stop and its follow-ups are dropped in code anyway. Follow-ups that repeat
an asked question are filtered before they cost a search.

## 4a. Structured output, and sources from metadata

Planner and critic return Pydantic objects via `with_structured_output`; no
node parses model prose. By default that uses Anthropic structured outputs
(`STRUCTURED_OUTPUT=json_schema`), which constrains decoding to the schema.
The forced-tool-call method it replaced let claude-sonnet-5 send a list as a
string, which the evals caught failing whole runs. Both nodes also retry on a
parse or validation error: LangGraph's default retry policy never retries a
`ValueError`, and those are both `ValueError`s. `ScriptedChatModel` answers those calls from
`structured_scripts`, keyed by schema name, so tests can steer the critic
(e.g. force a follow-up) without a network.

Workers answer with Anthropic's server-side web search. A finding's sources
come from the `citations` on the reply's text blocks (`agents/citations.py`),
never from URLs in the prose: a URL in citation metadata came back from a
search, a URL in the text may have been invented. Workers are not streamed,
so citations arrive whole rather than reassembled from deltas.

A worker that searched but attached no citations (claude-haiku-5-5 did in
about a quarter of findings, 2026-10-08) gets the first five pages its searches
returned as sources, with `cited: false`. The URLs are still real search
results; what is lost is which sentence each one supports. The critic and the
writer are told, and the `findings_cited` eval check counts them.

Two rules are enforced in code, not left to the critic's judgment: a finding
with no sources, or only uncited ones, is capped at 0.3 confidence, and the source list at the end of
the briefing is built from metadata and appended, so the writer cannot add a
source that was never retrieved.

## 4b. A model per role, resolved once per run

`src/waypoint/agents/models.py` (`resolve_models`, `ModelRouter`).

Graph nodes never name a model. Each agent declares its roles
(`AgentSpec.model_roles`), and each node asks a `RoleModel` for the runnable
of its role in the current run. The map from role to model is resolved once,
when the run starts, from the run request, `MODELS__*` and `MODEL`, and put in
`AgentContext.models`; it is part of the context because it is a per-run fact
like the tenant, not state to checkpoint and not plumbing. Models are built
lazily and cached by name, so choosing a different model per run costs one
construction per process.

Two consequences:

* **The node cache.** A cache key hashes a node's input only (§9). A cached
  `investigate` answer must not be served to a run that asked for a different
  searcher model, so `WorkerInput` carries the model name and the worker uses
  that name, not the context. Same pattern as `tenant_id`.
* **Attribution.** Each role's runnable carries `model_role` and `model_name`
  as run metadata, so callbacks and traces see which role made a call without
  knowing the graph's node names. The eval harness uses this to report tokens
  and latency per role.

Run requests can pick models only from `MODELS_ALLOWED`, which defaults to the
models already configured: a tenant can choose between approved models, not
spend on new ones.

## 4c. Verify: check each sentence against the quotes

The search API returns, with each citation, the text the answer quoted
(`cited_text`); findings keep up to three quotes per source. After the writer,
`verify` splits the briefing into sentences and asks the verifier role
(Haiku 5.5 by default) which ones are not supported as cited, with one of
three verdicts. The two common ones are fixed in code, not by a model:
`wrong_source` and `needs_citation` replace the sentence's `[n]` marks with the
numbers the verifier gives, if they are real sources. Only `unsupported`
sentences go back to the writer's model, which returns a replacement per
sentence (or an empty one to delete it). The replacement is spliced in by
index, so a revision cannot touch any other sentence. The first version
asked for the whole revised briefing, and coverage fell from 4 to 1 when the
model returned only the rewritten sentences.

The briefing message keeps its id, so the verified text replaces the draft in
the thread instead of adding a second reply. `metadata.verification` counts
sentences checked, re-cited, unsupported and rewritten. Verification must
never cost the user a briefing: malformed verifier output (Haiku can spend its
whole token budget reasoning) is retried once, and then the draft ships with
`verification.skipped`. `RESEARCH__VERIFY_CITATIONS=false` removes the node.
The evidence for it is in [case-studies/grounded.md](case-studies/grounded.md).

The checking itself lives in `agents/verify.py` (`check_citations`), shared
with the workspace agent (4d). It moved there from `graphs/research.py` with
no change to a prompt, a schema or a decision, so research briefings, replay
keys and eval fingerprints did not change.

## 4d. Workspace: chat over documents, with the same citations

`agents/graphs/workspace.py` is chat's tool loop plus documents, kept as a
separate agent so chat stays the simple one ([design](design/tools.md), W1):

```
select_corpus -> recall_memories -> model -> [approve_tools] -> tools -> model
                                      |
                                      v
                                    cite -> END
```

* **The thread's corpora are state.** A run's input may carry `corpus_ids`
  (the older single `corpus_id` becomes a list of one); the checkpoint keeps
  them, so later turns search the same documents until a run sends another
  list (or `[]`). `select_corpus` resolves each for the caller's tenant when
  the selection changes and fails the run on a corpus the tenant cannot read,
  or on more than `WORKSPACE__MAX_CORPORA`, before any search.
* **One search across them.** `retrieve` fetches each method's candidates per
  corpus, merges them by score, and fuses and reranks once, so five corpora
  cost what one does: one embedding, one rerank. Research passes one id and
  gets exactly what it got before.
* **Global corpora are published, not written.** An admin uploads into a
  private corpus and then publishes it (`POST /corpora/{id}/visibility`).
  The store runs that one update as the tables' owner, for the admin's own
  tenant only, so the tenant role still never writes a public row (8a).
* **`search_documents` returns `search_result` blocks.** The tool takes only a
  query; the corpus comes from state and the tenant from the run's context.
  It is bound to the model only when the thread has documents. Claude cites
  `search_result` blocks in a tool result with the same
  `search_result_location` citations the research worker gets.
* **`cite` makes the reply look like a briefing.** It numbers the cited
  passages in the order the reply cites them, puts `[n]` after each cited
  span, appends a `Sources` list, and records one finding per search (query,
  passages, quotes). Then `check_citations` verifies it exactly as research's
  `verify` does, with the `verifier` role on Haiku and unsupported sentences
  revised by the assistant's model. The reply keeps its message id, so the
  checked text replaces the streamed draft. Marks the model typed itself are
  stripped first: only citations the API attached are real.
* **Memory is chat's.** `agents/memory.py` holds the recall node and the
  prompt block for both graphs, and the namespace has no agent in it, so a
  fact told to chat holds in workspace (W2).
* **MCP tools come from configuration, per key.** `agents/mcp.py` keeps a
  catalog of each `MCP__SERVERS` entry's tools, fetched through
  `langchain.mcp`'s `MCPAdapter` in the background at boot and every
  `MCP__REFRESH_S`; a server that is down has no tools until it answers, and
  `/readyz` shows it as `mcp:<name>: unavailable` without failing. A run is
  offered the tools of servers whose `tools:<server>` scope its key holds
  (the scopes travel in the run's context). The model is bound per tool set;
  with no MCP tools it is the same binding as before, so eval replay keys did
  not move. The `tools` node runs only what this run was offered, so a call to
  anything else comes back as an unknown tool.
* **Anything that could change something waits for a person.** When a reply
  calls a tool that needs approval (all but a trusted server's read-only
  tools, unless `approval` in the server's config says otherwise), the route
  goes through `approve_tools`, whose `interrupt()` checkpoints the run and
  stops it before any call is made (section 3). The resume answers per call;
  `tools` then runs the approved calls and answers each declined one with
  "Not run: the user declined this call", which the model reports. Read-only
  calls in the same reply wait for the answer and then run.
* **The demo server is outside the agent.** `waypoint/mcp_demo/notes.py` is
  an ordinary FastMCP server that knows nothing about Waypoint but the
  tenant in `_meta`: the `notes` compose service runs it from the api's
  image, on the compose network only, behind a shared token, with notes in
  SQLite. The evals run the same server in process with an in-memory store.
* **Each MCP call is ours, not the adapter's.** Tools are named
  `{server}_{tool}`. A call sends the tenant and user in the request's
  `_meta` and the server's token as a bearer header, has the server's
  timeout, and turns a timeout, an unreachable server or an `isError` result
  into a tool result the model reads, never a failed run. Past
  `MCP__MAX_CALLS_PER_RUN` calls in one answer, the next ones are not made and
  the model is told why. Elicitation is off: the calling client offers no
  elicitation capability. Tool spans carry `waypoint.tool.server`.

## 5. Context, state, config are three different things

`src/waypoint/agents/context.py`.

* **state** — mutated by nodes, checkpointed, part of the conversation.
* **context** — immutable per-run facts: tenant, user, request id.
* **config** — plumbing: thread id, recursion limit, callbacks.

Identity goes in *context*, never state. If it were state it would be
checkpointed into the thread, and a replay six months later would run with a
snapshot of who you used to be. Memory namespaces derive from context, which
is what keeps one tenant's memories out of another's.

## 6. Ownership is ours, not LangGraph's

`src/waypoint/infra/threads.py`.

The checkpointer stores thread *state*; it has no concept of who owns a
thread. A thread id is therefore a capability — anyone who knows it can resume
the conversation. So ownership lives in our own `threads` table and is checked
on every request that names one.

Another tenant's thread returns **404, not 403**. A 403 confirms the id exists,
which is a real leak when ids appear in URLs and logs.

## 7. Limits in Lua, because check-then-set races

`src/waypoint/infra/ratelimit.py`.

Two workers reading a counter, both seeing "3 of 4 used", both proceeding, is
the classic way concurrency limits fail under exactly the load they exist for.
Both the token bucket and the concurrency semaphore are single Lua scripts, so
the check and the write are one atomic operation.

The semaphore is a sorted set scored by Redis server time (not worker clocks),
with a lease: entries older than the lease are swept on every acquire, so a
worker that dies mid-run cannot leak a slot forever. And the slot is taken
*before* the POST returns, so an over-quota caller gets a clean `429` instead
of a run that mysteriously fails later.

## 8. Postgres carries five things on one pool

The checkpointer, the long-term store, our own tables, the audit log and the
RAG corpora all share one `AsyncConnectionPool`. One place to size, one
number to reason about against `max_connections`, and no surprise second pool
appearing under load.

`pgvector/pgvector:pg17` rather than plain `postgres` because the store's
semantic search needs the extension the moment `EMBEDDINGS_MODEL` is set —
and discovering that in production is a worse afternoon than pulling a
slightly larger image today. Since the RAG tables it is required outright:
every boot creates the `vector` extension.

### 8a. Corpora are isolated by row-level security, through a role

Corpora, documents and chunks (design: [design/rag.md](design/rag.md)) carry
a `tenant_id`, and every query in `rag/store.py` filters on it. That is one
guard. The second is Postgres row-level security, and it needs more than
policies: the service connects as the tables' owner (a superuser in the
compose images), and both bypass RLS. So each store call opens a transaction
that does `set local role waypoint_tenant` and
`set_config('app.tenant_id', ..., true)`. The role owns nothing and cannot
bypass RLS, so the policies filter every row it touches even if a `where`
clause is wrong. Both settings are transaction-local, which is what makes them
safe on a pooled connection. Writes are allowed only into the tenant's own
*private* corpora, checked with `exists` against `corpora`, because foreign
keys are checked without RLS. `tests/test_rag_pg.py` tests each guard on its
own, including that the owner sees everything (the reason for the role).

Public corpora are therefore written only as the owner, and only by
`rag/seed.py`, which keeps `RAG__SEED_DIR` (the demo's `docs/`) indexed as
`waypoint-docs` under the system tenant `waypoint`. It never handles tenant
input. Through the API, changing any corpus also needs the `corpora:write`
scope. The demo's reviewer keys have it, bounded by the upload limits
(`infra/quota.py`, `UPLOADS__*`): at most 3 corpora per tenant and 10 files,
5 MB and 2,000 chunks per UTC day, counted in Redis with one Lua script so a
refused request counts nothing. Files and bytes are taken at upload; chunks
by the ingest run, before embedding, so a document over the limit fails
alone and costs nothing. Admin keys are not limited.

## 9. The cache is Redis, not a dict

`src/waypoint/infra/cache.py`.

A per-process node cache is a coincidence, not a cache: it is cold after every
deploy and each worker keeps its own. Backing LangGraph's `BaseCache` with
Redis makes a `CachePolicy` mean something across restarts and replicas. A
poisoned entry is logged and dropped rather than raised — a cache must never
be able to fail a run.

**A cache key is a hash of the node's input and nothing else.** The runtime
context, which is where tenant and user live, is not part of it. So a cached
node must either take identity in its input or not be cached:

* `research.investigate` carries `tenant_id` in `WorkerInput`, so answers are
  shared within a tenant and never across one. That is safe to put in a `Send`
  payload because a thread belongs to one tenant for its whole life.
* `chat.recall_memories` is not cached. Its output is per-user memories, and a
  cached read would also hide a fact saved with `remember` seconds earlier.

`tests/test_isolation.py` compiles both graphs with an in-memory cache and
checks this, because the unit tier otherwise runs with no cache at all.

## 10. Errors are RFC 9457, always

`src/waypoint/errors.py`. Every failure that leaves the service is
`application/problem+json` with a stable `type` slug and the request id.
Clients branch on the slug; humans grep the id. Validation errors carry the
pydantic error list, with `include_input=False` so a malformed body is never
echoed back into a log aggregator.

## 11. The frontend reads SSE with `fetch`

`web/src/api/sse.ts`.

`EventSource` cannot send an `Authorization` header and cannot be aborted
cleanly. So the client reads the stream itself: `fetch` → `TextDecoderStream`
→ frame parser. The cost is owning reconnection; the benefit is owning
`Last-Event-ID`, which is what makes reconnection lossless. `AbortController`
ties the whole thing to component lifetime, so unmounting cancels the run.

## 12. Migrations run under a *polled* advisory lock

`src/waypoint/infra/db.py::advisory_lock`.

Gunicorn boots N workers simultaneously and every one of them calls
`checkpointer.setup()` and `store.setup()`. LangGraph's migration step is a
plain `INSERT` into its own version table with no guard, so the losers die on
a duplicate key.

The fix is a Postgres advisory lock around all schema work — but it has to be
`pg_try_advisory_lock` in a poll loop, **not** a blocking `pg_advisory_lock`,
and that distinction is the whole trick. LangGraph's setup runs `CREATE INDEX
CONCURRENTLY`, which waits for every in-flight transaction to finish. A session
parked inside a blocking `pg_advisory_lock` *is* an in-flight transaction
holding a virtualxid. So the worker holding the lock waits for the worker
waiting for the lock. Postgres does not classify that as a deadlock — CIC's
wait is not in the deadlock graph — so neither side ever times out and the
whole service hangs at boot with no error.

Polling leaves the waiter genuinely idle between attempts, holding no
transaction and blocking nothing. `tests/test_integration.py` boots three apps
concurrently against a dropped schema to keep it that way.

For a large deployment, prefer running migrations as a separate one-shot step
and letting workers assume the schema exists. The lock is what makes the
simple path safe.

## 13. Two container details that are easy to get wrong

`WORKDIR /waypoint` creates the directory as **root**, and `COPY --chown`
only changes what it copies, so the image explicitly `install -d -o waypoint
-g waypoint /waypoint` before the copy. Otherwise the unprivileged user cannot
write to its own home.

gunicorn 26 opens a control socket at `$HOME/.gunicorn/gunicorn.ctl` by
default, and the `waypoint` user's home *is* `/waypoint`. Nothing here uses `gunicorn ctl`, so the
CMD passes `--no-control-socket`. If you want it, point it somewhere writable
with `--control-socket /tmp/gunicorn.ctl` instead.

## What is deliberately missing

* **A queue.** Runs execute in the worker that accepted them. That is right up
  to the point where you want work to survive a pod eviction — then put the
  run in a real queue and have workers pick it up. The bus and the audit table
  already give you the observability half of that.
* **Auth beyond API keys.** Swap `KeyStore.resolve` for JWT verification; the
  `Principal` everything else depends on does not change.
* **Per-tenant model routing.** Models are chosen per agent, role and run
  (§4b). A tenant-level default map needs somewhere to store it per tenant;
  it would slot in between the run request and `MODELS__*` in
  `resolve_models`.
