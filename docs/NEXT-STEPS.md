# Follow-up implementation plan

Started 2026-10-15 after the initial project analysis. Initial implementation
and validation used no live model evaluations. After the commit/push/continue
request, the change was deployed and checked with bounded live acceptance runs.

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
pass. Production kill/deploy checks must be run before claiming deployment
acceptance; the results below record those checks.

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
| Full Python suite on Linux, Python 3.14.2, locked dependencies | 484 passed, 1 skipped after the acceptance timeline fix |
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

## Completed deployment acceptance

Deployed `cc5bcda` to Bob on 2026-10-15, after a Postgres backup and an idle
queue check. Built the API, workers, notes server and web images and waited
for healthy services. Checks used an isolated temporary tenant and scoped
key; worker disruption refused to proceed while other tenants had active
queue entries.

| Check | Result |
|---|---|
| SIGKILL of the driving worker | Run `de9abbc9-ffbe-40e2-8af6-d6b2638df1a0` reclaimed on the other worker after about 34 s; one `run.reclaimed`, one `run.completed`, unique data-event ids; final report 1,540 characters |
| Graceful worker restart with 20 s drain | Browser-started run `d35aeae7-fcf4-4297-a8f6-b3e75f1690a2` handed off to the other worker; one reclaim and completion; final report 2,156 characters |
| Cumulative usage | Handoff attempt snapshots $0.008038 + $0.032513 = $0.040551; token/search totals and final SSE usage agree with the run record |
| Browser recovery | Recovery notice visible, streaming continued, one final assistant reply and no remaining live reply |
| Notes mutation replay | Saved approval checkpoint replayed twice, including after the worker restart; SQLite still contained exactly one note |
| Final readiness | Postgres, Redis, graphs and notes healthy; two workers; zero queued/running runs |

The killed attempt ended before its first completed model call and had no
usage snapshot. The successor's recorded cost was $0.055219. Heartbeat SSE
frames intentionally reuse the latest event id; uniqueness checks apply to
data events. One preliminary handoff probe hit the node cache and completed
without disruption, so it was replaced by the browser-driven uncached probe.

The browser check exposed a timeline export race: a handed-off root span
could make the timeline ready before the terminal attempt had exported,
leaving the UI with partial cost. Fix `d1e5e00` keeps nonterminal attempts
pending, selects the terminal root's cumulative cost, and adds three
regressions. The full Linux suite was rerun after the fix. The deployed fix
was also checked against the real handoff trace: a trace without its terminal
root remains pending, while the full trace reports $0.040551 and 40,074.3 ms.

Ten acceptance runs recorded $0.150963 in total, including planning and
preliminary probes. This excludes any provider charge for a killed call
whose usage never reached Waypoint. No paid eval baseline was run. Temporary
conversations and notes were deleted, the scoped key was revoked and removed
from the browser, and the credential file was deleted. Audit rows remain as
evidence. The final disposable Linux test containers, network and dependency
volume were removed. ROADMAP item 6 is complete.
