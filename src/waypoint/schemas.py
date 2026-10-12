"""Wire models. Pydantic is the boundary; nothing untyped gets past it."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Message(Strict):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=100_000)


class CreateThread(Strict):
    agent: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=200)


class StartRun(Strict):
    """Either start fresh (``input``) or continue a paused graph (``resume``)."""

    input: dict[str, Any] | None = None
    message: str | None = Field(default=None, max_length=100_000)
    """Convenience: wrapped into the agent's declared input key."""
    resume: dict[str, Any] | str | None = None
    checkpoint_id: str | None = Field(default=None, max_length=200)
    """Fork from this checkpoint instead of the thread head (time travel)."""
    durability: Literal["sync", "async", "exit"] = "async"
    models: dict[str, str] | None = Field(default=None, max_length=16)
    """Role -> model for this run, e.g. ``{"critic": "claude-opus-..."}``;
    ``"default"`` covers every role not named. Models must be in MODELS_ALLOWED."""


class UpdateState(Strict):
    """Rewrite a checkpoint's values -- the write half of time travel."""

    values: dict[str, Any]
    as_node: str | None = Field(default=None, max_length=128)
    checkpoint_id: str | None = Field(default=None, max_length=200)


class CreateKey(Strict):
    tenant_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["agents:run", "threads:write"])
    label: str | None = Field(default=None, max_length=120)


class RememberFact(Strict):
    fact: str = Field(min_length=1, max_length=4_000)


class CreateCorpus(Strict):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[\w .:-]+$")
    """Unique within the tenant. Letters, digits, spaces and ``_ . : -``."""


SCOPE_RUN = "agents:run"
SCOPE_READ = "threads:read"
SCOPE_WRITE = "threads:write"
SCOPE_ADMIN = "keys:admin"
SCOPE_CORPORA = "corpora:write"
"""Create and delete corpora and upload documents. Searching a corpus needs only
the scopes of a research run, so a demo key without it can use public corpora
but add nothing."""


# -- responses ----------------------------------------------------------------------
# What the API returns, for the OpenAPI document and for FastAPI to check on the
# way out. Fields mirror web/src/api/types.ts, which mirrors these by hand.


class Out(BaseModel):
    """Responses keep unknown keys rather than drop them silently."""

    model_config = ConfigDict(extra="allow")


class Health(Out):
    status: Literal["ok"]


class Readiness(Out):
    status: Literal["ready", "degraded"]
    checks: dict[str, str]
    """Dependency -> ``ok``, ``off`` (not configured) or ``down``."""


class AgentInfo(Out):
    name: str
    title: str
    description: str
    input_key: str
    model_roles: list[str]
    supports_interrupts: bool
    example: dict[str, Any] | None = None


class AgentList(Out):
    agents: list[AgentInfo]


class Thread(Out):
    thread_id: str
    tenant_id: str
    user_id: str
    agent: str
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class ThreadList(Out):
    threads: list[Thread]


class PendingInterrupt(Out):
    id: str | None = None
    value: Any = None


class PendingTask(Out):
    id: str
    name: str
    error: str | None = None


class Snapshot(Out):
    """One checkpoint of a thread: its values, what runs next, what is waiting."""

    checkpoint_id: str | None = None
    parent_checkpoint_id: str | None = None
    created_at: str | None = None
    next: list[str]
    values: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    interrupts: list[PendingInterrupt] = Field(default_factory=list)
    tasks: list[PendingTask] = Field(default_factory=list)


class History(Out):
    checkpoints: list[Snapshot]


class StateUpdated(Out):
    checkpoint_id: str | None = None
    thread_id: str


class RoleUsage(Out):
    """One role's share of a run. Chat roles have ``calls`` and token counts;
    ``embed`` and ``rerank`` have ``tokens`` only."""

    model: str
    calls: int | None = None
    input_tokens: int | None = None
    """All input, cached parts included."""
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    searches: int | None = None
    tokens: int | None = None
    """Embedding (estimated) or rerank tokens."""
    cost_usd: float | None = None
    """``null`` when the model is not in the price table."""


class RunUsage(Out):
    """What a run cost, priced from ``src/waypoint/prices.toml``."""

    cost_usd: float | None
    """``null`` when any call was by a model the price table does not list."""
    input_tokens: int
    output_tokens: int
    searches: int
    roles: dict[str, RoleUsage] = Field(default_factory=dict)


