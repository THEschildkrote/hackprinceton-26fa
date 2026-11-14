"""explorer agent: what to do when the map runs out.

invoked when the navigator reports "unmapped" (the traveller walked somewhere no scan covers), or when the
destination building has no scan or no dossier yet. this is where planning matters: survey the route a first
visitor will walk, join the building's other scans, fall back to an outside-in approach from the aerial and
the costar record, or tell the traveller to stop and get help.

the split with the deterministic layer is strict. the agent never hands the navigator coordinates. its one
acting tool, commit_plan, can only (a) set a destination the router resolves, (b) add a portal that
building.candidates_portals proposed, marked uncertain until the cartographer confirms it, or (c) block a pano.
and it pauses for the traveller's approval first (deep agents hitl). every metre and degree still comes from
the router and the scan geometry; while off the map the navigator itself walks them back along their own trail.
"""
import json
import threading

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from .. import building
from ..instruct import meters
from ..router import NoRoute, resolve_goal
from . import tools as T
from .exception import approve, handle_event, make_tools as exception_tools, new_thread  # noqa: F401

SYSTEM_PROMPT = """You help a blind or low-vision traveller when the map runs out. Either they have walked into
an area no 3D scan covers (the navigator reports "unmapped"), or they are heading to a building, or a part of
one, that has no scan or no accessibility dossier yet. Turn-by-turn guidance is deterministic and handled
elsewhere: while they are off the map, the navigator already walks them back along their own footsteps, or
tells them to stop if it can't. Never invent directions, distances or coordinates; only say what your tools
returned.

Start with explorer_status, then plan with write_todos. Your options, in order of preference:
1. Back to the map. If explorer_status shows a way back, tell them briefly that following it is the safest
   step, then help with what comes after.
2. Another scan of the same building. building_models and portal_candidates show scans that may connect
   (an elevator lobby, a floor). For a portal the traveller needs right now, commit_plan(portal_id=...) adds it
   as an uncertain link; delegate building-cartographer to confirm portals for future visits.
3. A first visit with no dossier. quick_survey(sid, goal) starts a route-scoped survey of only the panos
   near their route (hazards first), so the router can avoid what's found within minutes.
4. No interior scan at all. aerial_view and costar_record, or delegate site-surveyor, to find the accessible
   entrance from outside. Describe the approach from what you found, and recommend asking staff once inside.
5. Nothing safe. Tell them to stop where they are, use their cane, and offer to call for help.

commit_plan is the only tool that changes the trip, and the traveller approves it first. Keep spoken replies
short: at most two sentences per option, distances in meters, no visual-only cues. Ask one question when the
choice is theirs.
"""


def _scans(g):
    return sorted({f.get("sid") for f in g.floors if f.get("sid")}) or []


