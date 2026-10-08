"""Run a dataset, check and grade it, and compare two configurations."""

import asyncio
import random
import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Any

from evals.cases import Case
from evals.checks import run_checks
from evals.driver import run_case
from evals.judge import Judge, assign, load_rubric, material_for
from evals.scorecard import CaseResult, usage_summary
from waypoint.agents.models import resolve_models
from waypoint.agents.registry import get_spec
from waypoint.settings import Settings

TARGET_ROLE = {"plan": "planner", "briefing": "writer", "reply": "assistant"}
"""Which role produced what each rubric target shows the judge."""


def parse_overrides(agent: str, specs: Sequence[str]) -> dict[str, str]:
    """``role=model`` or ``agent.role=model``; ``default=model`` covers every role."""
    roles = {*get_spec(agent).model_roles, "default"}
    out: dict[str, str] = {}
    for spec in specs:
        key, sep, model = spec.partition("=")
        if not sep or not model.strip():
            raise ValueError(f"--model {spec!r}: expected role=model")
        scope, _, role = key.strip().lower().rpartition(".")
        if scope and scope != agent:
            continue  # aimed at another agent in an --agent all run
        if role not in roles:
            raise ValueError(f"--model {spec!r}: {agent!r} has roles {sorted(roles)}")
        out[role] = model.strip()
    return out


def self_judging(settings: Settings, agent: str, models: dict[str, str]) -> list[str]:
    """Criteria whose judge is the model that wrote what it grades."""
    warnings = []
    for name, criterion in load_rubric(agent).items():
        role = TARGET_ROLE[criterion.target]
        judge = assign(settings, agent, name).model
        if judge is not None and models.get(role) == judge:
            warnings.append(f"{name}: judge {judge} also plays {role}")
    return warnings


async def evaluate(
    settings: Settings,
    agent: str,
    cases: list[Case],
    models: dict[str, str],
    *,
    trials: int = 1,
    concurrency: int = 2,
    grade: bool = True,
    progress: Any = None,
) -> list[CaseResult]:
    gate = asyncio.Semaphore(concurrency)
    judge = Judge(settings, concurrency=concurrency * 2)
    rubric = load_rubric(agent)

    async def one(case: Case, trial: int) -> CaseResult:
        async with gate:
            transcript = await run_case(settings, agent, case, models=models, trial=trial)
        result = CaseResult(case, transcript, run_checks(transcript, case.checks, settings))
        if grade:
            result.grades = await judge.grade_transcript(agent, transcript, case, rubric)
        if progress:
            progress(result)
        return result

    jobs = [one(case, trial) for trial in range(trials) for case in cases]
    return list(await asyncio.gather(*jobs))


def resolved(settings: Settings, agent: str, overrides: dict[str, str]) -> dict[str, str]:
    return resolve_models(settings, agent, get_spec(agent).model_roles, overrides)


async def compare(
    settings: Settings,
    agent: str,
    cases: list[Case],
    a: dict[str, str],
    b: dict[str, str],
    *,
    trials: int = 1,
    concurrency: int = 2,
    seed: int = 7,
) -> dict[str, Any]:
    """Run both configurations and judge each criterion pairwise, A vs B."""
    results_a = await evaluate(
        settings, agent, cases, a, trials=trials, concurrency=concurrency, grade=False
    )
    results_b = await evaluate(
        settings, agent, cases, b, trials=trials, concurrency=concurrency, grade=False
    )
    judge = Judge(settings, concurrency=concurrency * 2)
    rubric = load_rubric(agent)
    rng = random.Random(seed)
    tallies: dict[str, Counter[str]] = {}
    pairs = zip(results_a, results_b, strict=True)

    async def judge_pair(ra: CaseResult, rb: CaseResult) -> list[tuple[str, str]]:
        if ra.transcript.error or rb.transcript.error or not ra.case.judge:
            return []
        out: list[tuple[str, str]] = []
        for name in ra.case.criteria or list(rubric):
            model = assign(settings, agent, name).model
            if model is None:
                continue
            criterion = rubric[name]
            verdict = await judge.prefer(
                criterion,
                material_for(criterion.target, ra.transcript, ra.case),
                material_for(criterion.target, rb.transcript, rb.case),
                model=model,
                rng=rng,
            )
            if verdict is not None:
                out.append((name, verdict))
        return out

    for verdicts in await asyncio.gather(*(judge_pair(x, y) for x, y in pairs)):
        for name, verdict in verdicts:
            tallies.setdefault(name, Counter())[verdict] += 1

    def checks_rate(results: list[CaseResult]) -> float:
        flat = [c.passed for r in results for c in r.checks if c.passed is not None]
        return round(sum(flat) / len(flat), 4) if flat else 0.0

    def elapsed(results: list[CaseResult]) -> float:
        return round(statistics.fmean(r.transcript.elapsed_s for r in results), 1)

    return {
        "agent": agent,
        "a": {"models": a, "check_pass_rate": checks_rate(results_a), "elapsed_s": elapsed(results_a),
              "usage": usage_summary(results_a), "errors": sum(bool(r.transcript.error) for r in results_a)},
        "b": {"models": b, "check_pass_rate": checks_rate(results_b), "elapsed_s": elapsed(results_b),
              "usage": usage_summary(results_b), "errors": sum(bool(r.transcript.error) for r in results_b)},
        "criteria": {
            name: {"a_wins": t["A"], "b_wins": t["B"], "ties": t["tie"],
                   "b_win_rate": round(t["B"] / max(t["A"] + t["B"], 1), 3)}
            for name, t in sorted(tallies.items())
        },
    }  # fmt: skip
