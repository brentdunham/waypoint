"""Aggregate results, identify the configuration, and gate against a baseline."""

import datetime as dt
import hashlib
import json
import math
import statistics
import subprocess
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

from evals.cases import DATASETS, ROOT, Case
from evals.checks import CheckResult
from evals.driver import Transcript
from evals.judge import Grade, rubric_hash
from waypoint.agents.registry import get_spec
from waypoint.settings import Settings

BASELINES = ROOT / "baselines"
RESULTS = ROOT / "results"
PRICES = ROOT / "prices.toml"

CHECK_FLOOR = 0.10
"""Smallest pass-rate drop that fails the gate, whatever the observed spread."""
CRITERION_FLOOR = 0.25
"""Smallest drop in a criterion's mean (1-4 scale) that fails the gate."""
MIN_BASELINE_TRIALS = 3
"""``accept`` refuses fewer: the gate's tolerance is the baseline's spread
across trials, and one trial has none. Iterate on one trial; accept on three."""


@dataclass(slots=True)
class CaseResult:
    case: Case
    transcript: Transcript
    checks: list[CheckResult]
    grades: list[Grade] = field(default_factory=list)


# -- identity -----------------------------------------------------------------------


def prompts_of(agent: str) -> dict[str, str]:
    """Every module-level prompt constant in the agent's graph module."""
    module = import_module(get_spec(agent).builder.__module__)
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str) and len(value) > 20
    }


def fingerprint(settings: Settings, agent: str, models: dict[str, str]) -> str:
    """Changes when anything that could change a score changes.

    Prompts, the resolved model per role, the agent's settings, the judge
    model, the rubric and the dataset. Not the code around them: that is what
    the git SHA is for, once there is one.
    """
    parts = {
        "prompts": prompts_of(agent),
        "models": models,
        "research": settings.research.model_dump() if agent == "research" else None,
        "max_tokens": settings.max_tokens,
        "judge": settings.evals.judge_model,
        "rubric": rubric_hash(agent),
        "dataset": hashlib.sha256((DATASETS / f"{agent}.jsonl").read_bytes()).hexdigest(),
    }
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return out.stdout.strip() or None


# -- aggregation ------------------------------------------------------------------------


def _spread(values: list[float]) -> dict[str, float]:
    return {
        "mean": round(statistics.fmean(values), 4) if values else math.nan,
        "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
    }


def _prices() -> dict[str, dict[str, float]]:
    if not PRICES.exists():
        return {}
    data = tomllib.loads(PRICES.read_text(encoding="utf-8"))
    return {k: v for k, v in data.get("models", {}).items() if isinstance(v, dict)}


def call_cost(
    price: Mapping[str, float], input_tokens: int, output_tokens: int, searches: int
) -> float:
    """USD for one call. ``long_*`` rates apply above ``long_threshold`` input tokens.

    A web-search call is several requests billed as one call here, so a call
    whose summed input crosses the threshold is priced at the long rate even
    when each request stayed under it. That overstates, never understates.
    """
    long = "long_threshold" in price and input_tokens > price["long_threshold"]
    rate_in = price["long_input"] if long else price["input"]
    rate_out = price["long_output"] if long else price["output"]
    tokens = input_tokens * rate_in + output_tokens * rate_out
    return tokens / 1_000_000 + searches * price.get("search", 0.0)


def usage_summary(results: list[CaseResult]) -> dict[str, Any]:
    prices = _prices()
    roles: dict[str, dict[str, Any]] = {}
    total_cost: float | None = 0.0
    for result in results:
        for call in result.transcript.calls:
            row = roles.setdefault(
                call.role,
                {
                    "models": set(),
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "searches": 0,
                    "latency_s": 0.0,
                },
            )
            row["models"].add(call.model)
            row["calls"] += 1
            row["input_tokens"] += call.input_tokens
            row["output_tokens"] += call.output_tokens
            row["searches"] += call.searches
            row["latency_s"] += call.latency_s
            price = prices.get(call.model)
            if price is None:
                total_cost = None
            elif total_cost is not None:
                total_cost += call_cost(price, call.input_tokens, call.output_tokens, call.searches)
    replayed: dict[str, int] = defaultdict(int)
    for result in results:
        for role, n in result.transcript.replayed.items():
            replayed[role] += n
    runs = len(results) or 1
    for row in roles.values():
        row["models"] = sorted(row["models"])
        row["latency_s_per_call"] = round(row.pop("latency_s") / max(row["calls"], 1), 2)
        row["input_tokens_per_run"] = round(row["input_tokens"] / runs)
        row["output_tokens_per_run"] = round(row["output_tokens"] / runs)
    return {
        "roles": roles,
        "replayed": dict(sorted(replayed.items())),
        "cost_usd": round(total_cost, 4) if total_cost is not None else None,
        "cost_usd_per_run": round(total_cost / runs, 4) if total_cost is not None else None,
    }


