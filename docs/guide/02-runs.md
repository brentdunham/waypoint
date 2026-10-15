# 2. Runs

`src/waypoint/infra/runs.py`, `src/waypoint/infra/bus.py`, `src/waypoint/infra/queue.py`,
`src/waypoint/worker.py`, `src/waypoint/api/runs.py`.

A **run** is one execution of an agent's graph on a thread: from a start
request until the graph finishes, fails, is cancelled, or pauses for a person.
A **thread** is the conversation the run belongs to; it outlives any one run
(chapter [3](03-state-and-persistence.md)).

The design rule for this chapter: **a run is not an HTTP request.** The naive
design runs the graph inside the POST that started it. Then a four-minute
research run needs a four-minute request, a dropped connection loses the work,
a proxy timeout kills it, and a second browser tab cannot watch it. Waypoint
separates starting a run from watching it.

## 2.1 Lifecycle

```mermaid
stateDiagram-v2
    [*] --> running: POST /threads/{id}/runs<br/>202 + run_id
    running --> completed: graph reached END
    running --> interrupted: a node called interrupt()
    running --> failed: exception, or LIMITS__RUN_TIMEOUT_S (300 s)
    running --> cancelled: POST /runs/{id}/cancel
    completed --> [*]
    failed --> [*]
    cancelled --> [*]
    interrupted --> [*]
    note right of interrupted
        Terminal for this run. The pause lives
        in the thread's checkpoint. A later
        POST with {"resume": ...} starts a
        new run that continues it.
    end note
```

