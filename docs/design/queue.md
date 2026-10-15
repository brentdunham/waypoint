# Design: the durable run queue

Status: **phase 0** (written 2026-10-15). Q2 decided with Brent on
2026-10-15 (the API only enqueues; runs execute in a separate `worker`
service); the rest are proposed and the remaining open decisions below are
for Brent. This note is the hand-off for the
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
   the run over.
4. A tool call that changes something (`notes_add`) is not done twice when
   its step is re-run after a worker dies.
5. Budgets, the run's reported cost and its trace stay correct across a
   reclaim.
6. Every reference document and the reviewer guide describe the queue (see
   [Documentation](#documentation)).

Non-goals for v1: priorities or fair scheduling between tenants beyond the
existing concurrency slots; scheduled or delayed runs; a queue other than
Redis; exactly-once execution (see Q10).

## What exists today

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

Proposed 2026-10-15; to be confirmed with Brent in phase 0.

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
| Q12 | **`durability` means something for recovery** and is documented per value: `sync` resumes from the last finished step; `async` from the last step whose write had landed (at most one more step repeats); `exit` starts the run over. Default stays `async` unless phase 0's measurement says otherwise (open decision 2). | The ROADMAP's "make durability a real per-run choice": the choice already exists, what is missing is its meaning. | A new field. |

Without Redis (`MemoryBus`), a `MemoryQueue` hands jobs to the same consumer
code inside the API process: no reclaim, no lease, the same code path for
tests. With Redis, local development runs the worker beside the API
(`uv run python -m waypoint.worker`, a second terminal); a run started with no
worker running waits in the queue, and `/readyz` says why.

## Open decisions (settle in phase 0)

1. **Where workers run.** *Decided 2026-10-15 (Brent): a separate `worker`
   service; the API only enqueues (Q2).* Still to settle: replicas and
   concurrency on Bob. Proposed: 2 replicas of 8 runs each, after measuring
   a worker's resident memory in phase 1 (the API's two gunicorn workers can
   drop to one or stay at two for request concurrency alone).
2. **Default durability.** Proposed: keep `async`, unless `sync` costs less
   than 5% of a research run's wall time on Bob. Measure both in phase 0
   (one research run each, under $0.10).
3. **Timings.** Proposed: lease 30 s, heartbeat 10 s, reclaim scan 10 s, so a
   killed worker's run moves within about 40 s; at most 3 attempts. Shorter
   leases move runs sooner and risk false reclaims on a busy box.
4. **Deploy handoff.** Proposed: drain 20 s, then hand off (Q9).
   Alternative: hand off at once on shutdown (faster deploys, more re-run
   steps).
5. **Repeated side effects.** Proposed: idempotency key for MCP calls and
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
| `docs/API.md` | `durability` per value, `run.reclaimed`, cancel of a queued run. | 2, 3 |
| `docs/CONFIGURATION.md` | `QUEUE__*` with defaults and why. | 1, 2 |
| `docs/TESTING.md` | The two-manager integration tests and how a worker death is simulated. | 1, 2 |
| `docs/DEPLOY.md` and `deploy/.env.production.example` | The worker service, its replicas and logs; deploys hand runs off; the kill test. | 1, 2, 3 |
| `docs/guide/01-overview.md` | The worker service in the system map; "survives the loss of the worker" moves from the gaps column. | 1, 2 |
| `docs/guide/02-runs.md` | The queue, claim, reclaim and resume, with a sequence diagram (Mermaid). | 1, 2 |
| `docs/guide/03-state-and-persistence.md` | What each durability mode recovers. | 2 |
| `docs/guide/06-security-and-operations.md` | The "worker restarts mid-run" row. | 2 |
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
* **Two runs on one thread.** Nothing today stops a second POST on a thread
  while a run on it is still going, and the queue does not change that: two
  workers can drive two runs of one thread, as two tasks can now. Out of
  scope here; worth a guard (409 while a run is `running`) as its own item.

## Implementation log

### Phase 0: spikes (2026-10-15)

* **Resume after a dead step.** See the first key fact. Calls `a, b, b, c`
  under all three durability modes; `aget_state` after the cancel shows the
  interrupted node as `next`.
* **Streams.** See the third key fact: the heartbeat (`JUSTID`) does not
  count a delivery, the handoff (`IDLE`) makes an entry claimable at once,
  and `XAUTOCLAIM`'s delivery count is the attempt number.
* Still to do: the `sync` versus `async` measurement (open decision 2).