def build_scorecard(
    settings: Settings,
    agent: str,
    models: dict[str, str],
    results: list[CaseResult],
    *,
    trials: int,
    calibration: Mapping[str, str],
) -> dict[str, Any]:
    by_trial_checks: dict[str, dict[int, list[bool]]] = defaultdict(lambda: defaultdict(list))
    by_trial_scores: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    skipped: dict[str, int] = defaultdict(int)
    for r in results:
        trial = r.transcript.trial
        for c in r.checks:
            if c.passed is None:
                skipped[c.name] += 1
            else:
                by_trial_checks[c.name][trial].append(c.passed)
        for g in r.grades:
            if g.score is not None:
                by_trial_scores[g.criterion][trial].append(g.score)

    checks = {}
    for name, trials_ in sorted(by_trial_checks.items()):
        rates = [sum(v) / len(v) for v in trials_.values() if v]
        flat = [x for v in trials_.values() for x in v]
        checks[name] = {
            "pass_rate": _spread(rates),
            "passed": sum(flat),
            "applicable": len(flat),
            "not_applicable": skipped.get(name, 0),
        }

    criteria = {}
    for name in sorted({g.criterion for r in results for g in r.grades}):
        scores = by_trial_scores.get(name, {})
        means = [statistics.fmean(v) for v in scores.values() if v]
        flat_scores = [x for v in scores.values() for x in v]
        criteria[name] = {
            "mean": _spread(means),
            "pass_rate": (
                round(sum(s >= 3 for s in flat_scores) / len(flat_scores), 4)
                if flat_scores
                else None
            ),
            "graded": len(flat_scores),
            "judge": calibration.get(name, "uncalibrated"),
        }

    return {
        "agent": agent,
        "date": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "fingerprint": fingerprint(settings, agent, models),
        "git_sha": git_sha(),
        "models": models,
        "judge_model": settings.evals.judge_model,
        "settings": settings.research.model_dump() if agent == "research" else {},
        "cases": len({r.case.id for r in results}),
        "trials": trials,
        "errors": sum(1 for r in results if r.transcript.error),
        "checks": checks,
        "criteria": criteria,
        "usage": usage_summary(results),
        "elapsed_s_per_run": _spread([r.transcript.elapsed_s for r in results]),
        "runs": [
            {
                "case": r.case.id,
                "trial": r.transcript.trial,
                "error": r.transcript.error,
                "elapsed_s": round(r.transcript.elapsed_s, 1),
                "failed_checks": {c.name: c.detail for c in r.checks if c.passed is False},
                "grades": {
                    g.criterion: {"score": g.score, "reasoning": g.reasoning, "judge": g.judge}
                    for g in r.grades
                },
            }
            for r in results
        ],
    }


# -- baseline gate -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Regression:
    metric: str
    baseline: float
    current: float
    tolerance: float

    def __str__(self) -> str:
        return (
            f"{self.metric}: {self.baseline:.2f} -> {self.current:.2f} "
            f"(drop {self.baseline - self.current:.2f} > tolerance {self.tolerance:.2f})"
        )


def regressions(baseline: dict[str, Any], current: dict[str, Any]) -> list[Regression]:
    """Metrics that dropped by more than their tolerance.

    Tolerance is twice the baseline's spread across trials, never below a
    floor, so a one-trial baseline is gated on the floor alone and noise in a
    multi-trial one does not fail the gate.
    """
    found: list[Regression] = []
    for name, base in baseline.get("checks", {}).items():
        now = current.get("checks", {}).get(name)
        if now is None:
            continue
        b, c = base["pass_rate"]["mean"], now["pass_rate"]["mean"]
        tol = max(CHECK_FLOOR, 2 * base["pass_rate"]["stdev"])
        if b - c > tol:
            found.append(Regression(f"check {name} pass rate", b, c, tol))
    for name, base in baseline.get("criteria", {}).items():
        now = current.get("criteria", {}).get(name)
        if now is None or math.isnan(base["mean"]["mean"]) or math.isnan(now["mean"]["mean"]):
            continue
        b, c = base["mean"]["mean"], now["mean"]["mean"]
        tol = max(CRITERION_FLOOR, 2 * base["mean"]["stdev"])
        if b - c > tol:
            found.append(Regression(f"criterion {name} mean", b, c, tol))
    return found


