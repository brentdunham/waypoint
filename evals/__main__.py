"""``uv run python -m evals``: run, compare, calibrate, accept.

Reads the same .env as the service (ANTHROPIC_API_KEY, MODEL, MODELS__*,
RESEARCH__*, EVALS__*). ``--fake`` runs everything on the scripted model,
which checks the harness, not the agents.

Model calls are recorded and replayed (``evals/replay.py``): a rerun pays only
for calls whose input or setup changed. ``--fresh`` resamples everything.
Iterate on one trial; ``accept`` needs a three-trial scorecard.
"""

import argparse
import asyncio
import datetime as dt
import io
import json
import sys
from pathlib import Path
from typing import Any

from evals.calibrate import calibrate
from evals.cases import load_dataset
from evals.checks import run_checks
from evals.driver import Transcript
from evals.judge import ROUTING, assign, load_rubric
from evals.replay import FILENAME, ReplayStore
from evals.retrieval import add_parser as add_retrieval_parser
from evals.runner import compare, evaluate, parse_overrides, resolved, self_judging
from evals.scorecard import (
    MIN_BASELINE_TRIALS,
    RESULTS,
    AcceptError,
    CaseResult,
    accept,
    build_scorecard,
    failed_checks,
    fingerprint,
    git_sha,
    load_baseline,
    regressions,
    render_markdown,
    summarize_checks,
)
from waypoint.agents.models import check_model_config
from waypoint.agents.registry import listed_agents, roles_by_agent
from waypoint.settings import Settings

AGENTS = listed_agents()
"""Agents with datasets. Unlisted ones (``ingest``) are measured by their own evals."""


