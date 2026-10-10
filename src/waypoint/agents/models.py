"""Model construction, including the deterministic stand-in used by tests."""

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast, override

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from waypoint.settings import Settings

if TYPE_CHECKING:
    from waypoint.agents.context import AgentContext

log = logging.getLogger(__name__)


class ScriptedChatModel(BaseChatModel):
    """A model that answers from a script instead of the network.

    Deterministic, free, and offline -- which is what makes the graph itself
    testable. Set ``FAKE_MODEL=true``. If the incoming text contains
    ``[[tool:name(json)]]`` it emits that tool call instead of prose.
    """

    # A pydantic field on BaseChatModel, not a mutable class attribute.
    responses: list[str] = []  # noqa: RUF012
    _cursor: int = 0

    structured_scripts: ClassVar[dict[str, Callable[[str], dict[str, Any]]]] = {}
    """Schema name -> function from the prompt text to the schema's fields.

    How ``with_structured_output`` answers offline. Defaults below cover the
    research agent; a test swaps one entry (``monkeypatch.setitem``) to steer a
    graph down a specific path, e.g. a critic that asks for follow-ups.
    """

    @property
    @override
    def _llm_type(self) -> str:
        return "scripted"

    @override
    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> BaseChatModel:
        return self

    @override
    def with_structured_output(  # type: ignore[override]
        self, schema: type[BaseModel], **kwargs: Any
    ) -> Runnable[Any, BaseModel]:
        if schema.__name__ not in self.structured_scripts:
            raise NotImplementedError(
                f"ScriptedChatModel has no structured script for {schema.__name__}; "
                "add one to ScriptedChatModel.structured_scripts."
            )

        def answer(value: Any) -> BaseModel:
            # Looked up per call, not at bind time: graphs are compiled when
            # the app boots, before a test gets the chance to swap a script.
            script = self.structured_scripts[schema.__name__]
            messages = value.to_messages() if hasattr(value, "to_messages") else value
            text = "\n\n".join(str(m.content) for m in messages)
            return schema.model_validate(script(text))

        return RunnableLambda(answer, name=f"scripted_{schema.__name__}")

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        text = str(messages[-1].content) if messages else ""
        if "[[tool:" in text:
            import json as _json
            import re

            match = re.search(r"\[\[tool:(\w+)\((.*?)\)\]\]", text)
            if match:
                name, raw = match.group(1), match.group(2) or "{}"
                return AIMessage(
                    content="",
                    tool_calls=[{"name": name, "args": _json.loads(raw), "id": "call_1"}],
                )
        if self.responses:
            reply = self.responses[self._cursor % len(self.responses)]
            object.__setattr__(self, "_cursor", self._cursor + 1)
            return AIMessage(content=reply)
        # Shape the reply to the prompt so the scripted model still exercises
        # the real code paths (planner -> N questions, worker -> a sentence).
        if text.startswith("Answer this research question"):
            # Shaped like a real web-search reply: content blocks, with the
            # citation on the text block, so source extraction runs offline too.
            question = text.rsplit("Question:", 1)[-1].strip()
            slug = "-".join(question.lower().split())[:60].strip("?-")
            return AIMessage(
                content=[
                    {
                        "type": "text",
                        "text": f"Scripted answer to: {question}",
                        "citations": [
                            {
                                "type": "web_search_result_location",
                                "url": f"https://example.com/{slug}",
                                "title": f"Example source for {question}",
                                "cited_text": "scripted",
                            }
                        ],
                    }
                ]
            )
        if text.startswith("Write a short briefing"):
            return AIMessage(content="Scripted briefing covering every finding above.")
        return AIMessage(content=f"echo: {text}")

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    @override
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        from langchain_core.messages import AIMessageChunk

        message = self._next(messages)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content="", tool_calls=message.tool_calls)
            )
            return
        if isinstance(message.content, list):  # content blocks arrive whole
            yield ChatGenerationChunk(message=AIMessageChunk(content=message.content))
            return
        for word in str(message.content).split(" "):
            yield ChatGenerationChunk(message=AIMessageChunk(content=word + " "))


def _scripted_plan(text: str) -> dict[str, Any]:
    topic = text.rsplit("Topic:", 1)[-1].strip() or "the topic"
    return {
        "questions": [
            {"question": f"What is {topic}?", "why": "Defines the subject."},
            {"question": f"Why does {topic} matter now?", "why": "Establishes stakes."},
            {"question": f"What are the main risks of {topic}?", "why": "Surfaces objections."},
        ]
    }


def _scripted_critique(text: str) -> dict[str, Any]:
    """Score cited findings 0.8 and uncited 0.3; ask for nothing more."""
    findings = json.loads(text.rsplit(FINDINGS_MARKER, 1)[-1].strip())
    return {
        "assessments": [
            {
                "question": f["question"],
                "confidence": 0.8 if f["sources"] else 0.3,
                "issue": "" if f["sources"] else "No sources.",
            }
            for f in findings
        ],
        "follow_ups": [],
    }