def load_baseline(agent: str) -> dict[str, Any] | None:
    path = BASELINES / f"{agent}.json"
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


class AcceptError(ValueError):
    """The scorecard cannot become a baseline."""


def accept(agent: str, scorecard_path: Path, *, min_trials: int = MIN_BASELINE_TRIALS) -> Path:
    card = json.loads(scorecard_path.read_text(encoding="utf-8"))
    if card.get("agent") != agent:
        raise AcceptError(f"{scorecard_path} is a scorecard for {card.get('agent')!r}")
    if int(card.get("trials", 0)) < min_trials:
        raise AcceptError(
            f"{scorecard_path} ran {card.get('trials')} trial(s); a baseline needs "
            f"{min_trials} so the gate has a spread to tolerate. Rerun with "
            f"--trials {min_trials} (replay makes the repeat cheap)."
        )
    card.pop("runs", None)  # the baseline is the summary, not the transcripts
    BASELINES.mkdir(exist_ok=True)
    target = BASELINES / f"{agent}.json"
    target.write_text(json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


# -- rendering ------------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if isinstance(value, dict) and "mean" in value:
        mean, sd = value["mean"], value["stdev"]
        return "n/a" if isinstance(mean, float) and math.isnan(mean) else f"{mean:.2f} ± {sd:.2f}"
    return "n/a" if value is None else str(value)


def render_markdown(
    card: dict[str, Any],
    found: list[Regression] | None = None,
    baseline: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"# {card['agent']} scorecard",
        "",
        f"{card['date']} · fingerprint `{card['fingerprint']}`"
        + (f" · git `{card['git_sha']}`" if card.get("git_sha") else ""),
        f"{card['cases']} cases x {card['trials']} trials · {card['errors']} failed runs · "
        f"judge `{card['judge_model']}`",
        "",
        "Models: " + ", ".join(f"{role} `{m}`" for role, m in card["models"].items()),
        "",
        "## Checks",
        "",
        "| check | pass rate | passed / applicable |",
        "|---|---|---|",
    ]
    for name, row in card["checks"].items():
        lines.append(
            f"| {name} | {_fmt(row['pass_rate'])} | {row['passed']} / {row['applicable']} |"
        )
    lines += [
        "",
        "## Judge (1-4, pass ≥ 3)",
        "",
        "| criterion | mean | pass rate | judge |",
        "|---|---|---|---|",
    ]
    for name, row in card["criteria"].items():
        lines.append(
            f"| {name} | {_fmt(row['mean'])} | {_fmt(row['pass_rate'])} | {row['judge']} |"
        )
    usage = card["usage"]
    lines += [
        "",
        "## Usage per role",
        "",
        "| role | model | calls | in tok/run | out tok/run | searches | s/call |",
        "|---|---|---|---|---|---|---|",
    ]
    for role, row in usage["roles"].items():
        lines.append(
            f"| {role} | {', '.join(row['models'])} | {row['calls']} | "
            f"{row['input_tokens_per_run']} | {row['output_tokens_per_run']} | "
            f"{row['searches']} | {row['latency_s_per_call']} |"
        )
    cost = usage["cost_usd_per_run"]
    lines += [
        "",
        f"Cost per run: {'$' + format(cost, '.4f') if cost is not None else 'unknown (model not in prices.toml)'}",
    ]
    if replayed := usage.get("replayed"):
        lines += [
            "",
            "Replayed, not billed and not counted above: "
            + ", ".join(f"{role} {n}" for role, n in replayed.items())
            + " calls.",
        ]
    lines += ["", "Grounded means grounded in what the agent retrieved; the judge does not browse."]
    if found is not None:
        lines += ["", "## Baseline gate", ""]
        if baseline is not None:
            lines.append(
                f"Baseline: fingerprint `{baseline.get('fingerprint')}`, "
                f"accepted from a run on {baseline.get('date', 'unknown date')}"
                + (f" at git `{baseline['git_sha']}`" if baseline.get("git_sha") else "")
                + "."
            )
            if baseline.get("fingerprint") != card["fingerprint"]:
                lines.append(
                    "Configuration differs from the baseline (prompts, models, settings, "
                    "judge, rubric or dataset), so this compares two different setups. "
                    "Expected right after such a change; if it persists, accept a new baseline."
                )
            lines.append("")
        lines += [f"- REGRESSION {r}" for r in found] or ["- no regressions"]
    return "\n".join(lines) + "\n"
