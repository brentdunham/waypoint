# Glossary

Terms as this project uses them. Where a term has a general meaning, the
definition gives the general meaning first, then what it means here.

## Agents and runtime

| Term | Definition |
|---|---|
| **Agent** | A program in which a model decides some of the steps. Here: a compiled LangGraph graph plus its entry in the registry (`chat`, `research`). |
| **Approval** | The research agent's pause for a person to approve, edit or decline its plan. Implemented with an interrupt. |
| **Briefing** | The research agent's final output: a cited summary with a source list appended from metadata. Stored as `report`. |
| **Checkpoint** | A snapshot of a thread's graph state, written after each super-step. Enables pausing, resuming and time travel. |
| **Citation** | Here, two things: an `[n]` mark in a briefing, and the provider's metadata on a searcher reply that links text to a web page. Sources come only from the second. |
| **Context (runtime)** | Immutable per-run facts passed to every node: tenant, user, request id, run id, models. Not checkpointed. |
| **Config** | LangGraph plumbing for a run: thread id, checkpoint id, recursion limit, callbacks, tags. |
| **Critic** | The research role that scores each finding's confidence, notes its weaknesses, and may ask follow-up questions. |
| **`defer`** | A LangGraph node option: run once after all parallel branches in a step have finished. Used on `critique`. |
| **Durability mode** | When checkpoints are written: `sync`, `async` (default) or `exit`. |
| **Edge** | A connection between nodes. A conditional edge chooses the next node, or returns `Send` objects to fan out. |
| **Fan-out** | Running one node several times in parallel on different inputs, here with `Send`. |
| **Finding** | One research question's answer: question, answer, sources with quotes, whether it was cited, confidence, reviewer note, round, model. |
| **Follow-up** | A new question the critic asks after a wave, run as another wave of workers. |
| **Graph** | A LangGraph `StateGraph`: nodes, edges and a state schema, compiled with a checkpointer, store and cache. |
| **Interrupt** | `interrupt(payload)`: checkpoint, record the payload, end the run. A later run resumes the node with a value. |
| **Long-term memory** | Facts about a user kept in the store across threads, under `("memories", tenant, user)`. |
| **Node** | A function in a graph that reads state and returns an update. |
| **Node cache** | Cached node outputs in Redis, keyed by a hash of the node's input. |
| **Planner** | The research role that turns a topic into up to five questions, each with a reason. |
| **Quote** | Text from a source page that the searcher's answer drew on, taken from the citation's `cited_text`. Up to three per source. |
| **Reducer** | A function that merges a node's update into a state key, for example `add_messages` or `merge_findings`. Keys without one are last-write-wins. |
| **Retrieved-only source** | A page a search returned when the searcher cited nothing. Kept, marked `cited: false`, confidence capped at 0.3. |
| **Role** | A model call site in an agent: `planner`, `searcher`, `critic`, `writer`, `verifier`, `assistant`. Each can use a different model. |
| **Round / wave** | One pass of parallel workers followed by one critique. At most `RESEARCH__MAX_ROUNDS`. |
| **Run** | One execution of a graph on a thread, from a start request to a terminal state. |
| **Searcher** | The research role that answers one question with web search. One worker per question. |
| **`Send`** | A LangGraph object that dispatches a node with its own input. Used for fan-out. |
| **State** | The data a graph carries and checkpoints: messages, plan, findings, report. |
| **Store** | LangGraph's key-value store for data that outlives a thread; Postgres with pgvector here. |
| **Structured output** | A model reply constrained to a schema and parsed into a Pydantic object. |
| **Super-step** | One round of graph execution: every node scheduled together runs, then state is checkpointed. |
| **Thread** | A conversation with one agent, owned by one tenant and user, made of checkpoints. |
| **Time travel** | Reading or writing an earlier checkpoint. Writing forks a new branch. |
| **Verifier** | The research role that checks each briefing sentence against the sources' quotes. |
| **Worker** | Two meanings: a gunicorn process serving requests, or a research `investigate` node instance answering one question. The text says which. |
| **Writer** | The research role that writes the briefing from the findings, and rewrites unsupported sentences during verification. |

## Platform

