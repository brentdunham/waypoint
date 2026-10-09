# Case study: a low `grounded` score, and what the eval loop found

October 2026. The research agent's lowest score was `grounded`: whether each
claim in a briefing is supported by a source it cites. In the baseline
accepted on 2026-10-08 it averaged 2.48 out of 4. Everything else was 3.3 or
higher. This is how that number was measured, what turned out to be wrong, and
what changed. Every number below comes from a scorecard in
`evals/results/research/` (Sonnet 5 for planner, critic and writer, Haiku 5.5
for searcher and verifier, 8 cases x 3 trials unless stated).

## Summary

| | before | after |
|---|---|---|
| `grounded` (Opus 4.5 judge, calibrated) | 3.67 | 3.86 |
| `claims_cited`: briefings with 80%+ of claim sentences cited | 48% | 95% |
| `calibrated` / `covers` / `useful` | 3.10 / 3.62 / 3.86 | 3.14 / 3.48 / 3.90 |
| Added cost per research run | | $0.021 |
| Added latency per run | | ~21 s, +7 s when a sentence is rewritten |

Two findings, in order of how much they mattered:

1. **Most of the 2.48 was the judge.** Recalibrated, the judge scored the
   *unchanged* briefings 3.67. The old judge (Haiku 4.5) saw source titles but
   not what the sources said, so it could not verify a claim and marked it
   down. It also drifted from the rubric into grading source quality.
2. **The real defect was uncited claims, and some wrong citations.** Half the
   briefings left more than 20% of their factual sentences without an `[n]`,
   and some sentences cited a source from the right finding that did not say
   the thing claimed. A verify step after the writer fixed most of it.

## 1. Measure before changing anything

The baseline judge's reasoning for all 21 briefings (7 web cases x 3 trials)
was read, not just the scores. The deductions fell into five groups:

* uncited summary and negative sentences ("No study comparing X was found");
* a citation to the right finding but the wrong source within it;
* claims the judge could not check, because it saw only a source's title;
* deductions for source quality (vendor blogs), which the rubric does not ask
  about;
* one claim marked down as "future-dated", which was a 2026 source.

Only the first two are faults in the briefing. The next three are faults in
the measurement, so the measurement had to be fixed first, or any change to the
agent would be scored by an instrument that could not see it.

## 2. Fix the instrument

* **Give the judge the evidence.** Anthropic's web search returns, with each
  citation, the text the answer quoted (`cited_text`). Findings now keep up to
  three quotes per source, and the judge sees them next to each `[n]`. That
  changed no agent call, so the existing baseline was re-scored from the
  replay store at no agent cost.
* **Rewrite the rubric** to judge support and attribution only, to separate
  "a less specific source" (3) from "a source about something else" (2), and
  to allow statements about the research itself without a citation.
* **Add a deterministic check.** `claims_cited` passes when at least 80% of
  a briefing's claim-length sentences (8+ words) carry an `[n]`. It needs no
  judge, so it cannot drift.
* **Relabel the calibration set.** Twelve new `grounded` examples, built from
  real briefing paragraphs in groups of three: the real text; the same text
  with citations corrected; and one with a planted error (an invented claim, a
  citation swapped to an unrelated source, or citations removed). Labels were
  agreed by a person before any judge saw them.

Calibration then failed, and that was the most useful result of the whole
exercise:

| judge | agreement (pass/fail) | mean abs. error | misses |
|---|---|---|---|
| Haiku 4.5 | 67%, then 75% on a rerun | 0.92, 0.75 | missed the planted wrong citation and the removed citations |
| Sonnet 4.5 | 75% | 0.42 | three borderline real paragraphs, each by one point |
| Opus 4.5 | **92%** | **0.17** | one borderline real paragraph, by one point |

Haiku is fine for the other four criteria (90% or better) but cannot tell a
citation to the wrong page from a right one, which is the thing `grounded`
exists to catch. `grounded` now routes to Opus 4.5 (`routing.json`); the rest
stay on Haiku. Opus was also preferred over Sonnet 5 on principle: Sonnet 5
writes the briefings, and a judge should not grade its own model's work. Haiku
also gave different agreement on two runs over the same labels, which is a
reminder that one calibration run is a sample too.

Re-judged with the calibrated judge and replayed agent calls, the unchanged
briefings scored **3.67**. That run became the "before" baseline.

## 3. Fix the agent

The remaining defect showed in `claims_cited` (48%) and in the judge's notes:
sentences with no citation, and citations to a neighbouring source. The fix
is a `verify` node after the writer (ARCHITECTURE.md §4c):

1. Split the briefing into sentences, keeping trailing `[n]` marks with the
   sentence they follow.
2. Ask the verifier (Haiku 5.5) for the sentences that are *not* supported as
   cited: `wrong_source`, `needs_citation` or `unsupported`, with the source
   numbers that do support it.