def make_tools(nav, g, survey_runner=None, sid=None):
    """tools bound to one live navigator. survey_runner(sid, focus, goal) runs a dossier (default: in a thread)."""
    base = {t.name: t for t in exception_tools(nav, g)}
    candidates = {}

    def runner(s, focus, goal):
        if survey_runner is not None:
            return survey_runner(s, focus, goal)
        from .dossier import run

        threading.Thread(target=run, kwargs={"sid": s, "focus": focus, "goal": goal,
                                             "on_event": lambda e: print(f"   quick survey {s}: {e}")},
                         daemon=True).start()

    @tool(parse_docstring=True)
    def explorer_status() -> str:
        """Where the traveller is relative to the map: unmapped or not, why, the way back along their own steps
        (metres), the last room they were in, the goal, and which scans this trip's map is made of.
        """
        st = nav.state() if nav.route else {"route": None}
        back = nav.way_back() if nav.unmapped else None
        last = nav.last_on_map
        last_room = None
        if last and last[2] in nav.floors:
            rid = g.room_at(last[0], last[1], last[2])
            last_room = g.rooms.get(rid, {}).get("label") if rid else None
        scans = _scans(g) or ([sid] if sid else [])
        return json.dumps({"unmapped": nav.unmapped,
                           "way_back_m": None if not back else round(back["m"], 1),
                           "way_back_spoken": None if not back else meters(back["m"]),
                           "can_return": bool(back), "last_room_on_map": last_room, "goal": nav.goal,
                           "profile": nav.profile, "scans": scans, "done": nav.done,
                           "remaining_m": st.get("remaining"), "reroutes": nav.reroutes}, default=str)

    @tool(parse_docstring=True)
    def dossier_status(sid: str) -> str:
        """Whether a scan has an accessibility dossier yet: annotations, briefing, portals, and counts.

        Args:
            sid: Matterport model id.
        """
        d = T.dossier_dir(sid)
        ann, err = T.read_annotations(sid)
        return json.dumps({"sid": sid, "annotations": ann is not None, "error": err,
                           "hazards": len((ann or {}).get("hazards") or []),
                           "landmarked_panos": sum(1 for v in ((ann or {}).get("nodes") or {}).values()
                                                   if v.get("landmarks")),
                           "briefing": (d / "briefing.md").exists(), "portals": (d / "portals.json").exists()})

    @tool(parse_docstring=True)
    def quick_survey(sid: str, goal: str) -> str:
        """Start a route-scoped survey for a first visit: hazards (then landmarks) only for panos within 3 m of
        the route from the scan's entrance to the goal. Runs in the background; the router uses its findings as
        soon as annotations.json is written.

        Args:
            sid: Matterport model id of the scan to survey.
            goal: Destination room label or pano id in that scan.
        """
        try:
            rf = T.route_focus(sid, goal, nav.profile)
        except NoRoute as ex:
            return json.dumps({"started": False, "error": str(ex)})
        runner(sid, rf["focus"], goal)
        return json.dumps({"started": True, "sid": sid, "goal": goal, "route_m": rf["length_m"],
                           "focus_panos": len(rf["focus"]), "focus": rf["focus"]})

    @tool(parse_docstring=True)
    def scan_portal_candidates(sid: str) -> str:
        """Candidate links (elevator, stairs, entrance) between this building's scans, with ids you can pass to
        commit_plan(portal_id=...). Only candidates whose both ends are in the traveller's current map can be
        committed now.

        Args:
            sid: Matterport model id (any scan of the building).
        """
        sids = [x["sid"] for x in building.siblings(sid)]
        if len(sids) < 2:
            return json.dumps({"candidates": [], "note": "only one scan of this building"})
        graphs = {s: T.graph(s, annotated=False) for s in sids}
        out = building.candidates_portals(sids, graphs)
        for c in out:
            c["committable_now"] = c["a"] in g.nodes and c["b"] in g.nodes
            candidates[c["id"]] = c
        return json.dumps({"candidates": out})

    @tool(parse_docstring=True)
    def commit_plan(goal: str = "", portal_id: str = "", block_node: str = "", reason: str = "") -> str:
        """Change the trip. The traveller approves this first. Only one of: a new destination (resolved by the
        router), a portal from scan_portal_candidates (added as an uncertain link until confirmed), or a pano to
        block. Never coordinates.

        Args:
            goal: New destination: a room label or pano id the router can resolve.
            portal_id: A candidate id from scan_portal_candidates, e.g. pc0.
            block_node: A pano id (or "sid:pano") to avoid.
            reason: One short sentence to tell the traveller why.
        """
        if sum(bool(x) for x in (goal, portal_id, block_node)) != 1:
            return json.dumps({"ok": False, "error": "give exactly one of goal, portal_id, block_node"})
        x, y, floor = nav.pos if nav.pos else (None, None, None)
        if portal_id:
            c = candidates.get(portal_id)
            if not c:
                return json.dumps({"ok": False, "error": f"{portal_id} is not a known candidate; call scan_portal_candidates"})
            if c["a"] not in g.nodes or c["b"] not in g.nodes:
                return json.dumps({"ok": False, "error": "both ends must be in the traveller's current map"})
            e = g.link(c["a"], c["b"], ["portal", "vertical", "uncertain"] +
                       ([c["kind"]] if c["kind"] in ("elevator", "stairs") else []))
            e.length = float(c.get("cost_m") or 4.0)
            e.info = {"kind": c["kind"], "note": None, "direction": c.get("direction"),
                      "reverse_direction": {"up": "down", "down": "up"}.get(c.get("direction"), c.get("direction")),
                      "a_map": c.get("a_map"), "b_map": c.get("b_map")}
            if nav.unmapped or nav.pos is None:
                return json.dumps({"ok": True, "added_portal": portal_id, "note": "route updates when back on the map"})
            ev = nav._reroute(x, y, floor, nav.heading, reason or "")
            return json.dumps({"ok": ev[0]["type"] != "no_route", "added_portal": portal_id,
                               "say": [e["text"] for e in ev if e.get("text")]})
        if goal:
            if not resolve_goal(g, goal):
                return json.dumps({"ok": False, "error": f"no place matches {goal!r} in the current map"})
            nav.goal, nav.done = goal, False
            if nav.unmapped or nav.pos is None:
                return json.dumps({"ok": True, "goal": goal, "note": "route updates when back on the map"})
            ev = nav._reroute(x, y, floor, nav.heading, reason or "")
            return json.dumps({"ok": ev[0]["type"] != "no_route", "goal": goal,
                               "say": [e["text"] for e in ev if e.get("text")]})
        if block_node not in g.nodes:
            return json.dumps({"ok": False, "error": f"{block_node!r} is not a pano in the current map"})
        if nav.unmapped or nav.pos is None:
            nav.blocked_nodes.add(block_node)
            return json.dumps({"ok": True, "blocked": block_node, "note": "route updates when back on the map"})
        ev = nav.block(x, y, floor, block_node, why=reason or "That way is blocked.")
        return json.dumps({"ok": ev[0]["type"] != "no_route", "blocked": block_node,
                           "say": [e["text"] for e in ev if e.get("text")]})

    return [explorer_status, dossier_status, quick_survey, scan_portal_candidates, T.building_models,
            T.aerial_view, T.costar_record, base["compare_options"], base["nav_status"], base["find_place"],
            commit_plan]


