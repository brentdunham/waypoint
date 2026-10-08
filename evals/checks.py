"""Programmatic checks: deterministic, free, and run before any judge.

A case lists the checks that apply to it (``"checks": {"tool_called": "now"}``).
``completed`` applies to every case. A check returns ``passed=None`` when it
does not apply under the current settings (e.g. sources without web search),
and that result is left out of pass rates rather than counted either way.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from evals.driver import Transcript
from waypoint.agents.graphs.research import MAX_QUESTIONS
from waypoint.settings import Settings

CITATION = re.compile(r"\[(\d+)\]")
RANGE = re.compile(r"\[\d+\s*[-\u2013]\s*\d+\]")
SOURCE_LINE = re.compile(r"^\[(\d+)\] ", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool | None
    detail: str = ""


type Check = Callable[[Transcript, Any, Settings], CheckResult]
CHECKS: dict[str, Check] = {}


def check(name: str) -> Callable[[Check], Check]:
    def register(fn: Check) -> Check:
        CHECKS[name] = fn
        return fn

    return register


def run_checks(
    transcript: Transcript, wanted: dict[str, Any], settings: Settings
) -> list[CheckResult]:
    results = [CHECKS["completed"](transcript, True, settings)]
    if transcript.error:
        # Nothing else is meaningful; count them as failed, not skipped.
        return results + [CheckResult(n, False, "run failed") for n in wanted]
    for name, arg in wanted.items():
        if name not in CHECKS:
            raise KeyError(f"Unknown check {name!r}. Known: {', '.join(sorted(CHECKS))}.")
        results.append(CHECKS[name](transcript, arg, settings))
    return results


def _ok(name: str, problems: list[str]) -> CheckResult:
    return CheckResult(name, not problems, "; ".join(problems[:5]))


@check("completed")
def _completed(t: Transcript, _: Any, __: Settings) -> CheckResult:
    if t.error:
        return CheckResult("completed", False, t.error)
    return CheckResult("completed", bool(t.reply()), "" if t.reply() else "no reply")


# -- research -------------------------------------------------------------------


def _briefing(t: Transcript) -> tuple[str, set[int]]:
    """The body the writer produced, and the source numbers listed under it."""
    report = str(t.values.get("report", ""))
    body, _, listing = report.partition("\n\nSources\n")
    return body, {int(n) for n in SOURCE_LINE.findall(listing)}


@check("citations_resolve")
def _citations_resolve(t: Transcript, _: Any, __: Settings) -> CheckResult:
    body, listed = _briefing(t)
    problems = [f"range {m}" for m in RANGE.findall(body)]
    problems += sorted(
        {f"[{n}] not listed" for n in map(int, CITATION.findall(body)) if n not in listed}
    )
    return _ok("citations_resolve", problems)


@check("sources_cited")
def _sources_cited(t: Transcript, arg: Any, s: Settings) -> CheckResult:
    """At least a share of the listed sources is cited in the body (default 0.3).

    Not all of them: research lists 30-50 sources and a good briefing cited
    about half in the first live runs. A writer that stops citing drops to
    zero, which is the regression this exists to catch.
    """
    if not s.research.web_search:
        return CheckResult("sources_cited", None, "web search off")
    body, listed = _briefing(t)
    if not listed:
        return CheckResult("sources_cited", False, "no sources listed")
    share = 0.3 if arg is True else float(arg)
    cited = set(map(int, CITATION.findall(body))) & listed
    ratio = len(cited) / len(listed)
    detail = f"{len(cited)}/{len(listed)} sources cited"
    return CheckResult("sources_cited", ratio >= share, detail)


@check("findings_sourced")
def _findings_sourced(t: Transcript, _: Any, s: Settings) -> CheckResult:
    if not s.research.web_search:
        return CheckResult("findings_sourced", None, "web search off")
    findings = t.values.get("findings") or []
    if not findings:
        return CheckResult("findings_sourced", False, "no findings")
    return _ok(
        "findings_sourced", [f"no source: {f['question']}" for f in findings if not f["sources"]]
    )


@check("findings_cited")
def _findings_cited(t: Transcript, _: Any, s: Settings) -> CheckResult:
    """Every finding's sources came from citations, not the retrieved-results
    fallback. Stricter than ``findings_sourced``, which the fallback satisfies."""
    if not s.research.web_search:
        return CheckResult("findings_cited", None, "web search off")
    findings = t.values.get("findings") or []
    if not findings:
        return CheckResult("findings_cited", False, "no findings")
    return _ok(
        "findings_cited",
        [f"not cited: {f['question']}" for f in findings if not f.get("cited", bool(f["sources"]))],
    )


@check("plan_bounds")
def _plan_bounds(t: Transcript, _: Any, __: Settings) -> CheckResult:
    n = len(t.values.get("plan") or [])
    return _ok("plan_bounds", [] if 1 <= n <= MAX_QUESTIONS else [f"{n} questions"])


@check("rounds_bounded")
def _rounds_bounded(t: Transcript, _: Any, s: Settings) -> CheckResult:
    rounds = max((f["round"] for f in t.values.get("findings") or []), default=0)
    limit = s.research.max_rounds
    return _ok("rounds_bounded", [] if rounds <= limit else [f"{rounds} rounds > {limit}"])


@check("distinct_questions")
def _distinct_questions(t: Transcript, _: Any, __: Settings) -> CheckResult:
    seen: set[str] = set()
    dupes: list[str] = []
    for finding in t.values.get("findings") or []:
        key = " ".join(finding["question"].lower().split()).rstrip("?")
        if key in seen:
            dupes.append(finding["question"])
        seen.add(key)
    return _ok("distinct_questions", [f"repeated: {q}" for q in dupes])


@check("declined_without_search")
def _declined_without_search(t: Transcript, _: Any, __: Settings) -> CheckResult:
    problems = []
    if t.values.get("approved") is not False:
        problems.append("plan was not declined")
    if t.values.get("findings"):
        problems.append("findings exist")
    if any(c.role == "searcher" for c in t.calls):
        problems.append("searcher was called")
    return _ok("declined_without_search", problems)


# -- chat -------------------------------------------------------------------------


def _names(arg: Any) -> list[str]:
    return [arg] if isinstance(arg, str) else list(arg)


@check("tool_called")
def _tool_called(t: Transcript, arg: Any, __: Settings) -> CheckResult:
    called = {c["name"] for c in t.tool_calls()}
    return _ok("tool_called", [f"{n} not called" for n in _names(arg) if n not in called])


@check("tool_not_called")
def _tool_not_called(t: Transcript, arg: Any, __: Settings) -> CheckResult:
    called = {c["name"] for c in t.tool_calls()}
    return _ok("tool_not_called", [f"{n} was called" for n in _names(arg) if n in called])


@check("no_tools")
def _no_tools(t: Transcript, _: Any, __: Settings) -> CheckResult:
    called = sorted({c["name"] for c in t.tool_calls()})
    return _ok("no_tools", [f"called {', '.join(called)}"] if called else [])


@check("remembered")
def _remembered(t: Transcript, arg: Any, __: Settings) -> CheckResult:
    facts = [
        str(c["args"].get("fact", "")).lower() for c in t.tool_calls() if c["name"] == "remember"
    ]
    missing = [w for w in _names(arg) if not any(w.lower() in f for f in facts)]
    return _ok("remembered", [f"no remember() with {w!r}" for w in missing])


@check("recalled")
def _recalled(t: Transcript, arg: Any, __: Settings) -> CheckResult:
    recalled = " ".join(t.values.get("recalled") or []).lower()
    return _ok(
        "recalled", [f"{w!r} not recalled" for w in _names(arg) if w.lower() not in recalled]
    )


@check("reply_mentions")
def _reply_mentions(t: Transcript, arg: Any, __: Settings) -> CheckResult:
    reply = t.reply().lower()
    return _ok("reply_mentions", [f"missing {w!r}" for w in _names(arg) if w.lower() not in reply])


@check("reply_excludes")
def _reply_excludes(t: Transcript, arg: Any, __: Settings) -> CheckResult:
    reply = t.reply().lower()
    return _ok("reply_excludes", [f"contains {w!r}" for w in _names(arg) if w.lower() in reply])
