# Design: the durable run queue

Status: **phases 1 to 3 implemented locally** (2026-10-15), not yet deployed.
Local verification is recorded in [NEXT-STEPS.md](../NEXT-STEPS.md); Bob
kill/deploy acceptance remains pending. All decisions settled with Brent
on 2026-10-15: Q2 as he changed it (the API only enqueues; runs execute in a
separate `worker` service), the rest as proposed. Phase 0 is done. This note is the hand-off for the
coding sessions: what to build, what is decided, what is still open, in what
order, and which documents each phase must update. Read it with
[ARCHITECTURE.md](../ARCHITECTURE.md), [guide/02-runs.md](../guide/02-runs.md)
and [guide/03-state-and-persistence.md](../guide/03-state-and-persistence.md).

## Goal

A run no longer belongs to the process that accepted its POST. It goes on a
Redis Streams queue; workers claim runs from it; a run whose worker dies is
claimed by another worker and resumes from its last checkpoint. The API
only accepts and enqueues runs; a separate `worker` service executes them.
ROADMAP item 6.

Success looks like:

1. On Bob, `docker kill` of the worker container driving a research run
   leaves the run to finish on the other worker container. The browser that was following it sees
   one notice that the run moved and then the rest of the answer, with no
   reload and no lost or doubled text (the ROADMAP's "without client-visible
   loss").
2. A deploy (`docker compose up -d --build`) no longer cancels runs in flight:
   the old worker container hands them off and a new one finishes them. A
   deploy that changes only the API leaves runs alone.
3. `durability` is a per-run choice whose effect on recovery is documented
   and tested: `sync` and `async` resume from the last step, `exit` starts
   the run over. The default becomes `sync` (open decision 2).
4. A tool call that changes something (`notes_add`) is not done twice when
   its step is re-run after a worker dies.
5. Budgets, the run's reported cost and its trace stay correct across a
   reclaim.
6. Every reference document and the reviewer guide describe the queue (see
   [Documentation](#documentation)).

Non-goals for v1: priorities or fair scheduling between tenants beyond the
existing concurrency slots; scheduled or delayed runs; a queue other than
Redis; exactly-once execution (see Q10).

## Starting point before phase 1

* **`RunManager.start`** (`infra/runs.py`) checks the agent, opens the run's
  span, takes a tenant concurrency slot (a 429 comes from the POST, not a
  failed run), writes the run's meta to Redis and its audit row to Postgres,
  appends `run.started`, then `asyncio.create_task(self._drive(...))` in the
  same process. The POST returns 202 with the run.
* **`_drive`** streams `graph.astream(..., durability=durability)` under
  `LIMITS__RUN_TIMEOUT_S`, appends each chunk to the run's event stream
  (`run:{id}:events`), and ends with `run.completed`, `run.interrupted`,
  `run.failed` or `run.cancelled`. Its `finally` writes the final meta and
  audit row, flushes the meter and gives the slot back.
* **Cancel** publishes on `runs:cancel`; every process listens, and the one
  holding the task cancels it.
* **`aclose()`** at shutdown cancels every task, so a deploy cancels every
  run in flight. Two gunicorn workers per container, `--graceful-timeout 30`.
* **A dead worker** leaves the run's meta at `running` forever, its slot held
  until the lease (`LIMITS__RUN_TIMEOUT_S` + 30 s) expires, and a client
  following the event stream waiting. Guide 06 lists this as a known gap;
  ARCHITECTURE "What is deliberately missing" and guide decision D11 name the
  queue as the fix.
* **Followers** (`events()`, SSE, `?wait=true`) read the run's event stream
  and meta from Redis, not from the process driving it. They already work
  from any worker.
* **`durability`** is already a field on the POST (`sync`, `async`, `exit`;
  default `async`) and is passed to `astream`. Nothing says what each one
  means for a run that is interrupted by a crash, because nothing recovers one.
* **Without Redis** (development, tests) `MemoryBus` keeps events in the
  process, and there is one process.

## The key facts this design rests on

* **Everything a run needs is already shared.** Checkpoints and the memory
  store are in Postgres, uploaded documents are stored in Postgres by the
  upload request before its ingest run starts, and events, meta, slots and
  budgets are in Redis. A run therefore has nothing tied to the process that
  accepted it, so another container can drive it.

* **LangGraph resumes a thread from its last checkpoint** when invoked with
  `None` as the input. Phase 0 spike (2026-10-15, langgraph 1.2.15,
  `/tmp/claude-0/spike6/s.py`): a four-step graph `a -> b -> c` with `b`
  slow, the task cancelled inside `b`, under each of `sync`, `async` and
  `exit`. In every case `aget_state` showed `next == ("b",)` with `a`'s
  values, and `ainvoke(None, config)` ran `b` and `c` to the end. Node calls
  were `a, b, b, c`: **the interrupted step runs again** (at-least-once). With
  `exit`, the cancelled run still wrote a checkpoint here because the
  cancellation ran LangGraph's exit path; a `kill -9` does not, so the design
  assumes `exit` loses the whole run.
* **Checkpoint metadata does not carry the run id** (keys `parents`,
  `source`, `step`). To tell "this run has made progress" from "this run has
  not started", the worker records the thread's latest checkpoint id before
  its first step (Q5).
* **Redis Streams consumer groups give the claim and the reclaim.** Spike on
  Redis 7.0 (Bob runs 8): after `XREADGROUP`, `XCLAIM ... JUSTID` by the
  owner resets the entry's idle time without counting a delivery (a
  heartbeat); `XCLAIM ... IDLE <ms>` makes an entry look idle at once (a
  handoff); `XAUTOCLAIM <min-idle>` by another consumer takes it and raises
  its delivery count from 1 to 2 (the attempt number).
* **A slow worker is not a dead one.** An event loop blocked past the lease
  would let another worker claim a run that is still running. Two drivers of
  one thread would interleave checkpoints and events. So ownership is fenced
  (Q4), not inferred from idle time alone.

## Decisions

Settled with Brent 2026-10-15.

| # | Decision | Why | Alternative rejected |
|---|---|---|---|
| Q1 | **The POST enqueues; a worker drives.** `start` keeps every check it makes today (agent, slot, budget, meta, audit row, `run.started`) and the 202, then `XADD runs:queue` with a job: run id, agent, thread, tenant, user, scopes, input or resume value, checkpoint id, durability, models, request id, trace parent, slot member, deadline. | Admission errors stay synchronous (a 429 is still a 429), and the client contract does not change. | Enqueueing before the checks (a refused run would be a failed run). |
| Q2 | **The API only enqueues; a `worker` service executes.** *Decided with Brent 2026-10-15.* `python -m waypoint.worker` is a new entry point and compose service on the same image, with no HTTP server: one asyncio process per container, a consumer of group `workers` on `runs:queue` named `{host}-{pid}-{uuid}`, reading one entry at a time while it has fewer than `QUEUE__CONCURRENCY` runs (default 8). Two replicas on Bob, so killing one leaves a worker to reclaim. The API process compiles graphs only to read thread state and validate agents; it never drives a run. Without Redis (tests, a laptop with no Redis) there is no shared queue, so `MemoryQueue` and an in-process consumer keep the API self-contained. | Runs stop competing with requests for the API's event loop, workers scale and restart on their own, and an API deploy does not touch runs in flight. | Consumers inside the API processes with a switch to split later (one fewer container, but every API restart is a run handoff). |
| Q3 | **A run's entry is acked and deleted when the run ends** in any terminal state, including `interrupted` (a resume is a new run). | The pending list then holds exactly the runs in flight. | Acking on claim (a dead worker would lose the run). |
| Q4 | **Ownership is a lease with a fence.** The worker that claims a run sets `run:{id}:owner` to its consumer name with a TTL of `QUEUE__LEASE_S` (30 s) and renews it every 10 s with a Lua compare-and-renew, alongside `XCLAIM ... JUSTID` and a refresh of the tenant slot's lease. A worker that finds it no longer owns a run stops its task without writing a terminal event. Events and the final meta are written only through a check of the owner key. | The fence keeps a paused worker from writing over the run its successor now drives. | Idle time alone (a long GC pause or a blocked loop would make two drivers). |
| Q5 | **Every worker reclaims.** Every 10 s, `XAUTOCLAIM runs:queue workers <me> 30000`. For each entry: if the run's cancel flag is set, finish it as `cancelled`; if its deadline has passed, as `failed` (timeout); if its delivery count is over `QUEUE__MAX_ATTEMPTS` (3), as `failed` ("the worker running this run was lost N times"); otherwise drive it again. | Bounded retries: a run that kills its worker every time (out of memory, say) stops after three tries. | A dedicated reaper process. |
| Q6 | **Resume from the checkpoint when the run made progress.** Before its first step the first worker records the thread's latest checkpoint id on the job's record (`run:{id}:start`). A reclaiming worker compares it with the thread's latest now: different means the run wrote at least one checkpoint, so it continues with `astream(None)`; the same means it did not, so it starts over with the original input (or `Command(resume=...)`). | Uses LangGraph's own recovery (the spike) and never repeats a step it does not have to. | Always starting over (repeats completed model calls and tool calls); always `None` (a run that had not started would do nothing). |
| Q7 | **The client sees `run.reclaimed`**, appended to the same event stream: `{attempt, from_checkpoint}`. Followers keep reading the same stream with `Last-Event-ID`. The web client drops the partial tokens of the step in progress (or of the whole reply when `from_checkpoint` is false) and shows "Resumed on another worker". | The step that died had streamed tokens that will stream again; without the marker the client would show them twice. | Hiding the move (doubled text); failing the run and asking the user to retry (the loss the ROADMAP rules out). |
| Q8 | **Cancel works on a run with no live worker.** `cancel` sets `run:{id}:cancel` (TTL a day) as well as publishing. A worker checks it before driving and between reclaim and resume. | A run queued behind a full worker, or orphaned, can be cancelled now. | Pubsub alone (nobody is listening for an orphan). |
| Q9 | **Shutdown hands runs off.** On SIGTERM a worker stops reading, waits up to `QUEUE__DRAIN_S` (20 s, inside the service's `stop_grace_period: 30s`) for its runs to end, then stops the rest without a terminal event, deletes its owner keys and marks their entries idle (`XCLAIM ... IDLE`), so the next `XAUTOCLAIM` takes them at once. | A deploy stops cancelling runs, which is ROADMAP item 6's "Why". | Waiting for every run to end (a five-minute research run would hold a deploy). |
| Q10 | **At-least-once, made safe where it matters.** A re-run step repeats its model calls (metered and charged again; the cost is honest) and its tool calls. MCP calls carry `io.waypoint/idempotency-key` = `{thread_id}:{tool_call_id}` in `_meta`; the tool call id comes from the checkpointed model message, so it is the same on the re-run. The notes server keeps keys for a day and returns the first result for a repeated key. | The only side effects that matter are external writes, and they are approved by a person, so doing one twice is the failure a user would notice. | Exactly-once (needs a transactional outbox per tool server). |
| Q11 | **Cost and traces survive a reclaim.** The meter adds each call's cost and tokens to `run:{id}:usage` (Redis hash) as it goes; the worker that ends the run reports that total, not only its own. The reclaiming worker opens a `run.attempt` span in the same trace (the job carries the trace parent) with `waypoint.run.attempt`. | Budgets are already charged per call and are right; without this the audit row and `usage` would count only the last attempt. | Accepting an undercount. |
| Q12 | **`durability` means something for recovery** and is documented per value: `sync` resumes from the last finished step; `async` from the last step whose write had landed (at most one more step repeats); `exit` starts the run over. **The default becomes `sync`** (phase 2), on phase 0's measurement (open decision 2). | The ROADMAP's "make durability a real per-run choice": the choice already exists, what is missing is its meaning. | A new field. |

Without Redis (`MemoryBus`), a `MemoryQueue` hands jobs to the same consumer
code inside the API process: no reclaim, no lease, the same code path for
tests. With Redis, local development runs the worker beside the API
(`uv run python -m waypoint.worker`, a second terminal); a run started with no
worker running waits in the queue, and `/readyz` says why.

## Open decisions (settled in phase 0)

1. **Where workers run.** *Decided 2026-10-15 (Brent): a separate `worker`
   service; the API only enqueues (Q2).* Still to settle: replicas and
   concurrency on Bob. Proposed: 2 replicas of 8 runs each, after measuring
   a worker's resident memory in phase 1 (the API's two gunicorn workers can
   drop to one or stay at two for request concurrency alone).
2. **Default durability.** *Decided 2026-10-15 (Brent): the rule as
   proposed, `sync` unless it costs 5% or more of a research run's wall time.
   Measured: about 0.3 ms per step, so `sync` becomes the default in phase 2
   (see the phase 0 log).*
3. **Timings.** *Decided 2026-10-15 (Brent): as proposed.* lease 30 s, heartbeat 10 s, reclaim scan 10 s, so a
   killed worker's run moves within about 40 s; at most 3 attempts. Shorter
   leases move runs sooner and risk false reclaims on a busy box.
4. **Deploy handoff.** *Decided 2026-10-15 (Brent): as proposed.* drain 20 s, then hand off (Q9).
   Alternative: hand off at once on shutdown (faster deploys, more re-run
   steps).
5. **Repeated side effects.** *Decided 2026-10-15 (Brent): as proposed.* idempotency key for MCP calls and
   dedupe in the notes server (Q10). Alternative: before re-running a tools
   step that had approved writes, pause for approval again.

## Components

* `infra/queue.py` (new): `RunQueue` (Redis Streams: enqueue, read, heartbeat,
  reclaim, ack, hand off; the owner-key Lua) and `MemoryQueue`.
* `worker.py` (new, `python -m waypoint.worker`): builds the runtime
  without the HTTP app, runs the `RunWorker` loop, handles SIGTERM (Q9),
  writes a liveness heartbeat (`workers:{consumer}` in Redis with a TTL, and
  a file the compose healthcheck checks for age). Uses `loops.py` on Windows.
* `agents/runtime.py`: the runtime split into what both sides need (bus,
  db, graphs, limiter) and what only the worker starts (MCP discovery, the
  cancel listener, the consumer). The API stops discovering MCP tools; the
  worker reports each server's status in its heartbeat for `/readyz`.
* `infra/runs.py`: `start` enqueues instead of creating a task; a
  `RunWorker` loop (read, reclaim, heartbeats, drain);
  `_drive` takes a job, decides input versus resume (Q6), writes through the
  fence, and acks in `finally`. Cancel sets the flag.
* `infra/ratelimit.py`: `Limiter.slot` split into `acquire`, `refresh` and
  `release` by member, so the slot taken by the POST is held by whichever
  worker drives the run.
* `usage.py` (`Meter`): mirrors totals to `run:{id}:usage`.
* `tracing.py`: the `run.attempt` span.
* `agents/mcp.py` and `mcp_demo/notes.py`: the idempotency key (Q10).
* `settings.py`: `QueueSettings` (`QUEUE__CONCURRENCY`,
  `QUEUE__LEASE_S`, `QUEUE__MAX_ATTEMPTS`, `QUEUE__DRAIN_S`).
* `/readyz`: live workers (from their heartbeats), the queue's length and
  pending count, and MCP status from the workers. `workers: unavailable`
  when none is alive.
* `compose.yaml` and `compose.prod.yaml`: a `worker` service (same image,
  `command: ["python", "-m", "waypoint.worker"]`, `deploy.replicas: 2`,
  `stop_grace_period: 30s`, healthcheck); the environment shared with `api`
  through a YAML anchor so the two cannot drift. `compose.dev.yaml`'s header
  gains the worker command.
* Web (`hooks/useRun.ts`, `api/types.ts`): `run.reclaimed`.

## Phases

Each phase ends green: pytest, ruff, mypy, `npm test`, `npm run build`, the
integration tier on Linux, and with the documents listed for it in
[Documentation](#documentation) updated in the same commit as the code.

| Phase | Scope | Done when |
|---|---|---|
| 0 | Settle open decisions 1 to 5. Spikes: resume after a cancelled step (done); Streams claim, heartbeat and handoff (done); `sync` versus `async` wall time on a research run. | Spikes and decisions recorded in this doc. |
| 1 | The queue and the worker service, without recovery: `RunQueue`, `MemoryQueue`, `python -m waypoint.worker`, the runtime split, the compose service, the worker loop with its concurrency cap, the slot carried by member, ack on end, the cancel flag, `QUEUE__*`, `/readyz`. Behaviour is otherwise unchanged; every existing test passes. Measure a worker's memory. | Integration test: the API side enqueues, two workers on real Redis drive the runs. Deployed to Bob: runs execute in the worker containers, none in `api`. |
| 2 | Recovery: the owner fence, heartbeats, reclaim, resume from checkpoint or input, deadline and attempts, `run.reclaimed`, usage across attempts, the attempt span, shutdown handoff, durability documented and tested per value. | Integration test: a worker stopped mid-step without cleanup, the other finishes the run; the event stream has one `run.reclaimed` and one terminal event; usage counts both attempts. |
| 3 | Safety and the client: MCP idempotency keys and the notes server's dedupe, `remember` keyed by tool call id, the web client's handling of `run.reclaimed`, the Bob kill test and a deploy during a run. | ROADMAP item 6's check on Bob: `kill -9` of the driving worker, the run finishes with no client-visible loss; a deploy during a run finishes it too. |

## Documentation

Each phase updates these in the same commit as its code, and the
implementation log records what changed from this design and why.

| Document | What it gains | Phase |
|---|---|---|
| `docs/design/queue.md` (this file) | Decisions settled, spike results, a log entry per phase. | every |
| `docs/ARCHITECTURE.md` | The API and worker processes, the run lifecycle through the queue; "What is deliberately missing: a queue" removed. | 1, 2 |
| `docs/API.md` | `/readyz`'s `workers` and `queue`, `run.started` meaning queued, cancel of a queued run (1); `durability` per value, `run.reclaimed` (2, 3). | 1, 2, 3 |
| `docs/CONFIGURATION.md` | `QUEUE__*` with defaults and why. | 1, 2 |
| `docs/TESTING.md` | The two-manager integration tests and how a worker death is simulated. | 1, 2 |
| `docs/DEPLOY.md` and `deploy/.env.production.example` | The worker service, its replicas and logs; deploys hand runs off; the kill test. | 1, 2, 3 |
| `docs/guide/01-overview.md` | The worker service in the system map; "survives the loss of the worker" moves from the gaps column. | 1, 2 |
| `docs/guide/02-runs.md` | The queue, claim, reclaim and resume, with a sequence diagram (Mermaid). | 1, 2 |
| `docs/guide/03-state-and-persistence.md` | What each durability mode recovers. | 2 |
| `docs/guide/06-security-and-operations.md` | Shutdown, health and the failure rows for the api, a stopped worker, a dead worker and no worker. | 1, 2 |
| `docs/guide/07-decisions.md` | D11 superseded by a queue decision record. | 1 |
| `docs/guide/glossary.md` | Run queue, consumer group, lease, reclaim, attempt, handoff. | 1, 2 |
| `README.md` | Durable runs in the feature list. | 3 |
| `docs/ROADMAP.md` | Item 6 progress after each phase. | every |
| `CLAUDE.md` | The current design doc and phase. | every |

## Risks

* **Runs wait when no worker is up.** A POST still returns 202, and the run
  sits in the queue until a worker starts. `/readyz` reports it, the
  healthcheck restarts a stuck worker, and the run's deadline (Q5) fails it
  cleanly if nothing picks it up in time.

* **Two drivers of one run.** The fence (Q4) is the defence; an integration
  test pauses a worker past its lease and checks that it stops and writes
  nothing.
* **Repeated side effects outside MCP.** `remember` keys each memory by the
  time it was written, so a re-run tools step stores the fact twice. Phase 3
  keys it by the tool call id instead (an upsert on a re-run), keeping
  `created_at` for ordering. Any new tool with an effect must take the same
  key.
* **Repeated cost.** A re-run step pays for its model calls again. Bounded by
  attempts (3) and the budgets, and visible in the run's usage.
* **Redis loss.** The queue is only as durable as Redis; compose runs it
  with `--appendonly yes`, and the audit row in Postgres still records the
  run.
* **Two runs on one thread.** Addressed in the follow-up: enqueue and a
  persistent thread reservation are one atomic operation. Terminal publication
  releases only that run's reservation; handoff retains it.

## Implementation log

### Phase 0: spikes (2026-10-15)

* **Resume after a dead step.** See the first key fact. Calls `a, b, b, c`
  under all three durability modes; `aget_state` after the cancel shows the
  interrupted node as `next`.
* **Streams.** See the third key fact: the heartbeat (`JUSTID`) does not
  count a delivery, the handoff (`IDLE`) makes an entry claimable at once,
  and `XAUTOCLAIM`'s delivery count is the attempt number.
* **What `sync` costs.** Measured in the sandbox against Postgres 16 with the
  fake model (`/tmp/claude-0/spike6/dur.py`): 15 runs per mode, `?wait=true`
  wall time, median. Chat: `exit` 24.3 ms, `async` 27.2 ms, `sync` 29.0 ms
  (4 checkpoints). Workspace: 25.1, 36.7 and 37.5 ms (6 checkpoints). `sync`
  over `async` is under 2 ms a run, about 0.3 ms a step; the difference is
  only that the run waits for each checkpoint write it already makes. A
  research run's median is 59 s on corpus cases
  ([case-studies/cost.md](../case-studies/cost.md)) and takes tens of steps,
  so `sync` costs well under 0.1% of it, against the 5% threshold. Not
  repeated on Bob: its Postgres is on the same compose network, and a step
  would have to cost 100 times more for the answer to change. No live
  model call was needed, so this cost nothing.

### Phase 1: the queue and the worker service (2026-10-15)

Built as designed, with Q2 as Brent changed it: with Redis the API process
never drives a run.

* **Queue** (`infra/queue.py`). `RunJob` is the job, JSON in one stream
  field; a field this worker does not know is ignored, so an API deployed
  ahead of its workers does not break them. `RedisRunQueue`: `XADD`,
  `XREADGROUP` one entry at a time, `XACK` and `XDEL` in one transaction;
  the group is created by whichever process starts first (`ensure`, also
  after a `NOGROUP` from a flushed Redis); the cancel flag; worker
  heartbeats as `worker:<consumer>` keys with a TTL. `MemoryRunQueue` is the
  same in one process.
* **API side** (`RunManager` in `infra/runs.py`). `start` does what it did up
  to `run.started`, then enqueues instead of creating a task. It no longer
  makes the run span; see below. `cancel` sets the flag and publishes.
* **Worker side** (`RunWorker`). The read loop holds a semaphore of
  `QUEUE__CONCURRENCY`; each claimed run is a task that checks the cancel
  flag, drives the graph as `_drive` did, and acks in `finally`, shielded.
  The cancel listener, the heartbeat and MCP discovery live here.
  `python -m waypoint.worker` (`worker.py`) builds the runtime with
  `role="worker"`, handles SIGTERM and SIGINT, and has `--check` for the
  container healthcheck (heartbeat file under three beats old).
* **Runtime roles** (`agents/runtime.py`). `create_runtime(role=...)`: both
  roles build the pools, bus, limiter and graphs; the API alone seeds the
  bootstrap key and the docs corpus; the process that drives runs alone
  starts MCP discovery and a `RunWorker`. Without Redis the API is that
  process. A worker without `REDIS_URL` refuses to start.
* **Slot.** `Limiter.slot` became `acquire`, `refresh` and `release` by
  member (the context manager stays, on top of them). The API acquires; the
  worker refreshes the lease as the run starts and releases at the end.
* **Compose.** A `worker` service on the api's image, `deploy.replicas:
  ${WORKER_REPLICAS:-2}`, `stop_grace_period: 30s`, its own healthcheck. The
  api's environment moved to an `x-app-env` anchor both services use, and
  `compose.prod.yaml` has an `x-prod-env` anchor for both (anchors do not
  cross files). Checked with `docker compose config`: api and worker get the
  same 50 variables in production.

Deviations and additions, with reasons:

* **The trace id is chosen by the API.** The design had the worker open a
  `run.attempt` span later; it did not say who makes the run's root span
  now that two processes touch the run. The `202` and `run.started` must
  carry the trace id (a test checks it), so the API picks one
  (`new_trace_id`) and the worker's root span takes it through a custom OTel
  id generator for that one `start_span` call. The span is still a root
  linked to the request (O8), and every tracing test passes unchanged.
* **Pub/sub cancels only reach runs inside their graph.** The first full
  integration run failed `test_a_running_job_can_be_cancelled` once: the
  listener cancelled a task still checking the cancel flag, the
  `CancelledError` was swallowed, and the run stayed `running`. Now the
  listener cancels only runs in `_driving`, and `_drive` checks the flag
  again as it enters the graph. The API sets the flag before it publishes,
  so a cancel lands in one check or the other. A run stopped by shutdown
  before its graph began now ends `cancelled` too. Six repeats of the
  cancel and queue tests and two full integration runs passed after.
* **The API's own worker does not drain.** Without Redis (development,
  tests) the API's in-process worker stops runs at once on shutdown, as the
  API did before; only `python -m waypoint.worker` waits `QUEUE__DRAIN_S`.
* **`QUEUE__HEARTBEAT_S`** (10 s) is a setting; the design named only the
  lease, which is phase 2's.
* **`/readyz` with Redis gains `workers` and `queue`**, and `mcp:<name>` comes
  from the workers' heartbeats, since the API no longer discovers tools.
  `workers: unavailable` never fails readiness: the API still accepts runs.
  Without Redis the checks are unchanged (the unit test compares them
  exactly). So API.md and guide 06 changed in this phase, earlier than the
  Documentation table said; the table is updated.
* **Durability default.** Still `async` in this phase; it becomes `sync` with
  phase 2, where its meaning for recovery is documented and tested.

Measured:

* **Worker memory.** 142 MB resident at rest and 143 MB after 20 workspace
  runs (sandbox, fake model, `ps -o rss`); the API process was 147 MB. Two
  replicas add about 290 MB to Bob. To check there after the deploy:
  `docker stats --no-stream $(docker compose ps -q worker)`.
* **Processes, end to end.** `python -m waypoint.worker` and a uvicorn API as
  separate processes on one Postgres and Redis: a run posted to the API
  completed in the worker, `/readyz` showed `workers: ok (1)`, `--check`
  exited 0, and SIGTERM logged the drain and removed the worker's
  heartbeat key.

Tests: `tests/test_queue.py`, six new (three unit: a job's round trip, the
memory queue giving a run to one reader, the API driving its own runs without
Redis; three integration: the API only queues and a worker started later
drives the run, two workers share four runs, a run cancelled while queued
never starts and gives its slot back). `TestApp` starts a worker runtime per
app when Redis is set; `test_a_tool_approval_survives_a_process_restart`
now points its in-memory notes server at the workers' catalogs. Unit tier
452 collected, all passing; integration tier 36 passed on Linux (Postgres 16,
Redis 7), twice.

Next: deploy to Bob and check `docker compose ps worker`, `/readyz`, and
that `docker compose logs worker` shows runs. Then phase 2.

### Recovery and safety follow-up (2026-10-15)

Implemented ownership leases with attempt-specific tokens, atomic renewal
of the owner and pending entry, reclaim with bounded deliveries and the
original deadline, checkpoint continuation, and shutdown handoff. The
terminal event and record commit in one Redis operation, so a worker lost
after publication but before ACK cannot repeat the graph. Admission
atomically reserves each thread, including queued runs. Memory mode remains
single-process and cancels at shutdown.

The follow-up also fences Postgres checkpoint writes. `run_fences` records
the active attempt token. Registering a successor and writing a checkpoint
both lock the thread row; the token check and checkpoint write share one
transaction. Registration rechecks Redis ownership under that lock. This
extends Q4 so a delayed old attempt cannot overwrite the successor's state,
even if its graph resumes before its heartbeat notices the lost lease.

Usage is a Redis hash of per-attempt snapshots, written after completed
calls and summed at the end. Q11 uses snapshots rather than incremental
counters so repeated flushes cannot double-count. Repeated model work is
still charged. A killed provider call that never reported usage cannot be
reconstructed; the provider's bill is the authority for unobserved calls.
Each retry opens `run.attempt` in the same trace. Timelines include the
accepted time and the final attempt's cumulative cost.

The frontend handles `run.reclaimed`, clears partial text and displays the
move. Transport ownership prevents late POST, stream or cancel callbacks
from updating another conversation. MCP sends `{thread_id}:{tool_call_id}`
as `io.waypoint/idempotency-key`. The notes server stores a day's replay
results alongside each mutation in the same SQLite transaction, with tenant
separation and payload-conflict checks. `remember` hashes the thread and
tool call id for its store key. Other MCP servers must honor the metadata
to deduplicate their own writes. `exit` repeats the original input from the
starting checkpoint, or an empty branch if none existed. `sync` is default.

Run record, SSE, cancel and historical trace ownership follow the thread
user/tenant rule. In-memory thread lists use the database tier's user filter.
Fractional run timeouts round the Redis slot TTL up to whole seconds.
Validation results are in NEXT-STEPS.md. No production deployment or paid
evaluation was performed. Bob kill/deploy and browser acceptance remain
required before marking ROADMAP item 6 deployed and complete.