def create_explorer_agent(nav, g, sid=None, model=None, vision_model=None, checkpointer=None, survey_runner=None,
                          user_profile_text=""):
    """sid: the scan whose dossier the subagents work on (defaults to the first scan in the map)."""
    from .dossier import READ_ONLY_AGENT, SUBAGENTS, _prompt, backend
    from .llm import model as default_model

    sid = sid or (_scans(g) or [None])[0] or "synthetic"
    m = model if model is not None else default_model(live=True)
    vm = vision_model if vision_model is not None else (m if model is not None else default_model(role="vision"))
    subagents = [{"name": name, "description": SUBAGENTS[name][0], "system_prompt": _prompt(name),
                  "tools": SUBAGENTS[name][1], "skills": ["/agent/skills/"], "permissions": READ_ONLY_AGENT,
                  "model": vm}
                 for name in ("hazard-scout", "site-surveyor", "building-cartographer")]
    prompt = SYSTEM_PROMPT + (f"\nAbout this traveller (from their settings): {user_profile_text}\n"
                              if user_profile_text else "")
    return create_deep_agent(
        model=m,
        tools=make_tools(nav, g, survey_runner, sid),
        subagents=subagents,
        system_prompt=prompt,
        middleware=[TodoListMiddleware()],
        skills=["/agent/skills/"],
        permissions=READ_ONLY_AGENT,
        backend=backend(sid),
        interrupt_on={"commit_plan": True},
        checkpointer=checkpointer or MemorySaver(),
        name="nudge-explorer",
    )


# voice.py drives the explorer like the exception agent (and both get traced there)
from .exception import approve, handle_event, new_thread  # noqa: E402,F401
