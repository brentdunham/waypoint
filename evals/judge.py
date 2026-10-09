"""LLM as judge.

One call per criterion per output, never one call for "overall quality": a
single score hides which property regressed, and a model asked several things
at once lets one answer colour the others. Each call reasons before it scores,
on a 1-4 scale with written anchors; 3 or more is a pass.

The judge is ``EVALS__JUDGE_MODEL``, by default the cheapest current Claude
model, because it runs once per case, criterion and trial. Whether that model
can be trusted on a criterion is not assumed: ``evals calibrate`` measures it
against hand-labelled examples and routes any criterion it fails to
``EVALS__JUDGE_ESCALATION_MODEL``, or marks it untrusted (``routing.json``).
"""

import asyncio
import datetime as dt
import hashlib
import json
import random
import tomllib
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from evals.cases import ROOT, Case
from evals.driver import Transcript
from waypoint.agents.citations import number_sources
from waypoint.agents.models import ScriptedChatModel, build_model, structured
from waypoint.settings import Settings

RUBRICS = ROOT / "rubrics"
CALIBRATION = ROOT / "calibration"
ROUTING = CALIBRATION / "routing.json"
PASS = 3

type Target = Literal["plan", "briefing", "reply"]


class JudgeVerdict(BaseModel):
    reasoning: str = Field(description="Two to four sentences, written before the score.")
    score: int = Field(ge=1, le=4, description="1-4, using the anchors exactly as written.")


class PairVerdict(BaseModel):
    reasoning: str = Field(description="Two to four sentences comparing A and B.")
    winner: Literal["A", "B", "tie"]


@dataclass(frozen=True, slots=True)
class Criterion:
    name: str
    target: Target
    question: str
    anchors: dict[int, str]


def load_rubric(agent: str) -> dict[str, Criterion]:
    raw = tomllib.loads((RUBRICS / f"{agent}.toml").read_text(encoding="utf-8"))
    return {
        name: Criterion(
            name=name,
            target=spec["target"],
            question=spec["question"],
            anchors={n: spec[str(n)] for n in range(1, 5)},
        )
        for name, spec in raw["criteria"].items()
    }


# -- what the judge sees -----------------------------------------------------------


def _source(source: Any, numbers: dict[str, int], finding: Any) -> dict[str, Any]:
    """``n``, title, and what the answer quoted from it, if the API said.

    A source of an uncited finding (``cited: false``) was only retrieved: no
    claim in that answer is tied to it, and the judge is told so.
    """
    out: dict[str, Any] = {"n": numbers.get(source["url"]), "title": source["title"]}
    if quotes := source.get("quotes"):
        out["quotes"] = quotes
    if finding.get("cited") is False:
        out["retrieved_only"] = True
    return out


def material_for(target: Target, transcript: Transcript, case: Case) -> dict[str, Any]:
    """The judge's input for one target, in the same shape calibration files use."""
    values = transcript.values
    if target == "plan":
        return {
            "topic": values.get("topic", ""),
            "plan": values.get("plan", []),
            "rationale": values.get("rationale", []),
        }
    if target == "briefing":
        findings = values.get("findings") or []
        # The same global [n] numbering the writer was given, so the judge can
        # follow a citation straight to its source and that source's quotes.
        numbers = number_sources(findings)
        return {
            "topic": values.get("topic", ""),
            "expect": case.expect,
            "findings": [
                {
                    "question": f["question"],
                    "answer": f["answer"],
                    "sources": [_source(s, numbers, f) for s in f["sources"]],
                    "confidence": f["confidence"],
                    "reviewer_note": f["issue"],
                }
                for f in findings
            ],
            "briefing": values.get("report", ""),
        }
    return {
        "expect": case.expect,
        "recalled_memories": values.get("recalled", []),
        "conversation": [
            {k: m[k] for k in ("type", "content", "tool_calls", "name") if m.get(k)}
            for m in values.get("messages", [])
        ],
    }


SYSTEM = """You grade one criterion of an AI agent's output. Be strict and literal.
Judge only the criterion you are given, using its anchors exactly as written.
Do not reward length. You cannot browse; judge against the material provided.

Today is {today}. The material may describe events, publications, laws or
identifiers dated after your training data ends. A recent or unfamiliar date
is not evidence of fabrication; never lower a score because of one."""


def system_prompt() -> str:
    """The judge's system prompt with today's date filled in.

    Without the date, judges marked briefings on recent events down as
    "future-dated" or "fabricated" (first research baseline, 2026-10-07).
    """
    return SYSTEM.format(today=dt.date.today().isoformat())


GRADE = """CRITERION: {name}
{question}

SCALE
1: {a1}
2: {a2}
3: {a3}
4: {a4}

MATERIAL (JSON):
{material}

Reason in two to four sentences, then give the score."""

PAIR = """CRITERION: {name}
{question}

Two outputs, A and B, for the same input. Which better satisfies the
criterion? Answer "tie" only if you cannot separate them.

A (JSON):
{a}

B (JSON):
{b}"""


# -- routing -------------------------------------------------------------------------


def rubric_hash(agent: str) -> str:
    """Identifies what the judge is asked: the rubric file and the prompt templates.

    Calibration is only valid for the exact instructions it was measured
    with, so changing either makes every criterion uncalibrated again. The
    templates are hashed unformatted, so the date in SYSTEM does not change it.
    """
    blob = (RUBRICS / f"{agent}.toml").read_bytes() + (SYSTEM + GRADE).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def load_routing() -> dict[str, dict[str, Any]]:
    """agent -> {judge, rubric, criteria: {criterion: model or None}}."""
    if not ROUTING.exists():
        return {}
    data: dict[str, dict[str, Any]] = json.loads(ROUTING.read_text(encoding="utf-8"))
    return data


