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
