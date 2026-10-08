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
        default=2,
        ge=1,
        le=4,
        description="Investigation waves, counting the first. 1 disables critic follow-ups.",
    )
    max_follow_ups: int = Field(default=3, ge=0, le=5, description="New questions per round.")


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

    @field_validator("judge_escalation_model", mode="before")
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

    # --- caching -----------------------------------------------------------
    node_cache_ttl_s: int = Field(default=300, ge=0, description="0 disables node caching.")

    # --- nested ------------------------------------------------------------
    limits: LimitSettings = Field(default_factory=LimitSettings)
    stream: StreamSettings = Field(default_factory=StreamSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    evals: EvalSettings = Field(default_factory=EvalSettings)

    # --- tracing -------------------------------------------------------------
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
        "bootstrap_api_key",
        "embeddings_model",
        "database_url",
        "redis_url",
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
