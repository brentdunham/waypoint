from fastapi import APIRouter

from waypoint.api import agents, corpora, health, keys, memories, runs, threads, usage

api = APIRouter()
for module in (health, agents, threads, runs, keys, memories, corpora, usage):
    api.include_router(module.router)

__all__ = ["api"]
