"""The dataset format.

One JSON object per line in ``datasets/<agent>.jsonl``. A case says what to
send, how to answer the agent's interrupts, which programmatic checks apply,
and which rubric criteria the judge grades. ``reviewed`` is the date a person
last confirmed the case still makes sense; research facts drift.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).parent
DATASETS = ROOT / "datasets"


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    input: str | list[str]
    """Research: the topic. Chat: one user message, or several for a multi-turn case."""
    resume: list[Any] = Field(default_factory=lambda: [{"approved": True}])
    """Answers to the agent's interrupts, in order. Research: the plan approval."""
    seed: list[str] = Field(default_factory=list)
    """Memories stored for the case's user before the first turn (chat)."""
    checks: dict[str, Any] = Field(default_factory=dict)
    """Programmatic check name -> its argument (``True`` when it takes none)."""
    criteria: list[str] = Field(default_factory=list)
    """Rubric criteria the judge grades; empty -> every criterion in the rubric."""
    judge: bool = True
    """False for cases only programmatic checks can score (a declined plan)."""
    expect: str = ""
    """What a good answer contains, for the judge. Not a reference answer."""
    tags: list[str] = Field(default_factory=list)
    notes: list[str] | None = None
    """Workspace: run the demo notes MCP server (``waypoint.mcp_demo.notes``)
    holding these notes for the case's tenant, and give the run its tools
    (scope ``tools:notes``). ``[]`` is the server with no notes. Its write
    tools pause for approval; ``resume`` answers them."""
    corpus: str | None = None
    """Research: a corpus under ``corpora/`` the workers also search (RAG). The
    driver indexes it once per process and passes its id as ``corpus_id``."""
    reviewed: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @property
    def turns(self) -> list[str]:
        return [self.input] if isinstance(self.input, str) else list(self.input)


def load_dataset(agent: str, *, path: Path | None = None) -> list[Case]:
    file = path or DATASETS / f"{agent}.jsonl"
    cases: list[Case] = []
    for n, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            cases.append(Case.model_validate(json.loads(line)))
        except Exception as exc:
            raise ValueError(f"{file.name}:{n}: {exc}") from exc
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{file.name}: duplicate case ids")
    return cases
