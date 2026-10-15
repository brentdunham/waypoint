# Follow-up implementation plan

Started 2026-10-15 after the initial project analysis. No deployment or paid
model evaluations are part of this change.

1. Make run records, streams, cancellation and historical traces follow the
   thread ownership rule: the owning user or a tenant admin, within the same
   tenant. Apply the same filter to in-memory thread lists. Add regressions
   for two users in one tenant and a caller in another tenant.
2. Admit only one active run per thread, including queued runs. Claim the
   thread atomically across API processes, release it only for the matching
   run, and retain it during worker recovery. A competing POST returns 409.
3. Detach the previous frontend stream when switching threads and ignore
   callbacks from superseded requests. Test switching while POST and SSE are
   still in flight.
4. Implement the remaining recovery design in `design/queue.md`: fenced
   ownership, renewal, reclaim, checkpoint continuation, bounded attempts and
   deadlines, graceful handoff, cumulative usage, and recovery notices.
   Make repeated built-in memory and demo MCP writes idempotent before
   enabling recovery of tool steps.
5. Synchronize the README, API/configuration/testing/deployment references,
   roadmap and design guide with the resulting behavior.
6. Run Python tests, lint, formatting and type checking, frontend tests and
   build, and the infrastructure suite on Linux. Record unavailable checks
   explicitly. Existing ignored scratch files must not pollute type checks.

## Acceptance

Ownership checks agree across endpoints; a thread cannot have two active
runs; old streams cannot update a different conversation; a lost worker's
run finishes on another worker with one terminal event and accumulated
usage; repeated demo writes do not duplicate side effects. Local checks
pass. Production kill/deploy checks remain deployment acceptance steps and
must not be described as completed without running them.

## Completed locally

All six implementation steps above are complete. Run ownership is shared
across thread and run access checks. Enqueue reserves the thread atomically.
Frontend transport callbacks are scoped to the current conversation. Worker
recovery includes Redis output fencing and transactional Postgres checkpoint
fencing, handoff, deadlines, bounded attempts, cumulative usage and traces.
Memory and demo notes writes are idempotent. `sync` is the default durability.
The API, configuration, deployment, testing, design guide and roadmap agree.

Validation on 2026-10-15:

| Check | Result |
|---|---|
| Full Python suite on Linux, Python 3.14.2, locked dependencies | 481 passed, 1 skipped |
| Postgres/Redis integration and real child-process SIGKILL recovery | Included in the passing full suite |
| Optional proposed retrieval dataset | Skipped because `retrieval.proposed.jsonl` does not exist |
| Python lint | `ruff check src tests evals`: passed |
| Python formatting | `ruff format --check src tests evals`: 117 files formatted |
| Python type checking | `python -m mypy`: passed, 82 source files |
| Frontend | `npm test`: 27 passed; `npm run build`: passed |
| Patch whitespace | `git diff --check`: passed |

Linux tests used a disposable Docker network, fresh pgvector/Postgres 17
and Redis 8 containers, a read-only source mount, and an isolated virtual
environment installed with `uv sync --locked`. No live model evaluations
were used. The test containers and network were removed afterward.

The SIGKILL test checks recovery against the checkpoint that actually
landed. With `async`, a completed step can still need replay after a crash;
its repeated work is included in the cumulative usage. The scripted model
now gives separate user turns distinct tool-call ids, so the offline
stand-in does not confuse new writes with replayed tool steps.

## Remaining deployment acceptance

Deploy and validate on Bob as described in `DEPLOY.md`: kill the driving
worker during a research run, restart workers during a run longer than the
drain, and confirm browser recovery without doubled text, cumulative usage,
and one notes mutation across a handoff. These production checks have not
been performed. ROADMAP item 6 remains pending deployment acceptance.
