from typing import Any

from flask import Blueprint, jsonify

from waypoint.agents import list_specs

bp = Blueprint("agents", __name__, url_prefix="/agents")


@bp.get("")
def index() -> Any:
    """The frontend builds its agent picker from this."""
    return jsonify(agents=list_specs())
