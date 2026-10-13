"""Configuration. Every knob the service has, in one place, read from the environment.

Nested settings use a double underscore: ``LIMITS__RPM=120`` sets ``settings.limits.rpm``.
See docs/CONFIGURATION.md for the full table.
"""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

type Env = Literal["development", "production", "test"]

# Values that appear in .env.example and the docs. A credential anyone can read
# in the repository is not a credential, so these are never seeded.
PLACEHOLDER_KEYS = frozenset({"ak_dev000000.change-this-local-key"})


class LimitSettings(BaseModel):
    """Per-tenant guardrails. All enforced in Redis, so they hold across workers."""

    rpm: int = Field(default=60, ge=1, description="Requests per minute per tenant.")
    concurrent_runs: int = Field(default=4, ge=1, description="Simultaneous graph runs per tenant.")
    max_input_chars: int = Field(default=100_000, ge=1)
    recursion_limit: int = Field(default=40, ge=1, description="LangGraph super-steps per run.")
    run_timeout_s: float = Field(default=300.0, gt=0)


class BudgetSettings(BaseModel):
    """Spending caps in US dollars per day (observability design, O2 to O4, D1).

    Each is a bucket in Redis that holds a day's budget and refills
    continuously at budget / 86 400 per second. A run is admitted while the
    tenant's balance and the server's are above zero; each model call's cost
    is charged when it ends, so a balance can go below zero by what runs
    already in flight spend. Unset means off in development; production
    refuses to boot without a decision (set both numbers, or ENABLED=false).
    """

    enabled: bool | None = Field(
        default=None,
        description=(
            "Unset: on when both daily budgets are set. false: off, also in production. "
            "true: on, and both budgets are required."
        ),
    )
    tenant_usd_per_day: float | None = Field(
        default=None,
        gt=0,
        description="Each tenant's daily budget, unless BUDGET__TENANTS says otherwise.",
    )
    global_usd_per_day: float | None = Field(
        default=None, gt=0, description="All tenants together, exempt ones aside."
    )
    tenants: dict[str, float | None] = Field(
        default_factory=dict,
        description=(
            'Per-tenant overrides, as JSON: {"acme": 5.0} raises a tenant\'s budget, '
            '{"brent": null} exempts a tenant from both its own and the global budget.'
        ),
    )

    @field_validator("tenant_usd_per_day", "global_usd_per_day", "enabled", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("tenants", mode="before")
    @classmethod
    def _blank_is_empty(cls, v: object) -> object:
        if v is None or (isinstance(v, str) and not v.strip()):
            return {}
        return v

    @field_validator("tenants")
    @classmethod
    def _positive(cls, v: dict[str, float | None]) -> dict[str, float | None]:
        for tenant, usd in v.items():
            if usd is not None and usd <= 0:
                raise ValueError(
                    f"BUDGET__TENANTS[{tenant!r}]={usd}: a budget must be above zero; "
                    "use null to exempt the tenant."
                )
        return v

    @property
    def active(self) -> bool:
        if self.enabled is False:
            return False
        return self.tenant_usd_per_day is not None and self.global_usd_per_day is not None

    def tenant_budget(self, tenant_id: str) -> float | None:
        """The tenant's daily budget; ``None`` when it is exempt or budgets are off."""
        if not self.active:
            return None
        if tenant_id in self.tenants:
            return self.tenants[tenant_id]
        return self.tenant_usd_per_day

    def exempt(self, tenant_id: str) -> bool:
        return self.active and tenant_id in self.tenants and self.tenants[tenant_id] is None


class TracingSettings(BaseModel):
    """OpenTelemetry tracing. On when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set."""

    content: bool = Field(
        default=False,
        description=(
            "Put prompts, completions and tool inputs on spans. For local debugging only: "
            "they are tenant data, and a trace backend is one more place to secure."
        ),
    )
    panel: bool = Field(
        default=False,
        description=(
            "Serve GET /runs/<id>/trace, the run timeline the UI shows under a finished run "
            "(observability design, D7). Needs query_url."
        ),
    )
    query_url: str | None = Field(
        default=None,
        description="Jaeger's query service, e.g. http://jaeger:16686, read for the run timeline.",
    )

    @field_validator("query_url", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _panel_needs_a_backend(self) -> TracingSettings:
        if self.panel and not self.query_url:
            raise ValueError(
                "TRACING__PANEL=true needs TRACING__QUERY_URL (Jaeger's query service) to read "
                "traces from."
            )
        return self


class StreamSettings(BaseModel):
    """Redis Stream backing the run event bus."""

    maxlen: int = Field(default=2_000, ge=10, description="Events retained per run.")
    ttl_s: int = Field(default=3_600, ge=60, description="How long a finished run stays readable.")
    heartbeat_s: float = Field(default=15.0, gt=0, description="SSE keep-alive interval.")
    block_ms: int = Field(default=5_000, ge=100, description="XREAD block timeout.")


class ResearchSettings(BaseModel):
    """The research agent's search and review budget. Bounds cost per run."""

    web_search: bool = Field(
        default=True,
        description=(
            "Answer questions with Anthropic's server-side web search and cite what it "
            "returns. Must be enabled for the organization in the Claude Console. Off -> "
            "answers come from the model alone and carry no sources."
        ),
    )
    max_searches: int = Field(
        default=2,
        ge=1,
        le=10,
        description=(
            "Web searches per worker call (the tool's max_uses). Each search's results "
            "are input again on every later search in the call, so the third search "
            "more than doubled a call's input in the 2026-10-08 evals (21k -> 48k tokens)."
        ),
    )
    max_rounds: int = Field(
        default=1,
        ge=1,
        le=4,
        description=(
            "Investigation waves, counting the first. 1 disables critic follow-ups: in the "
            "2026-10-12 evals it cut cost per run by 38%, with covers 0.11 lower "
            "(docs/case-studies/cost.md)."
        ),
    )
    max_follow_ups: int = Field(default=3, ge=0, le=5, description="New questions per round.")
    verify_citations: bool = Field(
        default=True,
        description=(
            "Check every briefing sentence against the quotes of the sources it cites, fix "
            "wrong or missing citations, and have the writer revise unsupported sentences."
        ),
    )


class RagSettings(BaseModel):
    """Retrieval over tenant documents (docs/design/rag.md).

    Separate from ``EMBEDDINGS_MODEL``, which indexes long-term memory: corpora
    and memories have different embedders, dimensions and lifecycles, and
    changing one must not force re-embedding the other.
    """

    embeddings_model: str | None = Field(
        default=None,
        description=(
            "'provider:model' for corpus chunks, e.g. 'voyageai:voyage-4'. Unset -> RAG is "
            "off: the corpora API answers 503 and no ingest run can start."
        ),
    )
    embeddings_dims: int = Field(
        default=1024,
        ge=1,
        le=2000,
        description=(
            "Width of chunks.embedding. pgvector's HNSW index stops at 2000 dims for "
            "`vector`. Changing it after the table exists means re-embedding every corpus."
        ),
    )
    chunk_tokens: int = Field(
        default=400,
        ge=50,
        le=4000,
        description="Target chunk size, estimated as characters / 4.",
    )
    max_upload_mb: float = Field(
        default=5.0,
        gt=0,
        le=8,
        description="Per file. The whole request is also capped at 8 MB by the middleware.",
    )
    top_k: int = Field(
        default=8,
        ge=1,
        le=50,
        description=(
            "Chunks handed to a research worker per question. Each is input tokens on "
            "every web search in that worker call, so more is not free (design: start "
            "at 8, measure 5/8/12/20)."
        ),
    )
    mode: Literal["vector", "keyword", "hybrid"] = Field(
        default="vector",
        description=(
            "hybrid fuses the vector and full-text rankings with RRF. vector by default: "
            "on the first retrieval scorecard (2026-10-10) hybrid scored below vector "
            "alone on recall@8 (0.76 vs 0.90) and MRR (0.60 vs 0.73)."
        ),
    )
    candidates: int = Field(
        default=50,
        ge=1,
        le=500,
        description=(
            "How many chunks each method ranks before fusion (hybrid), and how many the "
            "reranker reorders (rerank)."
        ),
    )
    rrf_k: int = Field(
        default=60,
        ge=1,
        description="RRF constant: a chunk at rank r scores 1/(rrf_k + r) per method.",
    )
    contextual: bool = Field(
        default=False,
        description=(
            "Contextual retrieval: before embedding, context_model writes a short preamble "
            "situating each chunk in its document. One call per chunk, with the document "
            "cached. The preamble is embedded and full-text indexed with the chunk, never "
            "shown to the research worker. Applies to documents ingested afterwards."
        ),
    )
    context_model: str = Field(
        default="claude-haiku-5-5",
        description="Writes the contextual preambles when `contextual` is on.",
    )
    rerank: bool = Field(
        default=True,
        description=(
            "Rerank the first stage's top `candidates` chunks with a Voyage reranker "
            "before taking top_k. One rerank call per query; needs VOYAGE_API_KEY. On "
            "by default: in the phase 4 retrieval eval (2026-10-10) it raised vector "
            "MRR from 0.80 to 0.90 and recall@5 from 0.88 to 0.96, for about $0.0007 "
            "and 0.2 s per query."
        ),
    )
    rerank_model: str = Field(
        default="rerank-3",
        description="Voyage reranker used when `rerank` is on.",
    )
    seed_dir: str | None = Field(
        default=None,
        description=(
            "A directory of documents to keep indexed as a public corpus, readable by "
            "every tenant. Reconciled in the background at every boot (Postgres only): "
            "new and changed files are indexed, removed ones deleted. Titles are paths "
            "relative to the working directory, e.g. docs/DEPLOY.md. Unset -> no seeding."
        ),
    )
    seed_corpus: str = Field(
        default="waypoint-docs",
        description="Name of the public corpus `seed_dir` is indexed into.",
    )

    @field_validator("embeddings_model", "seed_dir", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @property
    def enabled(self) -> bool:
        return self.embeddings_model is not None


class EvalSettings(BaseModel):
    """The eval harness (``python -m evals``). Not read by the service."""

    judge_model: str = Field(
        default="claude-haiku-4-5",
        description=(
            "Grades each rubric criterion. The cheapest current Claude model: the judge "
            "runs once per case, criterion and trial, so its price multiplies."
        ),
    )
    judge_escalation_model: str | None = Field(
        default=None,
        description=(
            "Used for criteria where `evals calibrate` found the judge below "
            "min_agreement. Unset -> those criteria are reported as untrusted."
        ),
    )
    judge_temperature: float | None = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "0 makes the judge's scores repeatable: at the API default, Haiku scored the "
            "same briefing 3,2,2,2,2 in five calls. Unset -> omit it (for models that "
            "reject the parameter)."
        ),
    )
    min_agreement: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Judge agreement with the hand-labelled set needed to trust a criterion.",
    )
    database_url: PostgresDsn | None = Field(
        default=None,
        description=(
            "Postgres for the retrieval eval's index, so it measures the same SQL the "
            "service runs. A database of its own, never the service's."
        ),
    )

    @field_validator("judge_escalation_model", "database_url", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- app -------------------------------------------------------------
    env: Env = "development"
    debug: bool = False
    secret_key: str = "dev-only-change-me"
    json_logs: bool = False
    log_level: str = "INFO"
    # NoDecode is load-bearing: without it pydantic-settings tries to JSON-decode
    # any complex-typed env var *before* validators run, so CORS_ORIGINS=http://x
    # fails to parse instead of reaching the splitter below.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # --- auth ------------------------------------------------------------
    auth_enabled: bool = True
    bootstrap_api_key: str | None = Field(
        default=None,
        description="Seeded on startup as tenant 'bootstrap'. Dev convenience; unset in prod.",
    )

    @property
    def bootstrap_is_placeholder(self) -> bool:
        return (self.bootstrap_api_key or "") in PLACEHOLDER_KEYS

    # --- model -----------------------------------------------------------
    anthropic_api_key: str | None = None
    model: str = "claude-sonnet-4-5"
    model_temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Omitted from the request entirely when unset, which is the default. "
            "Newer Anthropic models reject `temperature` with a 400."
        ),
    )
    max_tokens: int = 4096
    structured_output: Literal["json_schema", "function_calling"] = Field(
        default="json_schema",
        description=(
            "How planner, critic and eval judge get typed output. json_schema uses "
            "Anthropic structured outputs (constrained decoding: the reply always "
            "parses). function_calling is a forced tool call, for models without it."
        ),
    )
    models: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Per-agent and per-role models: MODELS__RESEARCH__DEFAULT, "
            "MODELS__RESEARCH__CRITIC. Unset roles fall back to the agent default, "
            "then MODEL. Agent and role names are checked against the registry at boot."
        ),
    )
    models_allowed: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Models a run may ask for with `models` in the run request. Empty -> every "
            "model already named by MODEL and MODELS__*."
        ),
    )
    fake_model: bool = Field(
        default=False,
        description="Replace the LLM with a deterministic stub. Used by the test suite.",
    )

    # --- infrastructure ----------------------------------------------------
    database_url: PostgresDsn | None = None
    redis_url: RedisDsn | None = None
    redis_socket_timeout_s: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Read timeout for every Redis command, including the blocking XREAD that "
            "follows a run. Must exceed STREAM__BLOCK_MS by at least a second."
        ),
    )
    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- long-term memory --------------------------------------------------
    embeddings_model: str | None = Field(
        default=None,
        description="e.g. 'openai:text-embedding-3-small'. Unset -> store without vector search.",
    )
    embeddings_dims: int = Field(default=1536, ge=1)
    openai_api_key: str | None = Field(
        default=None,
        description=(
            "Needed only when EMBEDDINGS_MODEL starts with 'openai:'. Read here and passed "
            "explicitly, because pydantic-settings does not export .env into os.environ."
        ),
    )
    voyage_api_key: str | None = Field(
        default=None,
        description="Needed when an embeddings model starts with 'voyageai:'.",
    )

    # --- caching -----------------------------------------------------------
    node_cache_ttl_s: int = Field(default=300, ge=0, description="0 disables node caching.")

    # --- nested ------------------------------------------------------------
    limits: LimitSettings = Field(default_factory=LimitSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    stream: StreamSettings = Field(default_factory=StreamSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    evals: EvalSettings = Field(default_factory=EvalSettings)
    rag: RagSettings = Field(default_factory=RagSettings)

    # --- tracing -------------------------------------------------------------
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None,
        description=(
            "OTLP/HTTP base URL, e.g. http://localhost:4318; spans go to <url>/v1/traces. "
            "Unset -> no tracing. OTEL_EXPORTER_OTLP_HEADERS etc. are read by the SDK."
        ),
    )
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    langsmith_tracing: bool = False
    langsmith_project: str = "waypoint"

    @field_validator("cors_origins", "models_allowed", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("models", mode="before")
    @classmethod
    def _normalise_models(cls, v: object) -> object:
        """Lower-case agent and role names; a blank model name means unset."""
        if not isinstance(v, dict):
            return v
        out: dict[str, dict[str, str]] = {}
        for agent, roles in v.items():
            if not isinstance(roles, dict):
                return v  # let pydantic report the shape error
            kept = {
                str(role).strip().lower(): str(name).strip()
                for role, name in roles.items()
                if str(name).strip()
            }
            if kept:
                out[str(agent).strip().lower()] = kept
        return out

    @field_validator(
        "anthropic_api_key",
        "openai_api_key",
        "voyage_api_key",
        "bootstrap_api_key",
        "embeddings_model",
        "database_url",
        "redis_url",
        "otel_exporter_otlp_endpoint",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """``FOO: ${FOO:-}`` in compose sends an empty string, not nothing.

        Left alone, that makes ANTHROPIC_API_KEY="" (which fails at the first
        model call, far from the cause) and DATABASE_URL="" (which fails DSN
        validation). An empty value means unset.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _block_fits_in_socket_timeout(self) -> Settings:
        """A blocking XREAD that outlives the socket timeout fails instead of
        returning empty, which kills every SSE stream on a quiet run."""
        block_s = self.stream.block_ms / 1000
        if block_s > self.redis_socket_timeout_s - 1:
            raise ValueError(
                f"STREAM__BLOCK_MS={self.stream.block_ms} must be at least 1s shorter than "
                f"REDIS_SOCKET_TIMEOUT_S={self.redis_socket_timeout_s:g}, or idle event "
                "streams time out. Lower the block or raise the timeout."
            )
        return self

    @model_validator(mode="after")
    def _budget_is_decided(self) -> Settings:
        """A public deployment must not spend without a cap by accident."""
        b = self.budget
        missing = [
            name
            for name, value in (
                ("BUDGET__TENANT_USD_PER_DAY", b.tenant_usd_per_day),
                ("BUDGET__GLOBAL_USD_PER_DAY", b.global_usd_per_day),
            )
            if value is None
        ]
        if b.enabled is False or not missing:
            return self
        if b.enabled is True or len(missing) == 1:
            raise ValueError(f"Budgets need both daily amounts; {' and '.join(missing)} unset.")
        if self.is_production:
            raise ValueError(
                "ENV=production needs spending caps: set BUDGET__TENANT_USD_PER_DAY and "
                "BUDGET__GLOBAL_USD_PER_DAY, or BUDGET__ENABLED=false to run without them."
            )
        return self

    @property
    def allowed_models(self) -> frozenset[str]:
        """What a run request may choose from. See ``models_allowed``."""
        if self.models_allowed:
            return frozenset(self.models_allowed)
        named = {self.model}
        for roles in self.models.values():
            named.update(roles.values())
        return frozenset(named)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def pg_dsn(self) -> str | None:
        return str(self.database_url) if self.database_url else None

    @property
    def redis_dsn(self) -> str | None:
        return str(self.redis_url) if self.redis_url else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