3. Fix the first two in code by replacing the sentence's `[n]` marks, and only
   with numbers that are real sources.
4. Send only `unsupported` sentences back to the writer's model for a
   replacement each, and splice the replacements in by index.

It took four iterations, each a one-trial run that replayed every agent call
except the new ones:

| iteration | result | change |
|---|---|---|
| 1 | verifier failed on 5 of 7 cases: empty or truncated JSON | Haiku 5.5 reasons before answering; on a 30k-character prompt, reasoning plus a verdict for each of 20–40 sentences ran past `max_tokens`. Ask only for the exceptions, and raise `max_tokens`. |
| 2 | verifier worked; `covers` fell to 1 and `useful` to 2 | The writer, asked for the whole revised briefing, returned only the rewritten sentences. Ask for one replacement per flagged sentence and splice it in, so a revision cannot touch anything else. |
| 3 | `claims_cited` 86% (before: 48%), `grounded` 4.0; one case still failed in the verifier | `calibrated` dropped by 1 in four cases, but one of them had identical wording before and after, so this was judge noise; the 3-trial run confirmed it. The failure was reasoning hitting the token cap again. Raise to 32k, and on malformed output retry once and then ship the draft unverified with `verification.skipped`. A quality step must never cost the user a briefing. |
| 4 | the gated 3-trial run in the summary | no regressions, 0 failed runs |

What the verifier does to a briefing is mostly re-citation. Across the 21
briefings of the final run it re-cited a median of 37% of sentences (7% to
73%), found 0–3 unsupported sentences (none in 6 briefings) and rewrote every
one of them; no run skipped verification. The rewrites are narrower claims, not new ones. For example, "No
SMR-specific interconnection data exists" became "The best available
interconnection figures are for solar and general generation, not SMRs
specifically: one analysis found a median time … approaching five years".

## 4. Does it generalise?

The iterations replayed the same planner, searcher and critic outputs, so the
verifier was tuned against one set of briefings per trial. A final run with
`--fresh` resampled every model call on new searches:

A fresh run (one trial) is a new sample of the whole pipeline, and this one
was weaker upstream: `findings_cited` was 57% against the baseline's 81%, so
more findings rested on uncited fallback sources. Against the baseline the
gate flagged `grounded` (3.86 → 3.57) and `calibrated` (3.14 → 2.71). That
compares a new sample with an old one, so it cannot say whether the verifier
or the research caused the drop.

The fresh run recorded every call, so the same briefings were then replayed
with `RESEARCH__VERIFY_CITATIONS=false`. Only the judge ran live, and the only
difference between the two columns is the verifier:

| same fresh briefings | verify off | verify on |
|---|---|---|
| `grounded` | 3.43 (86% pass) | 3.57 (100% pass) |
| `claims_cited` | 71% | 86% |
| `calibrated` / `covers` / `useful` | 2.86 / 3.57 / 3.86 | 2.71 / 3.43 / 3.71 |

Case by case, the verifier lifted one failing briefing (sodium-ion, `grounded`
2 → 3). The three drops are each one point on one case (eu-ai-act `covers` and
`calibrated`, cold-climate `useful`); with seven briefings, one point is 0.14.
The gate's flags were the weaker sample: without the verifier the same
briefings ground worse still. A one-trial fresh run against a three-trial
baseline is a noisy comparison, and pairing on replayed calls is what made
this one readable.

One pattern does repeat: `covers` is 0.14 lower with the verifier in both
comparisons. Deleting an unsupported sentence sometimes deletes a point the
reader wanted, even when nothing supported it.

## 5. What it cost, and what is left

Replay meant only the judge and the new node ran live during the work: the
agent-side cost of the iterations and the gated three-trial run was $0.86 in
total, and the fresh run, which pays for every search, $2.16. Judge calls are
not priced on scorecards; Opus 4.5 on `grounded` is the largest of them, about
21 calls per three-trial run. The one expensive mistake was nearly made:
a run with a new `--out` directory started with an empty replay store and
began re-running every agent call. It was stopped within seconds; the replay
store lives beside `--out`, which TESTING.md now says plainly.

Open:

* `covers` is 0.14 lower with the verifier, in both the gated run and the
  paired fresh comparison: within tolerance, and consistent. Rewriting an
  unsupported sentence to what *is* supported, rather than deleting it, is the
  next thing to try.
* The verifier adds ~21 s per run, nearly all of it Haiku's reasoning. An
  effort or thinking budget, when the API offers one for Haiku 5.5, is the
  first thing to try.
* High re-citation rates (up to 73% of a briefing's sentences) mean the writer
  attributes loosely. Moving the fix upstream, by giving the writer the quotes
  it now only gets in verification, could make the verifier mostly a no-op.
* `findings_cited` (81%, 57% on the fresh sample) is unchanged: it measures
  the searcher, which this work did not touch, and it moves `grounded` more
  than the verifier does.