`running` starts when the API accepts the run, so it covers the time the run
waits on the queue for a worker as well as the time a worker drives it
([2.6](#26-the-run-queue-and-the-workers)).

`interrupted` is a terminal state on purpose. A paused run holds nothing: no
task, no connection, no memory. Resuming is a new run with its own id, its own
event stream and, if the caller asks, its own model choices.

## 2.2 Starting a run

```mermaid
flowchart TD
    post(["POST /threads/{id}/runs"]) --> own{Thread exists<br/>and is this tenant's?}
    own -- no --> e404[404 not-found<br/>also for another tenant's thread]
    own -- yes --> usr{Caller's own thread,<br/>or tenant:admin scope?}
    usr -- no --> e403[403 forbidden]
    usr -- yes --> body{At least one of<br/>message, input, resume?<br/>resume takes precedence}
    body -- no --> e400[400 bad-request]
    body -- yes --> size{Input within<br/>LIMITS__MAX_INPUT_CHARS?}
    size -- no --> e400s[400 bad-request]
    size -- yes --> models{Requested models<br/>valid roles, in MODELS_ALLOWED?}
    models -- no --> e400b[400 bad-request]
    models -- yes --> rpm{Token bucket<br/>LIMITS__RPM per tenant}
    rpm -- empty --> e429[429 rate-limited<br/>with retry_after]
    rpm -- ok --> usd{Tenant and server<br/>budget balances above zero?}
    usd -- no --> e429c[429 over-budget<br/>Retry-After]
    usd -- yes --> slot{Concurrency slot<br/>LIMITS__CONCURRENT_RUNS}
    slot -- none free --> e429b[429 rate-limited]
    slot -- taken --> meta[Write run record to Redis,<br/>audit row to Postgres,<br/>append run.started]
    meta --> task[XADD the job to runs:queue<br/>a worker drives it, 2.6]
    task --> wait{?wait=true}
    wait -- no --> r202[202 run_id, events_url, cancel_url]
    wait -- yes --> r200[block until terminal event,<br/>200 with the result]
```

The concurrency slot is taken **before** the POST returns. An over-quota
caller gets a clean `429` from the request it made, instead of a run that
fails later for a reason it cannot see. If anything between taking the slot
and queueing the run fails, the slot is released at once rather than left for
the lease to expire (`RunManager._abandon`). The worker that drives the run
gives the slot back when the run ends.

`?wait=true` exists for scripts and tests. Real clients take the `run_id` and
stream.

## 2.3 Events and the stream

The run's task iterates the graph with three LangGraph stream modes and turns
each chunk into an **event** appended to a Redis Stream keyed by run id:

| LangGraph stream mode | Event | Payload |
|---|---|---|
| `messages` | `token` | text, and the node that produced it |
| `updates` | `node` | the node's state update, once that node finishes |
| `custom` | `custom` | whatever a node wrote with `get_stream_writer()`; the research agent reports each step as it *starts* |
| (from the run itself) | `run.started`, `run.interrupted`, `run.completed`, `run.failed`, `run.cancelled` | lifecycle; the last four are terminal |
| (from the reader) | `heartbeat` | keep-alive every 15 s (`STREAM__HEARTBEAT_S`) |

`node` events alone would leave a progress panel empty for the minute that
research workers spend searching, because a node's update arrives only when
it ends. The `custom` phase events (`planning`, `searching`, `reviewing`,
`writing`, `verifying`) fill that gap.

### Why Redis Streams

```mermaid
flowchart LR
    subgraph w1[worker 1]
        task[run task] -->|XADD| s
    end
    s[("Redis Stream run:{id}:events<br/>ids 1788...-0, -1, -2 ...<br/>MAXLEN ~2000, TTL 1 h after end")]
    subgraph w2[worker 2]
        sse1[SSE reader A] -->|"XREAD BLOCK 5000<br/>from last id"| s
    end
    subgraph w1b[worker 1]
        sse2[SSE reader B] -->|"XREAD from 0"| s
    end
```

`XADD` gives every event a monotonic id, and `XREAD` can start from any id.
That is exactly the SSE `Last-Event-ID` contract. Pub/sub would deliver only
to whoever is subscribed at that moment, so a dropped connection would lose
events. A list would need its own id scheme. Streams also bound the backlog
(`STREAM__MAXLEN`, 2000 events) and expire with the run (`STREAM__TTL_S`, an
hour after it ends).

## 2.4 Reconnection

```mermaid
sequenceDiagram
    participant C as Client (fetch + TextDecoderStream)
    participant A as api (any worker)
    participant R as Redis Stream

    C->>A: GET /runs/{id}/events, Last-Event-ID: 0
    A->>R: XREAD from 0
    R-->>A: events 1..41
    A-->>C: id: 41 ...
    Note over C: network drops, no terminal event seen
    C->>C: back off 0.5 s, 1 s, 2 s ... capped at 8 s
    C->>A: GET /runs/{id}/events, Last-Event-ID: 41
    A->>R: XREAD from 41
    R-->>A: events 42..
    A-->>C: id: 42 ...  (no gap, no duplicate)
    A-->>C: event: run.completed
    Note over C: terminal event: stop
```

Two rules keep this honest (`tests/test_stream_resilience.py`):

* **A stream that stops is not a run that failed.** If reading the stream
  fails, the server ends the SSE response *without* a terminal event; it never
  invents `run.failed`. The client treats a close without a terminal event as
  a drop and reconnects. After its retries run out it reports an error that
  says the run may still be going.
* **The socket timeout must outlast the block.** A reader parks in
  `XREAD BLOCK` for up to `STREAM__BLOCK_MS` (5 s). redis-py applies its
  socket timeout to that read, and its default (5 s) equalled the block, so
  any run quiet for 5 s, such as workers searching the web, had its stream
  time out. `REDIS_SOCKET_TIMEOUT_S` is 30 s and settings refuse a block that
  is not at least 1 s shorter.

The browser cannot use `EventSource`, which cannot send an `Authorization`
header and cannot be aborted cleanly. `web/src/api/sse.ts` reads the stream
with `fetch` and parses frames itself, which is what lets it own
`Last-Event-ID` and tie the stream to a component's lifetime.

## 2.5 Cancellation across processes

```mermaid
sequenceDiagram
    participant C as Client
    participant A as api process
    participant R as Redis
    participant W as worker (drives the run)

    C->>A: POST /runs/{id}/cancel
    A->>R: SET run:{id}:cancel
    A->>R: PUBLISH runs:cancel run id
    A-->>C: 202
    R-->>W: run id
    W->>W: inside the graph? task.cancel()
    W->>W: append run.cancelled, release slot, audit
```

Pub/sub is the right tool for a run being driven, unlike for events: a cancel
only matters to the worker running the task, and that worker is subscribed
for as long as the task exists. The flag covers a run no worker is driving
yet: a worker checks it when it takes a run off the queue and again as the
run enters its graph, and the API sets it before it publishes, so a cancel
cannot fall between the two. A run cancelled while queued ends `cancelled`
without its graph running. A cancel for a run that already ended returns
`409`.

## 2.6 The run queue and the workers

The API does not run graphs. It admits a run (2.2) and puts it on the Redis
Stream `runs:queue`; a separate process, `python -m waypoint.worker`, takes it
off and drives it. Design: [design/queue.md](../design/queue.md).

```mermaid
sequenceDiagram
    participant C as Client
    participant A as api process
    participant Q as Redis queue (runs:queue)
    participant W as worker
    participant E as Redis run events

    C->>A: POST /threads/{id}/runs
    A->>E: run.started (with trace_id)
    A->>Q: XADD job
    A-->>C: 202 run_id
    C->>A: GET /runs/{id}/events (SSE)
    W->>Q: XREADGROUP (one run, when it has room)
    Q-->>W: job
    W->>E: token, node, custom ... events
    A-->>C: the same events, as they land
    W->>E: run.completed
    W->>Q: XACK + XDEL
```

Why a separate process: a run used to be a task on the event loop of the
api process that took the POST, so a deploy cancelled every run in flight and
a process that died left its runs `running` for good. Now restarting or
deploying the api leaves runs alone, workers scale on their own, and a long
research run cannot slow a request.

* **The job** carries what the worker needs: input or resume value,
  `durability`, models, the key's scopes, the trace id the API chose (so the
  run's trace is still one root span, linked to the request), and the
  concurrency slot's member id, which the worker gives back.
* **Claiming.** Each worker reads the consumer group `workers` one entry at a
  time while it drives fewer than `QUEUE__CONCURRENCY` (8) runs. Redis gives
  each entry to one consumer, and it stays in the group's pending list until
  the worker acks it when the run ends, however it ends.
* **Liveness.** Every `QUEUE__HEARTBEAT_S` (10 s) a worker writes
  `worker:<name>` with a 30 s TTL. `/readyz` on the API reports
  `workers: ok (2)` and `queue: 0 queued, 1 running`, or
  `workers: unavailable`; with no worker, runs are accepted and wait.
* **Stopping.** On SIGTERM a worker takes nothing new and gives its runs
  `QUEUE__DRAIN_S` (20 s) to end before it cancels the rest.
* **Without Redis** there is no queue to share, so the API process runs a
  worker of its own on an in-memory queue, through the same code.

What this does not do yet: a worker that dies mid-run leaves that run in the
pending list, unfinished. Reclaiming it with `XAUTOCLAIM` and resuming it from
its last checkpoint is the design's phase 2.

## 2.7 Limits on a run

| Limit | Default | Enforced by |
|---|---|---|
| Run starts per tenant | 60 per minute (`LIMITS__RPM`) | Lua token bucket in Redis |
| Spend per tenant, and for the server | US dollars per day (`BUDGET__*`; the demo: $0.35 and $0.70) | Lua bucket in Redis, refilling continuously; checked before a run, charged per model call |
| Concurrent runs per tenant | 4 (`LIMITS__CONCURRENT_RUNS`) | Lua semaphore; lease = run timeout + 30 s, restarted when a worker starts the run |
| Wall-clock per run | 300 s (`LIMITS__RUN_TIMEOUT_S`) | `asyncio.timeout` around the graph |
| Super-steps per run | 40 (`LIMITS__RECURSION_LIMIT`) | LangGraph recursion limit |
| Request body | 8 MB | ASGI middleware, before parsing |

The research agent also bounds its own loop (rounds, follow-ups, searches per
worker); see [4.5](04-agents.md#45-the-research-agent).

## 2.8 Durability modes

The `durability` field on a start request is passed to LangGraph and decides
when checkpoints are written:

| Mode | Checkpoint written | Use |
|---|---|---|
| `sync` | before the next step starts | strongest; each step waits on Postgres |
| `async` (default) | in the background while the next step runs | normal use; a crash can lose the last step |
| `exit` | only when the run ends or pauses | cheapest; no mid-run history |

An interrupt is always durable: the graph cannot pause without writing the
checkpoint it will resume from.

## 2.9 What the run record holds

`GET /runs/{id}` returns the record kept in Redis under `run:{id}:meta`, next
to the event stream at `run:{id}:events`: `run_id`, `thread_id`, `agent`,
`tenant_id`, `user_id`, `status`, `created_at`, `error`, `models` (the
role-to-model map this run used) and, once the run ends or pauses, `usage`
(cost, tokens and searches per role and in total). The `run_audit` table in
Postgres keeps the durable part (ids, tenant, user, agent, status, error,
`started_at`, `ended_at`, and the cost, token and search totals with the
per-role breakdown in `usage`; not the models), outlives the stream's TTL,
and is indexed by thread and by tenant.

The cost comes from `waypoint.usage.Meter`, a LangChain callback handler that
`RunManager._drive` attaches to every run. Each chat model call reports its
usage to it, attributed to a role by the `model_role` metadata each role's
runnable carries, and is priced from `src/waypoint/prices.toml` (cache reads,
cache writes and long prompts at their own rates, web searches per request).
Embeddings and reranking are not chat model calls; the code that makes them
reports them through `METER`, a ContextVar holding the current run's meter.