| Term | Definition |
|---|---|
| **API key** | `ak_<key_id>.<secret>`. Only its SHA-256 is stored. Resolves to a principal. |
| **Event** | One item in a run's stream: `run.*` lifecycle, `token`, `node`, `custom`, `heartbeat`. |
| **Event bus** | Where run events are written and read: Redis Streams in production, in memory without Redis. |
| **`Last-Event-ID`** | The SSE header a reconnecting client sends so the server resumes after that event. Equal to the Redis Stream id here. |
| **Lease** | How long a concurrency slot lives without being released: run timeout + 30 s. |
| **Principal** | Who is calling: tenant, user, key id and scopes. |
| **Problem (RFC 9457)** | The error body format: `type` slug, title, status, detail, request id. |
| **Scope** | A permission on an API key: `threads:read`, `threads:write`, `agents:run`, `keys:admin`, `tenant:admin` (other users' threads in the tenant), or `*`. |
| **SSE** | Server-Sent Events: a long HTTP response of `id`/`event`/`data` frames. |
| **Tenant** | The unit of isolation: data, memory, cache entries and limits are per tenant. |
| **Terminal event** | `run.completed`, `run.failed`, `run.cancelled` or `run.interrupted`. A stream that closes without one was dropped. |
| **Token bucket** | A rate limit that refills continuously up to a capacity; allows short bursts, enforces an average. |

## Evaluation

| Term | Definition |
|---|---|
| **Agreement** | In calibration: the share of labelled examples where the judge and the label agree on pass (3 to 4) versus fail (1 to 2). |
| **Anchor** | The written description of one score on a criterion's scale. The judge is told to apply anchors exactly as written. |
| **Baseline** | An accepted scorecard (`evals/baselines/<agent>.json`) that later runs are compared with. Needs 3 trials. |
| **Calibration** | Measuring a judge against human-labelled examples per criterion, and routing each criterion to a judge that agrees well enough, or marking it untrusted. |
| **Case** | One dataset entry: input, interrupt answers, seeded memories, checks, criteria, what a good answer covers, tags, review date. |
| **Check** | A deterministic test on a transcript, written in code. Pass, fail or not applicable. |
| **Compare** | Running two configurations on the same cases and judging each criterion pairwise, in shuffled order. |
| **Criterion** | One graded property of an output, with a question, a target and four anchors. |
| **Dataset** | The fixed set of cases for one agent. |
| **Escalation model** | A stronger judge tried for criteria the default judge fails in calibration. |
| **Eval** | A repeatable measurement of output quality over a dataset, as scores with a spread rather than pass/fail tests. |
| **Expect** | A case's description of what a good answer covers. Context for the judge, not a reference answer. |
| **Fingerprint** | A hash of prompts, models per role, agent settings, max tokens, judge, rubric and dataset. Equal fingerprints measured the same thing. |
| **Fresh run** | An eval run that ignores stored calls (`--fresh`) and records new ones: a new sample of models and web. |
| **Gate** | `--against-baseline`: exit 1 when any metric drops more than its tolerance. |
| **Grade** | One judge verdict: reasoning, then a score of 1 to 4. |
| **Grounded** | The research criterion: every claim is supported by the findings and each `[n]` points to a source that supports it. Grounded in what was retrieved, not true in the world. |
| **Judge (LLM-as-judge)** | A model used to grade outputs against a rubric. Its grades are measurements and are calibrated before use. |
| **Label** | A person's score for a calibration example, with a reason. The ground truth for the judge. |
| **MAE** | Mean absolute error between judge scores and labels. Shows how far off disagreements are. |
| **Material** | What the judge is shown for a target: for a briefing, the topic, expectation, findings with sources and quotes, and the briefing. |
| **Paired comparison** | Comparing two configurations on the same recorded outputs, so the only difference is the change under test. Made possible by replay. |
| **Pass** | A grade of 3 or 4. |
| **Replay** | Answering a model call from a stored recording of an identical call. Keyed on case, trial, agent, role, model, bound setup and input. |
| **Routing** | `calibration/routing.json`: which judge grades each criterion, valid for the judge model, rubric and judge prompts it was measured with. |
| **Rubric** | The set of criteria for one agent, in `evals/rubrics/<agent>.toml`. |
| **Scorecard** | One eval run's results: check pass rates, criterion means and pass rates with spreads, judge status per criterion, usage and cost per role, fingerprint, git SHA. |
| **Self-judging** | A judge grading output produced by the same model. The harness warns when routing would do it. |
| **Spread** | The standard deviation of a metric across trials. |
| **Target** | What a criterion grades: `plan`, `briefing` or `reply`. |
| **Tolerance** | How far a metric may drop before the gate fails: twice the baseline's spread, at least 0.10 for checks and 0.25 for criteria. |
| **Transcript** | Everything one trial produced: final state, messages, every model call with role, model, tokens, searches and latency. |
| **Trial** | One run of one case. Several trials sample the model's variation. |
| **Untrusted** | A criterion no judge graded well enough in calibration. Not scored. |
