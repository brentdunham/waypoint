# 7. Decision log

Each entry: the situation, what was decided, what else was considered, and
what the decision costs. Entries are grouped by area, not by date. Where a
decision came from a measured failure, the entry says so.

- **Platform**: [D1](#d1-asgi-fastapi-one-event-loop-per-worker) ·
[D2](#d2-a-run-is-not-a-request) · [D3](#d3-redis-streams-for-events-pubsub-for-cancel) ·
[D4](#d4-sse-read-with-fetch-not-eventsource) · [D5](#d5-interrupts-end-the-run) ·
[D6](#d6-identity-in-context-ownership-in-our-own-table) ·
[D7](#d7-one-postgres-pool-pgvector-from-day-one) · [D8](#d8-a-polled-advisory-lock-for-migrations) ·
[D9](#d9-limits-as-lua-scripts-slot-taken-before-202) · [D10](#d10-a-redis-node-cache-with-identity-in-the-key) ·
[D11](#d11-no-job-queue-yet)
- **Agents**: [D12](#d12-a-model-per-role-resolved-once-per-run) ·
[D13](#d13-a-small-model-for-the-searcher) · [D14](#d14-constrained-structured-output-retried-on-parse-errors) ·
[D15](#d15-sources-from-citation-metadata-never-from-prose) ·
[D16](#d16-verify-citations-after-writing-fix-in-code-where-possible)
- **Evaluation**: [D17](#d17-evals-run-in-process-checks-before-the-judge) ·
[D18](#d18-one-criterion-per-judge-call-on-an-anchored-4-point-scale) ·
[D19](#d19-a-calibrated-judge-escalated-per-criterion) ·
[D20](#d20-gate-on-the-baselines-own-spread) · [D21](#d21-record-and-replay-keyed-on-exactly-what-is-sent)
- **Retrieval**: [D22](#d22-passages-retrieved-before-the-worker-call-as-search_result-blocks) ·
[D23](#d23-measure-retrieval-on-quoted-spans-before-any-answer) ·
[D24](#d24-vector-search-then-a-reranker-nothing-else-earned-its-place)
- **Workspace**: [D25](#d25-workspace-is-a-separate-agent-from-chat) ·
[D26](#d26-workspace-shares-chats-memory) ·
[D27](#d27-cited-workspace-replies-go-through-the-research-verifier)

---

## Platform

### D1. ASGI (FastAPI), one event loop per worker

**Context.** A psycopg pool, the async checkpointer, a Redis client and a
running graph all bind to the event loop that created them. The service was
first written on Flask, which is WSGI: `flask[async]` runs each async view in
a fresh event loop, which breaks every one of those objects.

**Decision.** FastAPI on uvicorn workers under gunicorn. The lifespan builds
the runtime on the worker's loop before the first request; handlers are
`async def`; every run is an `asyncio.Task` on the same loop.

**Alternatives.** *Keep Flask* with a daemon thread owning a loop and a bridge
from async generators to sync ones for SSE: it worked, and it was all
plumbing. *LangGraph's Agent Server*, which provides threads, runs and
streaming: less code, but tenancy, auth, limits, error format and the eval
harness's in-process access would be shaped by the server rather than by this
codebase, and those are the parts this project exists to show.

**Consequences.** No bridging code; streaming responses are native. Two
workers by default, because runs are I/O bound. Plain ASGI middleware, since
Starlette's `BaseHTTPMiddleware` breaks ContextVars and streaming.

### D2. A run is not a request

**Context.** Research runs take minutes. Tying a run to the POST that started
it means a dropped connection or proxy timeout loses the work, and a second
tab cannot watch.

**Decision.** `POST` starts a task and returns `202` with a `run_id`; clients
attach to an event stream separately (chapter [2](02-runs.md)).

**Alternatives.** Long-polling the POST; WebSockets. Both still tie the work's
visibility to one connection.

**Consequences.** Any number of watchers, from any worker, after any
reconnect. The run executes on the worker that accepted it, which is what
[D11](#d11-no-job-queue-yet) is about.

### D3. Redis Streams for events, pub/sub for cancel

**Context.** Watchers can be on another worker and can reconnect after
missing events.

**Decision.** Each run's events go to a Redis Stream; the stream id is the SSE
event id. Cancellation is published on a pub/sub channel.

**Alternatives.** Pub/sub for events loses whatever a disconnected client
missed. A Postgres table works but adds write load for every token. Kafka is
far more than this needs.

**Consequences.** Exact `Last-Event-ID` resumption for free; bounded backlog
(`MAXLEN`) and expiry. Pub/sub suits cancel because only the worker that owns
the task needs it, and that worker is listening for as long as the task
exists.

### D4. SSE read with `fetch`, not `EventSource`

**Context.** `EventSource` cannot send an `Authorization` header and cannot be
aborted cleanly.

**Decision.** The frontend reads the stream with `fetch` and parses frames
itself.

**Consequences.** The client owns reconnection, with backoff and
`Last-Event-ID`, and an `AbortController` ties the stream to the component.
A close without a terminal event is a drop, never a failure.

### D5. Interrupts end the run

**Context.** The research plan waits for a person, possibly for days.

**Decision.** `interrupt()` checkpoints and ends the run as `interrupted`. A
resume is a new run.

**Alternatives.** Keeping the task alive and awaiting a signal: holds memory
and a concurrency slot, and dies with the process.

**Consequences.** Pauses survive reloads, deploys and restarts. The node
re-executes from its first line on resume, so side effects go after the
interrupt. A resumed run can choose different models.

### D6. Identity in context, ownership in our own table

**Context.** LangGraph's checkpointer stores state per thread id but has no
notion of an owner; anyone who knows a thread id could resume it. State is
checkpointed forever.

**Decision.** Tenant and user live in the runtime context, never in state or
`configurable`. A `threads` table records each thread's owner and is checked
on every request. Another tenant's thread or run is a 404.

**Consequences.** A replay months later runs as the current caller, not as a
snapshot. Memory namespaces derive from context. A 404 does not confirm that
an id exists.

### D7. One Postgres pool, pgvector from day one

**Decision.** Checkpointer, store, own tables and audit share one pool. The
image is `pgvector/pgvector:pg17`.

**Alternatives.** Separate pools per component: more connections to reason
about, and a second pool appearing under load. Plain Postgres: the store's
semantic search fails the day `EMBEDDINGS_MODEL` is set.

### D8. A polled advisory lock for migrations

**Context.** Workers boot together, and LangGraph's setup is not safe to run
concurrently: the losers die on a duplicate key.

**Decision.** All schema work runs under `pg_try_advisory_lock` in a poll
loop.

**Alternatives.** A blocking `pg_advisory_lock` deadlocks invisibly with
`CREATE INDEX CONCURRENTLY` (details in [6.6](06-security-and-operations.md#66-boot-and-shutdown)).
A separate migration job is better at scale and is the recommendation for a
large deployment; the lock makes the simple path safe.

### D9. Limits as Lua scripts, slot taken before 202

**Context.** Check-then-set across workers lets two callers take the last
slot.

**Decision.** Token bucket and semaphore are each one Lua script. The
concurrency slot is taken before the start request returns. The semaphore
uses Redis server time and a lease.

**Consequences.** Limits hold under concurrency. An over-quota caller gets a
`429` from its own request. A dead worker's slot frees itself.

### D10. A Redis node cache, with identity in the key

**Context.** A per-process cache is cold after each deploy and different on
each worker. LangGraph's cache key is a hash of node input only.

**Decision.** Back the node cache with Redis. A cached node must carry
`tenant_id` (and anything else that should partition answers, such as the
searcher model) in its input; nodes whose output is per user are not cached.

**Consequences.** Search answers are shared within a tenant and model, never
across. A test compiles both graphs with a cache to enforce it.

### D11. No job queue (yet)

**Context.** Runs execute on the worker that accepted them.

**Decision.** No queue for now.

**Consequences.** Simple, and correct up to the point where work must survive
the loss of a worker. Then the run goes on a queue and workers pick it up; the
event bus and audit table already provide the observability half of that.

---

## Agents

### D12. A model per role, resolved once per run

**Context.** Different call sites need different strengths and costs. Nodes
that name models make every change a code change.

**Decision.** Agents declare roles. A run's role-to-model map is resolved once
at start (run request, then `MODELS__<AGENT>__<ROLE>`, then defaults) and put
in the context. Nodes ask their role for a runnable. Requests may choose only
from `MODELS_ALLOWED`.

**Consequences.** Model choice is configuration, per run if needed. Cost and
latency are attributable per role. The cached worker takes its model name
from its input, not the context, so a cached answer never crosses models.

### D13. A small model for the searcher

**Context.** On 2026-10-08 one 8-case, 3-trial eval with every role on Sonnet
5 cost about $18.50, and the searcher was about $15.60 of it: each search's
results are input again for the next search in the same call.

**Decision.** Searcher on Haiku 5.5; at most 2 searches per worker call
(calls with a third search averaged 48k input tokens against 21k for two).
Planner, critic and writer stay on the larger model.

**Consequences.** About $7.60 for the same eval. Haiku cited nothing in about
a quarter of findings, which led to the retrieved-results fallback
([D15](#d15-sources-from-citation-metadata-never-from-prose)) and the
`findings_cited` check.

### D14. Constrained structured output, retried on parse errors

**Context.** With forced tool calls, Sonnet 5 sent a list as a string with a
stray tag inside, which failed validation on every retry and failed whole
runs.

**Decision.** `STRUCTURED_OUTPUT=json_schema` (constrained decoding) by
default; nodes with structured output retry on `ValidationError` and
`OutputParserException`, which LangGraph's default policy does not.

**Consequences.** Malformed output is rare and survivable. A reasoning model
can still exhaust its token budget before writing JSON
([D16](#d16-verify-citations-after-writing-fix-in-code-where-possible)).

### D15. Sources from citation metadata, never from prose

**Context.** A URL in a model's text may be invented; a URL in the API's
citation metadata came back from a search.

**Decision.** Sources come only from citation metadata, with the quoted text
kept as evidence. If the searcher cited nothing, the pages its searches
returned stand in, marked `cited: false` and capped at 0.3 confidence in
code. The source list is appended from metadata, so the writer cannot add one.

**Consequences.** Every listed source was retrieved. Uncited findings are
visible to the critic, the writer and the evals instead of silently passing.
Since 2026-10-10 a reply that cites nothing although it had material to cite
is asked once more first; that took uncited findings from 20 of 277 to 1 of
281 in the eval, for one extra Haiku call in about one worker in twenty.

### D16. Verify citations after writing; fix in code where possible

**Context.** Measured on 2026-10-09: about half the briefings left more than
20% of their claim sentences uncited, and some citations pointed at the wrong
source within the right finding
([case study](../case-studies/grounded.md)).

**Decision.** A `verify` node after the writer. A small model lists sentences
not supported as cited. Wrong and missing citations are fixed in code; only
unsupported sentences go back to the writer's model, one replacement each,
spliced in by index. Malformed verifier output falls back to the unverified
draft.

**Alternatives.** *Better writer prompt*: the writer did not have the quotes,
and asking it to attribute more carefully does not let it check. *Rewrite the
whole briefing*: tried, and the model returned only the rewritten sentences,
dropping coverage from 4 to 1.

**Consequences.** Briefings with 80%+ of claims cited: 48% to 95%; `grounded`
3.67 to 3.86; about $0.02 and 21 s per run. `covers` is about 0.14 lower,
because deleting an unsupported sentence can drop a point the reader wanted.

---

## Evaluation

### D17. Evals run in process; checks before the judge

**Decision.** The harness builds the same graphs the service registers and
runs them in process. Deterministic checks run first; the judge grades only
what code cannot decide.

**Consequences.** Evals measure the agent, not the network. Every model call is
visible to a callback with its role. Properties like "80% of claims carry a
citation" are checks: free, and immune to judge drift.

### D18. One criterion per judge call, on an anchored 4-point scale

**Decision.** Each judge call grades one criterion, reasons before scoring,
and scores 1 to 4 against written anchors; 3 or more passes.

**Alternatives.** A single overall score hides which property regressed, and
lets one answer colour the others. A 10-point scale without anchors is less
repeatable and invites a safe middle.

**Consequences.** More judge calls (criteria x trials x cases), each cheap.
The written reasoning is what makes a bad score diagnosable.

### D19. A calibrated judge, escalated per criterion

**Context.** A judge is a model; its scores are a measurement with error. On
2026-10-09 the cheap judge agreed with human labels on `grounded` only 67% to
75% of the time and missed planted wrong citations.

**Decision.** Measure each criterion's judge against labelled examples. Below
80% pass/fail agreement, try the escalation model; if that fails too, mark the
criterion untrusted rather than report it. Routing is valid only for the
judge model and rubric hash it measured.

**Consequences.** Haiku 4.5 grades four research criteria; Opus 4.5 grades
`grounded` (92%). Labels are a real maintenance cost, and they decide what
"correct" means, so a person reviews each one.

### D20. Gate on the baseline's own spread

**Decision.** A metric regresses when it drops by more than twice the
baseline's trial-to-trial standard deviation, with floors of 0.10 (check pass
rate) and 0.25 (criterion mean). A baseline needs 3 trials. Only a person
accepts a new baseline.

**Alternatives.** Fixed thresholds ("grounded must be at least 3.5") fail on
noise or miss real drops depending on where they are set.

**Consequences.** The gate is as sensitive as the data allows and no more.
One trial against a three-trial baseline is still noisy; paired comparisons
on replayed calls answer "did this change help" more precisely
([5.9](05-evaluation.md#59-replay)).

### D21. Record and replay, keyed on exactly what is sent

**Context.** Five full eval runs were most of a $100 bill.

**Decision.** Store every model call under a hash of case, trial, agent, role,
model, bound setup and input messages. Replay identical calls; run the rest
live and record them.

**Consequences.** A writer change costs the writer alone (about $0.90 instead
of $18.50). Nothing stale can be replayed, because any change to what reaches
the API changes the key. Replayed searches freeze the web as recorded, so
baselines meant to last are checked with `--fresh`, and CI never replays.

---

## Retrieval

Design and measurements: [design/rag.md](../design/rag.md),
[case study](../case-studies/retrieval.md).

### D22. Passages retrieved before the worker call, as `search_result` blocks

**Context.** The research agent should answer from a tenant's documents as
well as the web, with the same citations, verifier and evals.

**Decision.** Each worker retrieves its top passages for its question and
sends them ahead of the prompt as Claude `search_result` blocks, next to the
web search tool. Citations to them come back as `search_result_location`,
become sources with `kind: "document"` and quotes, and share the `[n]`
numbering with web sources. Runs without a corpus send exactly the prompt
they sent before.

**Alternatives.** *A `search_documents` tool loop*: lets the model rephrase
queries, but adds model calls to every worker; revisit if recall on
rephrasing-heavy questions is poor.

**Consequences.** One model call per worker, retrieval deterministic and
replayable. The searcher must attach citations to what it read; when it does
not, the finding is uncited, which became the limit on `grounded` for corpus
cases in phase 4 and is now handled by a retry (D15).

### D23. Measure retrieval on quoted spans, before any answer

**Context.** Answer evals cost dollars per run and mix retrieval with
everything after it.

**Decision.** A separate retrieval eval with no model at query time: a
reviewed gold set whose labels are quoted spans (with alternatives), a
corpus pinned to a git commit, recall@5/8/12/20 and MRR per mode and per
query kind, cost and latency, its own baseline and gate.

**Alternatives.** *Chunk-id labels*: invalid after any chunking change.
*LLM-judged relevance*: costs per run and drifts.

**Consequences.** Ten configurations measured in under ten minutes for about
$0.05. Spans survive re-chunking, so chunk size could be ablated without
relabelling.

### D24. Vector search, then a reranker; nothing else earned its place

**Context.** Hybrid search, contextual retrieval, larger or contextualized
embedding models and other chunk sizes are all commonly recommended.

**Decision.** Vector search over voyage-4 at 400-token chunks, then Voyage
`rerank-3` over the top 50, top 8 to each worker. Hybrid and contextual
retrieval stay as settings, off.

**Alternatives.** Each measured: hybrid lost to vector (MRR 0.62 vs 0.80);
contextual preambles helped keyword search but not vector (0.75); voyage-4-large
matched voyage-4 at twice the price; voyage-context-4 and 250-token chunks
were worse.

**Consequences.** MRR 0.80 to 0.90 and recall@5 0.88 to 0.96 for $0.0007 and
0.2 s per query. With the defaults, `VOYAGE_API_KEY` is required whenever RAG
is on. Better
retrieval did not raise answer scores, which are now limited by citation
attachment.

## Workspace

Decisions from [design/tools.md](../design/tools.md), 2026-10-13.

### D25. Workspace is a separate agent from chat

**Context.** Documents in chat, then MCP tools and approval for tool calls,
were first designed as additions to the `chat` agent.

**Decision.** A new agent, `workspace`, starts from chat's tool loop and
memory and takes all of it. `chat` does not change.

**Alternatives.** Growing `chat`: one agent fewer, but the simplest agent
would become the most complex one, and every change to tools would move its
eval baseline.

**Consequences.** Two agents share a loop; `agents/memory.py` holds what both
use so the copies cannot drift. Workspace has its own dataset, rubric and
baseline.

### D26. Workspace shares chat's memory

**Context.** Workspace keeps long-term memory. Memories are stored under
`("memories", tenant, user)`, with no agent in the key.

**Decision.** Use the same namespace. A preference told to chat holds in
workspace and the other way round.

**Alternatives.** A namespace per agent: memories split by agent, and the
`/memories` API would need an agent filter.

**Consequences.** No schema or API change. A fact remembered in one agent
can change the other's answers, which is what a user expects of "remember".

### D27. Cited workspace replies go through the research verifier

**Context.** Every other cited answer in the product is checked sentence by
sentence before it is shown.

**Decision.** Workspace's `cite` step shapes the reply like a briefing and
calls the same `check_citations`, with its own Haiku `verifier` role. One
call per reply that cites documents, about $0.003 and 5 to 15 seconds.

**Alternatives.** Showing the API's citations unchecked: faster, but the
grounded case study found wrong and missing citations in half the briefings
before verification.

**Consequences.** The verifier moved to `agents/verify.py` unchanged, so
research's prompts, replay keys and fingerprint did not move.


### D28. Anything that could change something waits for a person

**Context.** Workspace calls tools on external MCP servers. A server labels
its tools with hints such as `readOnlyHint`, but the MCP spec calls these
untrusted unless the server is: they are claims a server makes about itself.

**Decision.** A tool runs without asking only when its server is marked
`trusted` and the tool declares `readOnlyHint`, or when the operator sets
`approval: false` for it in `MCP__SERVERS`. Every other call stops the run
with an `interrupt()` before it is made, showing the tool, its server and its
arguments; each call is allowed or declined on its own. Built-in tools never
ask.

**Alternatives.** Asking only for tools flagged as dangerous: one missing flag
runs a write unasked. Asking before every call: read-only lookups would make
the agent unusable.

**Consequences.** Safe by default, and quick for lookups. A declined call is
answered with "Not run: the user declined this call", so the model says so
instead of claiming it was done; the evals check that (`tool_declined`, and
`honest_tools` grades it). The pause is a checkpoint, so it survives a reload
or a restart (tested in the integration tier).

### D29. MCP through `langchain.mcp`, calls through our own tool

**Context.** LangChain 1.4 replaced `langchain-mcp-adapters` with a beta
`langchain.mcp` namespace built on FastMCP.

**Decision.** Use its `MCPAdapter` to discover tools (schemas, annotations,
server identity), pinned below 1.5. Make each call through our own tool over
FastMCP's client, which sends the tenant in the request's `_meta`, applies the
server's timeout, and turns a timeout or an unreachable server into a message
for the model.

**Alternatives.** The older adapter package, which LangChain says to
uninstall. The adapter's own tools: no per-call metadata, no per-server
timeout, and a transport failure fails the whole run.

**Consequences.** One file, `agents/mcp.py`, holds everything that touches the
beta API, so a change there touches one place. Elicitation is off: the
calling client does not offer it.