def _settings(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {}
    if args.fake:
        overrides["fake_model"] = True
    settings = Settings(**overrides)
    check_model_config(settings, roles_by_agent())
    if not settings.fake_model and not settings.anthropic_api_key:
        sys.exit("ANTHROPIC_API_KEY is not set. Use --fake to exercise the harness offline.")
    return settings


def _agents(name: str) -> list[str]:
    return list(AGENTS) if name == "all" else [name]


def _cases(agent: str, args: argparse.Namespace) -> list[Any]:
    cases = load_dataset(agent)
    if args.case:
        cases = [c for c in cases if c.id in set(args.case)]
    if args.tag:
        cases = [c for c in cases if set(args.tag) & set(c.tags)]
    if not cases:
        sys.exit(f"{agent}: no cases match --case/--tag")
    return cases


def _replay(args: argparse.Namespace) -> ReplayStore | None:
    if args.no_replay or args.fake:
        return None  # the scripted model is free, and its answers are not worth keeping
    store = ReplayStore((args.out or RESULTS) / FILENAME)
    print(f"replay: {store.path}{' (fresh: recording only)' if args.fresh else ''}")
    return store


def _progress(result: CaseResult) -> None:
    t = result.transcript
    failed = [c.name for c in result.checks if c.passed is False]
    scores = " ".join(
        f"{g.criterion}={g.score if g.score is not None else '-'}" for g in result.grades
    )
    status = (
        "ERROR " + (t.error or "") if t.error else ("FAIL " + ",".join(failed) if failed else "ok")
    )
    replayed = f" replayed {sum(t.replayed.values())}/{sum(t.replayed.values()) + len(t.calls)}"
    print(
        f"  {t.case_id}#{t.trial} {t.elapsed_s:5.1f}s {status} {scores}"
        f"{replayed if t.replayed else ''}".rstrip(),
        flush=True,
    )


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    replay = _replay(args)
    failed = False
    for agent in _agents(args.agent):
        models = resolved(settings, agent, parse_overrides(agent, args.model))
        cases = _cases(agent, args)
        print(f"{agent}: {len(cases)} cases x {args.trials} trials, models {models}")
        for warning in self_judging(settings, agent, models):
            print(f"  warning: self-judging, {warning}")
        results = asyncio.run(
            evaluate(
                settings,
                agent,
                cases,
                models,
                trials=args.trials,
                concurrency=args.concurrency,
                grade=not args.no_judge,
                progress=_progress,
                replay=replay,
                fresh=args.fresh,
            )
        )
        calibration = {name: assign(settings, agent, name).status for name in load_rubric(agent)}
        card = build_scorecard(
            settings, agent, models, results, trials=args.trials, calibration=calibration
        )
        found = baseline = None
        if args.against_baseline:
            baseline = load_baseline(agent)
            if baseline is None:
                print(f"  no baseline for {agent}; run `evals accept` on a scorecard first")
            else:
                found = regressions(baseline, card)
                failed |= bool(found)
                if baseline.get("fingerprint") != card["fingerprint"]:
                    print(
                        f"  warning: configuration {card['fingerprint']} differs from "
                        f"baseline {baseline.get('fingerprint')}"
                    )

        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = (args.out or RESULTS) / agent / f"{stamp}-{card['fingerprint']}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "scorecard.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
        summary = render_markdown(card, found, baseline)
        (out / "summary.md").write_text(summary, encoding="utf-8")
        with (out / "transcripts.jsonl").open("w", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(r.transcript.to_dict(), ensure_ascii=False) + "\n")
        print(summary)
        print(f"written to {out}")
    return 1 if failed else 0


def cmd_compare(args: argparse.Namespace) -> int:
    settings = _settings(args)
    agent = args.agent
    a = resolved(settings, agent, parse_overrides(agent, args.a))
    b = resolved(settings, agent, parse_overrides(agent, args.b))
    if a == b:
        sys.exit("A and B resolve to the same models; nothing to compare.")
    cases = _cases(agent, args)
    print(f"{agent}: A {a}\n{' ' * len(agent)}  B {b}")
    report = asyncio.run(
        compare(
            settings, agent, cases, a, b, trials=args.trials, concurrency=args.concurrency,
            replay=_replay(args), fresh=args.fresh,
        )
    )  # fmt: skip
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = (args.out or RESULTS) / agent / f"{stamp}-compare"
    out.mkdir(parents=True, exist_ok=True)
    (out / "compare.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = ["| criterion | A wins | B wins | ties | B win rate |", "|---|---|---|---|---|"]
    for name, row in report["criteria"].items():
        lines.append(
            f"| {name} | {row['a_wins']} | {row['b_wins']} | {row['ties']} | {row['b_win_rate']} |"
        )
    for side in ("a", "b"):
        r = report[side]
        cost = r["usage"]["cost_usd_per_run"]
        lines.append(
            f"\n{side.upper()}: checks {r['check_pass_rate']:.0%}, {r['elapsed_s']}s/run, "
            f"cost/run {'$' + format(cost, '.4f') if cost is not None else 'unknown'}, "
            f"errors {r['errors']}"
        )
    print("\n".join(lines))
    print(f"written to {out}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    settings = _settings(args)
    for agent in _agents(args.agent):
        print(
            f"{agent}: judge {settings.evals.judge_model}, threshold {settings.evals.min_agreement:.0%}"
        )
        for row in asyncio.run(calibrate(settings, agent)):
            print(
                f"  {row.criterion:<14} {row.model:<22} agreement {row.agreement:.0%} "
                f"(n={row.examples}, MAE {row.mean_abs_error:.2f})"
            )
            for miss in row.disagreements:
                print(f"      {miss}")
        print(f"  routing written to {ROUTING}")
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    try:
        target = accept(args.agent, Path(args.scorecard), min_trials=args.min_trials)
    except AcceptError as exc:
        print(f"not accepted: {exc}", file=sys.stderr)
        return 2
    print(f"baseline for {args.agent} is now {target}")
    return 0


def recheck(results_dir: Path, settings: Settings) -> dict[str, Any]:
    """Re-run the programmatic checks on a results directory's saved transcripts.

    Checks read only the transcript, so a corrected check can be applied to a
    past run without calling a model. ``checks`` and each run's
    ``failed_checks`` are replaced; grades, usage and the fingerprint are kept,
    and ``rechecked`` records when and at which commit. The run's own research
    settings are used, not the current ones.
    """
    path = results_dir / "scorecard.json"
    card: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    agent = card["agent"]
    if agent == "research" and card.get("settings"):
        research = type(settings.research).model_validate(card["settings"])
        settings = settings.model_copy(update={"research": research})
    cases = {c.id: c for c in load_dataset(agent)}
    results = []
    with (results_dir / "transcripts.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            t = Transcript.from_dict(json.loads(line))
            case = cases[t.case_id]
            results.append(CaseResult(case, t, run_checks(t, case.checks, settings)))
    by_run = {(r.case.id, r.transcript.trial): r for r in results}
    card["checks"] = summarize_checks(results)
    for run in card["runs"]:
        run["failed_checks"] = failed_checks(by_run[(run["case"], run["trial"])])
    card["rechecked"] = {
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
    }
    path.write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    (results_dir / "summary.md").write_text(render_markdown(card, None, None), encoding="utf-8")
    return card


def cmd_recheck(args: argparse.Namespace) -> int:
    for results_dir in args.results:
        card = recheck(Path(results_dir), Settings(fake_model=True))
        rates = ", ".join(f"{k} {v['passed']}/{v['applicable']}" for k, v in card["checks"].items())
        print(f"{results_dir}: {rates}")
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    settings = _settings(args)
    for agent in _agents(args.agent):
        models = resolved(settings, agent, parse_overrides(agent, args.model))
        print(agent, fingerprint(settings, agent, models), models)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Summaries contain non-ASCII (≥, ±). A Windows console or redirect defaults
    # to cp1252 and would crash the final print after the files are written.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    agent_choices = [*AGENTS, "all"]

    def common(p: argparse.ArgumentParser, *, all_agents: bool = True) -> None:
        p.add_argument("--agent", default="all" if all_agents else None, required=not all_agents,
                       choices=agent_choices if all_agents else list(AGENTS))  # fmt: skip
        p.add_argument("--fake", action="store_true", help="scripted model; tests the harness")

    def selection(p: argparse.ArgumentParser) -> None:
        p.add_argument("--case", action="append", default=[], help="case id (repeatable)")
        p.add_argument("--tag", action="append", default=[], help="cases with this tag")
        p.add_argument("--trials", type=int, default=1,
                       help=f"1 to iterate; {MIN_BASELINE_TRIALS} for a scorecard to accept")  # fmt: skip
        p.add_argument("--concurrency", type=int, default=2)
        p.add_argument("--out", type=Path, default=None, help="results directory")
        p.add_argument("--fresh", action="store_true",
                       help="resample every model call; record, do not replay")  # fmt: skip
        p.add_argument("--no-replay", action="store_true",
                       help="neither replay nor record model calls")  # fmt: skip

    run = sub.add_parser("run", help="run the dataset and write a scorecard")
    common(run)
    selection(run)
    run.add_argument("--model", action="append", default=[],
                     help="role=model or agent.role=model; default=model for every role")  # fmt: skip
    run.add_argument("--no-judge", action="store_true", help="programmatic checks only")
    run.add_argument("--against-baseline", action="store_true",
                     help="exit 1 when a metric drops beyond tolerance")  # fmt: skip
    run.set_defaults(fn=cmd_run)

    cmp_ = sub.add_parser("compare", help="two model configurations, judged pairwise")
    common(cmp_, all_agents=False)
    selection(cmp_)
    cmp_.add_argument("--a", action="append", default=[], help="role=model for A (default: config)")
    cmp_.add_argument("--b", action="append", default=[], required=True, help="role=model for B")
    cmp_.set_defaults(fn=cmd_compare)

    cal = sub.add_parser("calibrate", help="measure the judge against labelled examples")
    common(cal)
    cal.set_defaults(fn=cmd_calibrate)

    acc = sub.add_parser("accept", help="make a scorecard the baseline")
    acc.add_argument("--agent", required=True, choices=[*AGENTS, "retrieval"])
    acc.add_argument("scorecard", help="path to a scorecard.json")
    acc.add_argument("--min-trials", type=int, default=MIN_BASELINE_TRIALS,
                     help=f"refuse scorecards with fewer trials (default {MIN_BASELINE_TRIALS})")  # fmt: skip
    acc.set_defaults(fn=cmd_accept)

    rec = sub.add_parser("recheck", help="re-run the checks on saved transcripts (no model calls)")
    rec.add_argument("results", nargs="+", help="results directories (with transcripts.jsonl)")
    rec.set_defaults(fn=cmd_recheck)

    add_retrieval_parser(sub)

    fp = sub.add_parser("fingerprint", help="print the configuration fingerprint")
    common(fp)
    fp.add_argument("--model", action="append", default=[])
    fp.set_defaults(fn=cmd_fingerprint)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
