"""Graph state schemas.

A key without a reducer is last-write-wins, which silently loses data when two
parallel branches write it. Anything a fan-out touches gets a reducer.
"""

from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ChatState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    recalled: list[str]
    """Long-term memories loaded for this turn."""
    step_count: int


class Source(TypedDict):
    """A page or passage behind a finding. Taken from the provider's citation
    metadata or, failing that, its search results (``Finding.cited``); never
    from text the model wrote, so a URL here is one the search actually
    returned."""

    url: str
    """A web URL, or ``doc://<document_id>#<chunk ordinal>`` for a passage from
    the tenant's documents. Unique per source either way: it keys the [n]
    numbering."""
    title: str
    """The page title, or the passage's heading path (``DEPLOY.md`` then ``4. Caddy``)."""
    kind: NotRequired[Literal["web", "document"]]
    """Absent means web: only document sources are marked, so findings and
    prompts of web-only runs are byte-identical to before documents existed."""
    quotes: NotRequired[list[str]]
    """What the API says the answer drew from this page: the ``cited_text`` of
    each citation to it, deduplicated and capped (``citations.MAX_QUOTES``).
    Absent for retrieved-only sources and for findings made before 2026-10-09."""


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
    model: NotRequired[str]
    """The searcher model that wrote ``answer``. Absent before 2026-10-09."""
    retried: NotRequired[bool]
    """True when the first answer attached no citations and the searcher was
    asked again (``RECITE_RULE``); ``answer`` and ``sources`` are the second
    reply's. Absent otherwise."""


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
    corpus_id: str | None
    """Run input: search this corpus as well as the web. The approval step can
    switch it off (set to None)."""
    corpus: dict[str, Any] | None
    """``{"corpus_id", "name"}`` of the corpus, resolved for the tenant by the
    planner, for the approval card and the briefing's metadata."""


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
    corpus_id: NotRequired[str | None]
    """The corpus to retrieve from, if any. In the input, so in the cache key:
    the same question with and without documents is a different answer."""


class WorkspaceState(TypedDict, total=False):
    """The workspace agent's state: chat's, plus the thread's documents and the
    last reply's sources (docs/design/tools.md)."""

    messages: Annotated[list[AnyMessage], add_messages]
    recalled: list[str]
    """Long-term memories loaded for this turn."""
    step_count: int
    corpus_ids: list[str]
    """Run input: the corpora ``search_documents`` searches, the tenant's own
    and public ones. Kept in the thread's checkpoint, so they hold for later
    turns until a run sends another list (``[]`` stops searching documents)."""
    corpus_id: str | None
    """The older single-corpus input, still accepted: ``select_corpus`` turns
    it into ``corpus_ids`` and clears it."""
    corpora: list[dict[str, Any]]
    """``{"corpus_id", "name"}`` per selected corpus, resolved for the tenant
    when the selection changes."""
    findings: list[Finding]
    """The last reply's evidence: one finding per ``search_documents`` call it
    drew on (question = the query, answer = the passages, sources = the
    passages with what the reply quoted). Replaced every turn. The verifier and
    the evals read it, as they read research findings."""
    report: str
    """The last reply as shown: the answer with ``[n]`` citations, then the
    ``Sources`` list, the same shape as a research briefing."""
    approvals: dict[str, bool]
    """Tool call id -> whether the person allowed it, for the last reply's
    calls that needed approval (``approve_tools``). Replaced every time."""
    metadata: dict[str, Any]
