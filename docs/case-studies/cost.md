# Case study: a research run at 38% less, measured

October 2026. Observability phase 1 put a dollar figure on every run. This is
the first change made with it: find where a research run's money goes, try the
cheaper settings one at a time, and keep only what the evals say costs no
quality. Every number below comes from a scorecard in `evals/results/research/`
(13 cases: 7 web questions, 5 questions on the demo corpus and one declined
plan; Sonnet 5 for planner, critic and writer, Haiku 5.5 for searcher and
verifier, Haiku 4.5 as judge).

## Summary

| | before (2 rounds) | after (1 round) |
|---|---|---|
| Cost per run, every call priced live | $0.2413 | $0.1496 |
| Median seconds per corpus case | 99 | 59 |
| `calibrated` (3 trials) | 2.89 | 3.61 |
| `covers` | 3.56 | 3.44 |
| `grounded` | 3.83 | 3.92 |
| `useful` | 3.69 | 3.61 |
| `plan_quality` | 4.00 | 3.97 |
| `claims_cited` | 35/36 | 33/36 |

The change is one setting: `RESEARCH__MAX_ROUNDS` now defaults to 1, so the
critic scores the first wave of findings but sends no follow-up questions.
`covers` fell 0.11 and `useful` 0.08, each about one standard deviation of
the trial-to-trial spread (0.05 to 0.17); `calibrated` rose 0.72. Along the way the `claims_cited` check
turned out to be counting honest sentences as failures, and was fixed.

The "before" cost is a fresh one-trial run at the accepted baseline's
configuration (`20261012-145258-3bd69055c49a`), because the baseline itself
was scored before the replay store kept each call's usage. The "before"
scores are that baseline (`20261010-185106-3bd69055c49a`, 3 trials); the
"after" column is `20261012-160644-ed8a1508cbd4`, 3 trials, now the accepted
baseline.

## 1. Where the money went

Per-role tokens and searches from the fresh run, priced with `prices.toml`:

| Part | Per run | Share |
|---|---|---|
| Web searches (155 over 13 cases, $0.01 each) | $0.119 | 49% |
| Critic (Sonnet, 2 calls a run) | $0.058 | 24% |
| Writer (Sonnet) | $0.032 | 13% |
| Searcher tokens (Haiku) | $0.022 | 9% |
| Planner (Sonnet) | $0.007 | 3% |
| Verifier (Haiku) | $0.003 | 1% |

Two things had to be fixed before these numbers could be compared across runs
(design log, phase 4 part 2):

* **The fingerprint** counted two server-only seeding settings, so a `.env`
  with the demo corpus turned on made every run look like a new
  configuration. They are now left out.
* **Billed cost is not run cost.** Replay answers unchanged calls from the
  store for free, so a scorecard's billed cost depends on what was replayed.
  The store now keeps each call's usage, and scorecards report "cost per run
  if every call ran live". All five runs below are compared on that figure.

## 2. Three experiments, one trial each

Each changes one thing. The searches and the critic were the two biggest
lines, so each experiment targets one of them.

| | Cost if live | `calibrated` | `covers` | `grounded` | `useful` | `claims_cited` |
|---|---|---|---|---|---|---|
| Baseline | $0.2413 | 3.08 | 3.17 | 4.00 | 3.67 | 11/12 |
| One search per question (`MAX_SEARCHES=1`) | $0.1913 | 3.25 | 3.58 | 3.92 | 3.33 | 10/12 |
| No follow-up round (`MAX_ROUNDS=1`) | $0.1503 | 3.58 | 3.50 | 3.92 | 3.67 | 9/12 |
| No follow-up round, critic on Haiku | $0.1348 | 2.83 | 3.33 | 3.58 | 3.67 | 9/12 |

* **One search per question** saved 21%, but four of 12 briefings scored 2 on
  `useful`: with half the evidence they recommended less. Searches fell from
  155 to 88, not to half, because many calls had used only one of their two.
* **No follow-up round** saved 38% and lost nothing the judge could see. The
  second wave mostly re-searched near the first, and the critic's second call
  disappears with it.
* **The critic on Haiku**, tried on top of one round because that is what
  would ship, saved another $0.0155 per run and cost 0.75 on `calibrated`
  (five briefings at 2). The critic's job is to say how sure each finding is,
  and the writer hedges from that. A weaker critic produced briefings that
  stated thin evidence as fact. Not worth it.

Replay kept this cheap. The one-round experiment replayed the planner and the
first wave of searches from the baseline and billed $0.68 for 13 cases; the
Haiku-critic run billed $0.48.

## 3. Three trials, and a check that was counting the wrong thing

At three trials the one-round configuration held its judge scores, but
`claims_cited` (at least 80% of a briefing's claim-length sentences carry an
`[n]`) fell from 34/36 to 29/36, a drop larger than its spread. The obvious
fix was a writer prompt: drop or hedge any claim without a source.

Reading the uncited sentences first changed the plan. Almost all of them were
about the research, not the topic:

* "No field data at -20°C to -25°C was found."
* "No verified warranty data could be found for First Solar, LONGi, or Qcells."
* "Confidence here is low (0.3): no Oak Ridge or NREL field data could be
  verified despite being core to the question."

Counted with the rule below, the one-round briefings had 0.83 such sentences
per run against 0.36 for two rounds, and the same number of other uncited
sentences (1.11 against 1.08). The
second round had been filling gaps that one round now reports, which is also
why `calibrated` went up. A sentence that says nothing was found has nothing
to cite, and the `grounded` rubric already allowed it ([grounded
case study](grounded.md), section 2). The writer-prompt fix would have deleted
honest sentences to pass a check, so the check was fixed instead:

* `claims_cited` no longer counts an uncited sentence that states a gap (a
  negation and a reporting word: "found", "verified", "documented",
  "assessed") or a confidence. A cited sentence always counts.
* `python -m evals recheck <results dir>` re-runs the checks on saved
  transcripts, with the run's own settings, and rewrites the scorecard's
  checks. It calls no model.

Rechecked, the two-round baseline scores 35/36 and the one-round run 33/36.
The three one-round failures each miss the 80% line by one sentence, and
they are real uncited claims.

The rule is a heuristic. Of the 43 sentences it excuses across both runs,
about 40 are plainly about the research; three attach a "not verified"
caveat to a claim, and arguably should have cited it.

## 4. What changed

* `RESEARCH__MAX_ROUNDS` defaults to 1 (was 2), here and in
  `deploy/.env.production.example`. Set 2 where coverage matters more than
  cost: about $0.24 a run.
* `claims_cited` leaves out uncited statements about the research
  (`evals/checks.py`, `about_the_research`).
* `evals recheck`, for applying a corrected check to past runs.
* The accepted research baseline is now the one-round run
  (`ed8a1508cbd4`), rechecked.

## 5. What it cost, and what is left

The five eval runs billed $8.94 in agent calls (the fresh baseline $3.14,
the three experiments $3.56, the three-trial confirmation $2.24), plus judge
calls.

* `covers` is 0.11 lower. That is the trade: one round reports gaps a second
  would partly fill. It is about one standard deviation, and the direction to
  watch.
* Latency is compared on corpus cases only. Replayed calls return at once,
  so a run that replays first-round searches looks faster than it is; in the
  confirmation's second and third trials the corpus cases ran their searches
  live.
* Web searches remain the largest cost. Prompt caching (ROADMAP) is the next
  lever this measurement makes visible.