FINDINGS_MARKER = "FINDINGS (JSON):"
"""Where the critic prompt puts its findings, so the scripted critic can read them."""


def _scripted_verification(text: str) -> dict[str, Any]:
    """Every sentence supported: offline, verification changes nothing."""
    return {"issues": []}


def _scripted_revision(text: str) -> dict[str, Any]:
    return {"rewrites": []}


ScriptedChatModel.structured_scripts.update(
    {
        "ResearchPlan": _scripted_plan,
        "Critique": _scripted_critique,
        "Verification": _scripted_verification,
        "Revision": _scripted_revision,
    }
)


# -- routing: which model serves which role of which agent ------------------

DEFAULT_ROLE = "default"
"""In ``MODELS__<AGENT>__DEFAULT`` and in a run's ``models`` map: every role of
the agent that is not named explicitly."""


class ModelConfigError(ValueError):
    """MODELS__* names an agent or role that does not exist. Raised at boot."""


class ModelChoiceError(ValueError):
    """A run asked for a role the agent lacks or a model that is not allowed."""


def resolve_models(
    settings: Settings,
    agent: str,
    roles: Sequence[str],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The model for every role of ``agent``.

    Most specific wins: run override for the role, run override ``default``,
    ``MODELS__<AGENT>__<ROLE>``, ``MODELS__<AGENT>__DEFAULT``, then ``MODEL``.
    ``overrides`` must already have passed :func:`check_overrides`.
    """
    configured = settings.models.get(agent, {})
    run = dict(overrides or {})
    return {
        role: run.get(role)
        or run.get(DEFAULT_ROLE)
        or configured.get(role)
        or configured.get(DEFAULT_ROLE)
        or settings.model
        for role in roles
    }


def check_overrides(
    settings: Settings, agent: str, roles: Sequence[str], overrides: Mapping[str, str]
) -> dict[str, str]:
    """Validate a run request's ``models`` map. Returns it normalised."""
    known = {*roles, DEFAULT_ROLE}
    allowed = settings.allowed_models
    out: dict[str, str] = {}
    for raw_role, raw_name in overrides.items():
        role, name = raw_role.strip().lower(), raw_name.strip()
        if role not in known:
            raise ModelChoiceError(
                f"Agent {agent!r} has no model role {raw_role!r}. "
                f"Roles: {', '.join(sorted(known))}."
            )
        if name not in allowed:
            raise ModelChoiceError(
                f"Model {name!r} is not allowed. Allowed: {', '.join(sorted(allowed))}."
            )
        out[role] = name
    return out


def check_model_config(settings: Settings, roles_by_agent: Mapping[str, Sequence[str]]) -> None:
    """Fail at boot on a typo in MODELS__*, not silently at the first run."""
    for agent, configured in settings.models.items():
        if agent not in roles_by_agent:
            raise ModelConfigError(
                f"MODELS__{agent.upper()}__* names no agent. "
                f"Agents: {', '.join(sorted(roles_by_agent))}."
            )
        known = {*roles_by_agent[agent], DEFAULT_ROLE}
        for role in configured:
            if role not in known:
                raise ModelConfigError(
                    f"MODELS__{agent.upper()}__{role.upper()}: agent {agent!r} has no role "
                    f"{role!r}. Roles: {', '.join(sorted(known))}."
                )


class ModelRouter:
    """Hands each node the model for its role in the current run.

    Built once per graph. ``bind`` returns a :class:`RoleModel`: call it with
    the run's context to get a ready runnable (structured output, tools, tags
    applied). Runnables are built on first use and cached by model name, so a
    per-run choice costs one construction per process, not one per call.
    """

    def __init__(self, settings: Settings, agent: str, roles: Sequence[str]) -> None:
        self.settings = settings
        self.agent = agent
        self._roles = tuple(roles)

    def name(self, role: str, context: AgentContext | None) -> str:
        if role not in self._roles:
            raise KeyError(f"{self.agent!r} declares no model role {role!r}")
        chosen = context.model_for(role) if context is not None else None
        return chosen or resolve_models(self.settings, self.agent, (role,))[role]

    def bind(
        self,
        role: str,
        prepare: Callable[[BaseChatModel], Any] | None = None,
        **build_kwargs: Any,
    ) -> RoleModel:
        """``prepare`` turns the bare chat model into what the node invokes."""
        if role not in self._roles:
            raise KeyError(f"{self.agent!r} declares no model role {role!r}")
        return RoleModel(self, role, prepare, build_kwargs)


class CallInterceptor(Protocol):
    """Wraps a role's runnable when it is built. See :data:`INTERCEPTOR`."""

    def wrap(self, agent: str, role: str, model: str, runnable: Any) -> Any: ...


INTERCEPTOR: ContextVar[CallInterceptor | None] = ContextVar("model_interceptor", default=None)
"""Set by the eval harness (``evals.replay``) to record and replay model calls.

Never set by the service. Read when a role's runnable is first built, so it
applies to graphs built and first run inside the context that set it.
"""


class RoleModel:
    """One role's runnable, per model name. See :meth:`ModelRouter.bind`."""

    def __init__(
        self,
        router: ModelRouter,
        role: str,
        prepare: Callable[[BaseChatModel], Any] | None,
        build_kwargs: dict[str, Any],
    ) -> None:
        self._router = router
        self.role = role
        self._prepare = prepare
        self._build_kwargs = build_kwargs
        self._built: dict[str, Any] = {}

    def name(self, context: AgentContext | None) -> str:
        return self._router.name(self.role, context)

    def __call__(self, context: AgentContext | None) -> Any:
        return self.named(self.name(context))

    def named(self, name: str) -> Any:
        """For a node whose input already carries the model (a cached node)."""
        if name not in self._built:
            model = build_model(self._router.settings, model=name, **self._build_kwargs)
            prepared = self._prepare(model) if self._prepare else model
            # Inherited by the chat model's callbacks, so usage and traces can
            # be attributed to a role without knowing the graph's node names.
            built = prepared.with_config(metadata={"model_role": self.role, "model_name": name})
            if (interceptor := INTERCEPTOR.get()) is not None:
                built = interceptor.wrap(self._router.agent, self.role, name, built)
            self._built[name] = built
        return self._built[name]


def stamp_model(message: BaseMessage, configured: str) -> BaseMessage:
    """Record on a reply which model wrote it, as ``response_metadata["model_name"]``.

    Anthropic replies already carry the model the API reports (which, for an
    alias, is the dated snapshot that answered). Anything else, such as the
    scripted test model, gets the configured name. Stored with the message in
    the checkpoint, so the transcript shows it after a reload too.
    """
    message.response_metadata.setdefault("model_name", configured)
    return message


def model_of(message: BaseMessage, configured: str) -> str:
    """The model that wrote ``message``: as reported, else as configured."""
    return str((message.response_metadata or {}).get("model_name") or configured)


def structured(model: BaseChatModel, schema: type[BaseModel], settings: Settings) -> Any:
    """``model.with_structured_output(schema)`` using ``STRUCTURED_OUTPUT``.

    The default, ``json_schema``, constrains decoding to the schema. With
    ``function_calling`` the schema is only a tool definition, and the live
    evals caught claude-sonnet-5 sending a list as a string with a stray
    ``<parameter>`` tag inside, which fails validation on every retry.
    """
    return model.with_structured_output(schema, method=settings.structured_output)


def build_model(
    settings: Settings,
    *,
    model: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """The single place in the codebase that names a provider.

    Pass ``temperature=`` here when a specific call site wants variety; it is
    otherwise omitted from the request. See :func:`anthropic_options`.
    """
    if settings.fake_model:
        return ScriptedChatModel()

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(**anthropic_options(settings, model=model, **kwargs))


def anthropic_options(
    settings: Settings,
    *,
    model: str | None = None,
    temperature: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build the constructor kwargs. Separated out so it is testable offline.

    `temperature` is only included when someone asked for one. Newer Anthropic
    models reject the parameter outright -- extended thinking pins its own
    sampling -- so sending a default of 0.0 turns every call into a 400. Leaving
    it out is also what you want on older models: their default already is 0.
    Ask for it per call site when you actually want variety.
    """
    # Spelled as a dict because several of these are pydantic aliases.
    options: dict[str, Any] = {
        "model": model or settings.model,
        "max_tokens": settings.max_tokens,
        "api_key": settings.anthropic_api_key,
        **kwargs,
    }
    resolved = temperature if temperature is not None else settings.model_temperature
    if resolved is not None:
        options["temperature"] = resolved
    return options


class HashEmbeddings(Embeddings):
    """Deterministic, offline embeddings for tests: same text, same unit vector.

    langchain's DeterministicFakeEmbedding does the same but needs numpy, which
    this service does not otherwise depend on.
    """

    def __init__(self, dims: int) -> None:
        self.dims = dims

    def embed_query(self, text: str) -> list[float]:
        import hashlib
        import math
        import random

        rng = random.Random(hashlib.sha256(text.encode()).digest())
        vector = [rng.gauss(0.0, 1.0) for _ in range(self.dims)]
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]


VOYAGE_DIMS = frozenset({256, 512, 1024, 2048})
"""Output widths the Voyage 4 series offers (docs.voyageai.com, checked 2026-10-10)."""


class EmbeddingsConfigError(RuntimeError):
    """EMBEDDINGS_MODEL is set but cannot work. Raised at boot, not at first use."""


def build_embeddings(settings: Settings) -> Embeddings | None:
    """The single place that names an embeddings provider.

    ``None`` means no vector index: the store still does exact gets and puts,
    but ``asearch(query=...)`` ignores the query. With ``FAKE_MODEL=true`` the
    embedder is a deterministic hash, so tests exercise the pgvector path
    without a network call (the vectors carry no meaning, only the plumbing).
    """
    return _embeddings(
        settings,
        settings.embeddings_model,
        settings.embeddings_dims,
        setting="EMBEDDINGS_MODEL",
        unset_hint="unset EMBEDDINGS_MODEL to run the store without vector search",
    )


def build_rag_embeddings(settings: Settings) -> Embeddings | None:
    """The embedder for corpus chunks and retrieval queries (``RAG__*``).

    ``None`` means RAG is off. Same providers and same offline stand-in as
    :func:`build_embeddings`, configured separately.
    """
    return _embeddings(
        settings,
        settings.rag.embeddings_model,
        settings.rag.embeddings_dims,
        setting="RAG__EMBEDDINGS_MODEL",
        unset_hint="unset RAG__EMBEDDINGS_MODEL to run without document retrieval",
    )


def _embeddings(
    settings: Settings, spec: str | None, dims: int, *, setting: str, unset_hint: str
) -> Embeddings | None:
    if not spec:
        return None
    if settings.fake_model:
        return HashEmbeddings(dims=dims)

    provider, sep, name = spec.partition(":")
    if not sep:
        raise EmbeddingsConfigError(
            f"{setting}={spec!r} must be 'provider:model', e.g. 'openai:text-embedding-3-small'."
        )

    def need(key: str | None, env: str) -> str:
        if not key:
            raise EmbeddingsConfigError(
                f"{setting}={spec!r} needs {env}. Set it in .env, or {unset_hint}."
            )
        return key

    if provider == "voyageai":
        # Built directly rather than through init_embeddings: Voyage models
        # take the output width as a request parameter, so the width we index
        # with is the width we ask for, not a model default that could move.
        if dims not in VOYAGE_DIMS:
            raise EmbeddingsConfigError(
                f"{setting}={spec!r} offers output widths {sorted(VOYAGE_DIMS)}, not {dims}. "
                "Set the matching *_DIMS setting to one of them."
            )
        try:
            from langchain_voyageai import VoyageAIEmbeddings
        except ImportError as exc:
            raise EmbeddingsConfigError(
                f"{setting}={spec!r}: langchain-voyageai is not installed ({exc})."
            ) from exc
        return VoyageAIEmbeddings(  # type: ignore[call-arg]
            model=name,
            output_dimension=cast("Literal[256, 512, 1024, 2048]", dims),
            voyage_api_key=need(settings.voyage_api_key, "VOYAGE_API_KEY"),
        )

    kwargs: dict[str, Any] = {}
    if provider == "openai":
        kwargs["api_key"] = need(settings.openai_api_key, "OPENAI_API_KEY")

    from langchain.embeddings import init_embeddings

    try:
        return init_embeddings(spec, **kwargs)
    except ImportError as exc:
        raise EmbeddingsConfigError(
            f"{setting}={spec!r}: the provider package is not installed ({exc})."
        ) from exc


def http_status(exc: BaseException) -> int | None:
    """The HTTP status a provider error carries, whatever the SDK calls it.

    OpenAI's and Anthropic's SDKs say ``status_code``; Voyage's says
    ``http_status``. Reading only the first let a rejected Voyage key through
    the boot probe as a mere network warning (found 2026-10-10).
    """
    for attr in ("status_code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


async def check_embedding_dims(
    embeddings: Embeddings, expected: int, *, setting: str = "EMBEDDINGS_DIMS"
) -> None:
    """Embed one string at boot and compare its length with ``setting``.

    A mismatch otherwise surfaces as a pgvector error on the first ``remember``,
    long after the misconfiguration. A bad key fails here too. A network error
    is only logged: a provider blip should not stop the service from booting.
    """
    try:
        vector = await embeddings.aembed_query("dimension probe")
    except Exception as exc:
        status = http_status(exc)
        if status in {401, 403}:
            raise EmbeddingsConfigError(
                f"The embeddings provider rejected the API key ({status})."
            ) from exc
        log.warning("embeddings probe failed; dimensions not verified", exc_info=True)
        return
    if len(vector) != expected:
        raise EmbeddingsConfigError(
            f"The embeddings model returns {len(vector)} dimensions but "
            f"{setting}={expected}. Set {setting}={len(vector)}."
        )
