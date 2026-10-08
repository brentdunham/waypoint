from fastapi import APIRouter

from waypoint.api import agents, health, keys, memories, runs, threads

api = APIRouter()
for module in (health, agents, threads, runs, keys, memories):
    api.include_router(module.router)

__all__ = ["api"]