class Run(Out):
    run_id: str
    thread_id: str
    agent: str
    tenant_id: str
    user_id: str
    status: str
    created_at: str
    error: str | None = None
    models: dict[str, str] = Field(default_factory=dict)
    """Role -> model this run used."""
    usage: RunUsage | None = None
    """Set when the run ends (and when it pauses at an interrupt)."""
    trace_id: str | None = None
    """The run's OpenTelemetry trace id, when tracing is on."""


class RunStarted(Run):
    events_url: str
    """``GET`` it for Server-Sent Events; resumable with ``Last-Event-ID``."""
    cancel_url: str


class RunResult(Out):
    """``?wait=true``: the run's terminal event, flattened. Other keys come from
    that event (``result``, ``error``, ``interrupts``)."""

    run_id: str
    status: str
    models: dict[str, str] = Field(default_factory=dict)
    """Role -> model this run used."""
    usage: RunUsage | None = None


class TraceStep(Out):
    """One step of a run's timeline. Which optional fields are set depends on ``kind``."""

    id: str
    parent: str | None
    """The step this one ran inside (a model call's node); ``null`` for a top-level node."""
    kind: Literal["node", "model", "tool", "retrieval"]
    name: str
    start_ms: float
    """From the start of the run."""
    duration_ms: float
    error: bool = False
    node: str | None = None
    role: str | None = None
    model: str | None = None
    tool: str | None = None
    mode: str | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    searches: int | None = None
    hits: int | None = None
    rerank_tokens: int | None = None


class RunTrace(Out):
    """``GET /runs/<id>/trace``: the run's timeline, or ``pending`` until it is stored."""

    status: Literal["ready", "pending"]
    run_id: str
    trace_id: str | None = None
    duration_ms: float | None = None
    cost_usd: float | None = None
    steps: list[TraceStep] = Field(default_factory=list)


class AgentSpend(Out):
    runs: int
    cost_usd: float


class TenantUsage(Out):
    """``GET /usage``: the caller's tenant's spend over a trailing window."""

    tenant_id: str
    days: int
    since: str
    """Start of the window (UTC, ISO 8601); it ends now."""
    runs: int
    """Runs started in the window that have ended."""
    cost_usd: float
    """Sum over runs with a recorded cost."""
    unpriced_runs: int
    """Ended runs with no cost recorded: an unpriced model, or from before metering."""
    input_tokens: int
    output_tokens: int
    searches: int
    by_agent: dict[str, AgentSpend] = Field(default_factory=dict)
    budget_usd_per_day: float | None = None
    """The tenant's daily budget; ``null`` when budgets are off or it is exempt."""
    balance_usd: float | None = None
    """What is left of it now. It refills continuously and can be below zero
    after runs that were already going when it ran out."""
    exempt: bool = False


class KeyCreated(Out):
    api_key: str
    """Shown once. Only its hash is stored."""
    tenant_id: str
    scopes: list[str]


class Memory(Out):
    key: str
    value: dict[str, Any]
    created_at: str


class MemoryList(Out):
    memories: list[Memory]


class MemoryCreated(Out):
    key: str
    fact: str


class CorpusOut(Out):
    corpus_id: str
    tenant_id: str
    name: str
    visibility: Literal["private", "public"]
    embed_model: str
    """Chunks are only comparable within one embedding model."""
    created_at: str
    documents: int = 0
    chunks: int = 0


class CorpusList(Out):
    corpora: list[CorpusOut]


class DocumentOut(Out):
    document_id: str
    corpus_id: str
    title: str
    source_uri: str | None = None
    media_type: str
    sha256: str
    bytes: int
    status: Literal["pending", "ready", "failed"]
    error: str | None = None
    created_at: str
    chunks: int = 0


class DocumentList(Out):
    documents: list[DocumentOut]


class UploadedDocument(DocumentOut):
    queued: bool
    """In this upload's ingest run. False for a duplicate already pending or ready."""


class UploadAccepted(Out):
    corpus_id: str
    documents: list[UploadedDocument]
    run_id: str | None = None
    """The ingest run; null when every file was a duplicate."""
    thread_id: str | None = None
    events_url: str | None = None
