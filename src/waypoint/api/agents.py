from fastapi import APIRouter

from waypoint.agents import list_specs
from waypoint.schemas import AgentInfo, AgentList

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
async def index() -> AgentList:
    """The frontend builds its agent picker from this."""
    return AgentList(agents=[AgentInfo.model_validate(spec) for spec in list_specs()])
