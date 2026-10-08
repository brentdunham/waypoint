"""Shared type aliases.

``CompiledStateGraph`` is generic over four parameters (state, context, input,
output). Every graph in the registry has a different concrete spelling, so the
registry and everything downstream is typed against the erased alias.
"""

from typing import Any

from langgraph.graph.state import CompiledStateGraph

type Graph = CompiledStateGraph[Any, Any, Any, Any]
