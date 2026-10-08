"""Shared dependencies, as ``Annotated`` aliases so endpoints read cleanly."""

from typing import Annotated

from fastapi import Depends

from waypoint.agents.runtime import AgentRuntime, get_runtime
from waypoint.infra.security import Principal, requires
from waypoint.schemas import SCOPE_ADMIN, SCOPE_READ, SCOPE_RUN, SCOPE_WRITE

Runtime = Annotated[AgentRuntime, Depends(get_runtime)]
Reader = Annotated[Principal, Depends(requires(SCOPE_READ))]
Writer = Annotated[Principal, Depends(requires(SCOPE_WRITE))]
Runner = Annotated[Principal, Depends(requires(SCOPE_RUN))]
Admin = Annotated[Principal, Depends(requires(SCOPE_ADMIN))]
