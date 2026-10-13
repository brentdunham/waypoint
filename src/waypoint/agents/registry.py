"""The agent registry.

Adding an agent is one entry here plus one module under ``graphs/``. Everything
downstream -- routing, the ``/agents`` endpoint, the frontend picker -- reads
from this table, so there is no second place to update.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from waypoint.agents.graphs import chat, ingest, research, workspace
from waypoint.agents.models import check_overrides, resolve_models
from waypoint.agents.types import Graph
from waypoint.errors import NotFound
from waypoint.rag.store import CorpusStore
from waypoint.settings import Settings

type GraphBuilder = Callable[..., Graph]


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: str
    title: str
    description: str
    builder: GraphBuilder
    input_key: str
    """Which state key a plain user message is written into."""
    model_roles: tuple[str, ...]
    """Each model call site in the graph. ``MODELS__<AGENT>__<ROLE>`` and a run's
    ``models`` map choose per role."""
    supports_interrupts: bool = False
    example: dict[str, Any] = field(default_factory=dict)
    listed: bool = True
    """Shown by ``GET /agents`` and open to ``POST /threads``. False for agents
    another endpoint starts (``ingest``, started by the corpora API)."""
    uses_corpora: bool = False
    """The builder takes ``corpora=``, the runtime's corpus store."""

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "input_key": self.input_key,
            "model_roles": list(self.model_roles),
            "supports_interrupts": self.supports_interrupts,
            "example": self.example,
        }


AGENTS: dict[str, AgentSpec] = {
    "chat": AgentSpec(
        name="chat",
        title="Chat",
        description=("Tool-using assistant with long-term memory scoped to your tenant and user."),
        builder=chat.build,
        input_key="messages",
        model_roles=chat.ROLES,
        example={"messages": [{"role": "user", "content": "What time is it in Chicago?"}]},
    ),
    "research": AgentSpec(
        name="research",
        title="Research",
        description=(
            "Plans a topic into questions, waits for your approval, researches each "
            "question in parallel with web search, reviews the findings and follows up "
            "on gaps, then writes a cited briefing."
        ),
        builder=research.build,
        input_key="topic",
        model_roles=research.ROLES,
        supports_interrupts=True,
        uses_corpora=True,
        example={"topic": "small modular reactors for datacentre power"},
    ),
    "workspace": AgentSpec(
        name="workspace",
        title="Workspace",
        description=(
            "Chat over your documents and connected tools, with long-term memory. "
            "Pick a corpus and it answers from it with checked citations; anything "
            "that changes something will wait for your approval."
        ),
        builder=workspace.build,
        input_key="messages",
        model_roles=workspace.ROLES,
        # True from phase 4 (tool approval); nothing in it interrupts yet.
        uses_corpora=True,
        example={
            "messages": [
                {"role": "user", "content": "How does Waypoint recover a dropped event stream?"}
            ],
            "corpus_id": "<a corpus id from GET /corpora>",
        },
    ),
    "ingest": AgentSpec(
        name="ingest",
        title="Ingest",
        description="Parses, chunks and embeds uploaded documents into a corpus.",
        builder=ingest.build,
        input_key="document_ids",
        model_roles=ingest.ROLES,
        listed=False,
        uses_corpora=True,
    ),
}


def get_spec(name: str) -> AgentSpec:
    try:
        return AGENTS[name]
    except KeyError:
        raise NotFound(f"No agent named {name!r}.", available=sorted(AGENTS)) from None


def list_specs() -> list[dict[str, Any]]:
    return [spec.public() for spec in AGENTS.values() if spec.listed]


def listed_agents() -> list[str]:
    """Agents a person talks to directly (and the eval harness evaluates)."""
    return [name for name, spec in AGENTS.items() if spec.listed]


def roles_by_agent() -> dict[str, tuple[str, ...]]:
    return {name: spec.model_roles for name, spec in AGENTS.items()}


def models_for(
    settings: Settings, agent: str, overrides: dict[str, str] | None = None
) -> dict[str, str]:
    """Validate a run's ``models`` request and resolve every role of the agent.

    Raises :class:`~waypoint.agents.models.ModelChoiceError` on a bad request.
    """
    roles = get_spec(agent).model_roles
    checked = check_overrides(settings, agent, roles, overrides) if overrides else {}
    return resolve_models(settings, agent, roles, checked)


def build_all(
    settings: Settings, *, corpora: CorpusStore | None = None, **compile_kwargs: Any
) -> dict[str, Graph]:
    graphs: dict[str, Graph] = {}
    for name, spec in AGENTS.items():
        extra: dict[str, Any] = {"corpora": corpora} if spec.uses_corpora else {}
        graphs[name] = spec.builder(settings, **extra, **compile_kwargs)
    return graphs
