from flask import Blueprint

from waypoint.api import agents, health, keys, memories, runs, threads

api = Blueprint("api", __name__)
for module in (health, agents, threads, runs, keys, memories):
    api.register_blueprint(module.bp)

__all__ = ["api"]