@dataclass(frozen=True, slots=True)
class Assignment:
    model: str | None
    status: Literal["calibrated", "escalated", "untrusted", "uncalibrated"]


def assign(settings: Settings, agent: str, criterion: str) -> Assignment:
    """Which model grades ``criterion``.

    Calibration is only valid for the judge model and rubric it was measured
    with; after either changes the criterion is ``uncalibrated`` again and the
    default judge grades it until ``evals calibrate`` runs.
    """
    entry = load_routing().get(agent, {})
    criteria: dict[str, str | None] = entry.get("criteria", {})
    current = entry.get("judge") == settings.evals.judge_model and entry.get(
        "rubric"
    ) == rubric_hash(agent)
    if not current or criterion not in criteria:
        return Assignment(settings.evals.judge_model, "uncalibrated")
    model = criteria[criterion]
    if model is None:
        return Assignment(None, "untrusted")
    status: Literal["calibrated", "escalated"] = (
        "calibrated" if model == settings.evals.judge_model else "escalated"
    )
    return Assignment(model, status)


# -- grading -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grade:
    criterion: str
    score: int | None
    """None when the criterion is untrusted or the judge failed."""
    reasoning: str
    judge: str | None
    status: str

    @property
    def passed(self) -> bool | None:
        return None if self.score is None else self.score >= PASS


class Judge:
    def __init__(self, settings: Settings, *, concurrency: int = 4) -> None:
        self._settings = settings
        self._models: dict[str, Any] = {}
        self._gate = asyncio.Semaphore(concurrency)

    def _model(self, name: str, schema: type[BaseModel]) -> Any:
        key = f"{name}:{schema.__name__}"
        if key not in self._models:
            # A distinct name from any agent role; never streamed.
            model = build_model(
                self._settings,
                model=name,
                disable_streaming=True,
                temperature=self._settings.evals.judge_temperature,
            )
            self._models[key] = structured(model, schema, self._settings)
        return self._models[key]

    async def grade(self, criterion: Criterion, material: dict[str, Any], *, model: str) -> Grade:
        prompt = GRADE.format(
            name=criterion.name,
            question=criterion.question,
            a1=criterion.anchors[1],
            a2=criterion.anchors[2],
            a3=criterion.anchors[3],
            a4=criterion.anchors[4],
            material=json.dumps(material, indent=1, ensure_ascii=False),
        )
        try:
            async with self._gate:
                verdict = await self._model(model, JudgeVerdict).ainvoke(
                    [SystemMessage(system_prompt()), HumanMessage(prompt)]
                )
        except Exception as exc:
            return Grade(criterion.name, None, f"judge failed: {exc}", model, "error")
        assert isinstance(verdict, JudgeVerdict)
        return Grade(criterion.name, verdict.score, verdict.reasoning, model, "ok")

    async def grade_transcript(
        self, agent: str, transcript: Transcript, case: Case, rubric: dict[str, Criterion]
    ) -> list[Grade]:
        if not case.judge:
            return []
        names = case.criteria or list(rubric)
        unknown = [n for n in names if n not in rubric]
        if unknown:
            raise KeyError(f"{case.id}: unknown criteria {unknown}")
        if transcript.error:
            return [Grade(n, None, "run failed", None, "skipped") for n in names]

        async def one(name: str) -> Grade:
            assignment = assign(self._settings, agent, name)
            if assignment.model is None:
                return Grade(name, None, "judge untrusted on this criterion", None, "untrusted")
            criterion = rubric[name]
            grade = await self.grade(
                criterion, material_for(criterion.target, transcript, case), model=assignment.model
            )
            if grade.status == "ok":
                return Grade(name, grade.score, grade.reasoning, grade.judge, assignment.status)
            return grade

        return list(await asyncio.gather(*(one(n) for n in names)))

    async def prefer(
        self,
        criterion: Criterion,
        a: dict[str, Any],
        b: dict[str, Any],
        *,
        model: str,
        rng: random.Random,
    ) -> Literal["A", "B", "tie"] | None:
        """Which of two outputs wins. Order is shuffled to cancel position bias."""
        swapped = rng.random() < 0.5
        first, second = (b, a) if swapped else (a, b)
        prompt = PAIR.format(
            name=criterion.name,
            question=criterion.question,
            a=json.dumps(first, indent=1, ensure_ascii=False),
            b=json.dumps(second, indent=1, ensure_ascii=False),
        )
        try:
            async with self._gate:
                verdict = await self._model(model, PairVerdict).ainvoke(
                    [SystemMessage(system_prompt()), HumanMessage(prompt)]
                )
        except Exception:
            return None
        assert isinstance(verdict, PairVerdict)
        if verdict.winner == "tie" or not swapped:
            return verdict.winner
        return "B" if verdict.winner == "A" else "A"


# Offline answers, so the judge path runs under FAKE_MODEL in the test suite.
ScriptedChatModel.structured_scripts.update(
    {
        "JudgeVerdict": lambda _text: {"reasoning": "Scripted verdict.", "score": 3},
        "PairVerdict": lambda _text: {"reasoning": "Scripted preference.", "winner": "tie"},
    }
)
