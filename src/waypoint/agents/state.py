"""Graph state schemas.

A key without a reducer is last-write-wins, which silently loses data when two
parallel branches write it. Anything a fan-out touches gets a reducer.
"""

from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    recalled: list[str]
    """Long-term memories loaded for this turn."""
    step_count: int


class Source(TypedDict):
    """A page behind a finding. Taken from the provider's citation metadata or,
    failing that, its search results (``Finding.cited``); never from text the
    model wrote, so a URL here is one the search actually returned."""

    url: str
    title: str


class Finding(TypedDict):
    question: str
    answer: str
    sources: list[Source]
    cited: NotRequired[bool]
    """False when the searcher searched but cited nothing, and ``sources`` are
    the pages its searches returned rather than ones it cited. Absent (findings
    checkpointed before 2026-10-08) means cited."""
    confidence: float | None
    """Set by the critic. ``None`` until the finding has been reviewed."""
    issue: str
    """The critic's note on what is weak or missing; empty when nothing is."""
    round: int


def merge_findings(left: list[Finding], right: list[Finding]) -> list[Finding]:
    """Upsert by question.

    Parallel workers append distinct questions; the critic writes back the same
    questions with a confidence and an issue. ``operator.add`` would duplicate
    every reviewed finding, last-write-wins would drop all but one worker.
    Order is first-seen, so the briefing follows the plan.
    """
    merged: dict[str, Finding] = {f["question"]: f for f in left}
    for finding in right:
        merged[finding["question"]] = finding
    return list(merged.values())


class ResearchState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    topic: str
    plan: list[str]
    rationale: list[str]
    """Why the planner asked each question, index-aligned with ``plan``."""
    approved: bool
    findings: Annotated[list[Finding], merge_findings]
    round: int
    """Which investigation wave the critic is reviewing. Starts at 1."""
    follow_ups: list[str]
    """Questions the critic wants answered next. Empty means synthesize."""
    report: str
    metadata: dict[str, Any]


class WorkerInput(TypedDict):
    """The slice of state a single fan-out worker receives via Send().

    ``tenant_id`` is here for the node cache, not for the worker. A cache key is
    a hash of the node's input and nothing else, so without it two tenants
    asking the same question about the same topic would share an answer. It is
    safe to carry because a thread belongs to exactly one tenant for its whole
    life; user identity, which is not fixed per thread, stays in the context.
    """

    question: str
    topic: str
    tenant_id: str
    model: str
    """The searcher model, for the same reason: a per-run model choice must not
    be answered from another model's cached reply. The worker uses this name."""
    round: int
