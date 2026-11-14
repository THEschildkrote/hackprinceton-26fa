"""module-level graphs for `langgraph dev` (see langgraph.json).

studio can't pass a building id at build time, so the dossier graph is built for NUDGE_SID
(default "synthetic"). the exception graph needs a live navigator, so it gets one started at the
building's entrance heading for NUDGE_GOAL (default "Conference Room") with profile NUDGE_PROFILE.

the langgraph server persists threads itself, so the agents' own MemorySaver is dropped here.
importing this works without keys: init_chat_model only needs ANTHROPIC_API_KEY on the first call.
"""
import os

from .dossier import create_dossier_agent
from .exception import create_exception_agent

SID = os.environ.get("NUDGE_SID", "synthetic")
GOAL = os.environ.get("NUDGE_GOAL", "Conference Room")
PROFILE = os.environ.get("NUDGE_PROFILE", "blind")


def _serverless(graph):
    # langgraph dev supplies its own checkpointer
    graph.checkpointer = None
    return graph


def _exception_graph():
    from ..navigator import Navigator
    from .tools import graph as walk_graph, key_places
    g = walk_graph(SID)
    entry, _ = key_places(g)
    nav = Navigator(g, GOAL, PROFILE)
    nav.start(entry.x, entry.y, entry.floor)
    return create_exception_agent(nav, g, f"travels as: {PROFILE}")


dossier = _serverless(create_dossier_agent(SID))
exception = _serverless(_exception_graph())
