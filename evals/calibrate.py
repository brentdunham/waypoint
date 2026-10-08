"""Measure the judge against hand-labelled examples and route each criterion.

``calibration/<agent>.jsonl`` holds outputs a person has scored, in the same
shape :func:`evals.judge.material_for` produces. For each criterion the
default judge grades every example; agreement is the share where judge and
label agree on pass (3-4) versus fail (1-2). Below ``EVALS__MIN_AGREEMENT`` the
escalation model is tried; if it is unset or also below, the criterion is
recorded as untrusted and scorecards show it ungraded rather than guessed.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.judge import (
    CALIBRATION,
    PASS,
    ROUTING,
    Judge,
    load_routing,
    load_rubric,
    rubric_hash,
)
from waypoint.settings import Settings


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    criterion: str
    label: int = Field(ge=1, le=4)
    why: str = ""
    """The labeller's reason, for the next person who disagrees with the label."""
    material: dict[str, Any]


def load_examples(agent: str, *, path: Path | None = None) -> list[Example]:
    file = path or CALIBRATION / f"{agent}.jsonl"
    return [
        Example.model_validate(json.loads(line))
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("//")
    ]


@dataclass(slots=True)
class CriterionCalibration:
    criterion: str
    model: str
    examples: int
    agreement: float
    mean_abs_error: float
    disagreements: list[str]


async def measure(
    judge: Judge, agent: str, examples: list[Example], model: str, criterion: str
) -> CriterionCalibration:
    rubric = load_rubric(agent)
    mine = [e for e in examples if e.criterion == criterion]
    grades = [await judge.grade(rubric[criterion], e.material, model=model) for e in mine]
    agree, errors, misses = 0, 0.0, []
    for example, grade in zip(mine, grades, strict=True):
        if grade.score is None:
            misses.append(f"{example.id}: {grade.reasoning}")
            errors += 3
            continue
        if (grade.score >= PASS) == (example.label >= PASS):
            agree += 1
        else:
            misses.append(f"{example.id}: label {example.label}, judge {grade.score}")
        errors += abs(grade.score - example.label)
    n = len(mine) or 1
    return CriterionCalibration(criterion, model, len(mine), agree / n, errors / n, misses)


async def calibrate(settings: Settings, agent: str) -> list[CriterionCalibration]:
    """Grade every labelled example, write ``routing.json``, return the measurements."""
    cfg = settings.evals
    judge = Judge(settings)
    examples = load_examples(agent)
    rubric = load_rubric(agent)
    unknown = {e.criterion for e in examples} - set(rubric)
    if unknown:
        raise KeyError(f"calibration/{agent}.jsonl names unknown criteria {sorted(unknown)}")

    results: list[CriterionCalibration] = []
    route: dict[str, str | None] = {}
    for criterion in rubric:
        if not any(e.criterion == criterion for e in examples):
            continue  # no labels: stays uncalibrated, graded by the default judge
        first = await measure(judge, agent, examples, cfg.judge_model, criterion)
        results.append(first)
        if first.agreement >= cfg.min_agreement:
            route[criterion] = cfg.judge_model
            continue
        if cfg.judge_escalation_model:
            second = await measure(judge, agent, examples, cfg.judge_escalation_model, criterion)
            results.append(second)
            if second.agreement >= cfg.min_agreement:
                route[criterion] = cfg.judge_escalation_model
                continue
        route[criterion] = None

    routing = load_routing()
    routing[agent] = {
        "judge": cfg.judge_model,
        "escalation": cfg.judge_escalation_model,
        "rubric": rubric_hash(agent),
        "min_agreement": cfg.min_agreement,
        "criteria": route,
    }
    ROUTING.write_text(json.dumps(routing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return results
