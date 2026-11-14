"""runtime exception agent: when the plan breaks (elevator out, locked door, user lost), reason about
soft constraints the graph doesn't encode, then act through the same deterministic router.

every option it offers comes from router output; it never invents geometry. the only tool that changes
anything is commit_reroute, and that one pauses for the user's approval (deepagents hitl).
"""
import json
from uuid import uuid4

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ..geom import dist
from ..instruct import maneuvers, meters
from ..navigator import snap
from ..router import PROFILES, NoRoute, alternatives, plan, resolve_goal

SYNONYMS = {
    "restroom": ["restroom", "bathroom", "toilet", "washroom", "wc", "lavatory"],
    "exit": ["exit", "entrance", "entry", "lobby", "foyer", "vestibule"],
    "elevator": ["elevator", "lift"],
    "stairs": ["stair", "steps"],
}

SYSTEM_PROMPT = """You help a blind or low-vision traveller when their indoor route breaks: an elevator is out,
a door is locked, a hallway is blocked, or they are lost. Live turn-by-turn guidance is handled elsewhere;
you only handle the exception.

How to work:
- The traveller is standing still waiting for you, so be quick. Each report arrives with nav_status,
  list_nearby and compare_options for the link just ahead already run. Don't call those again unless
  something you need is missing; when the compare_options result fits the problem, call commit_reroute
  in your first turn with a one-sentence reason.
- If you need more, call every tool you need in the same turn rather than one per turn. Use
  compare_options to see what a different choice costs before suggesting it. Use alternate_goals when
  another place could serve the same need.
- When the traveller names a place or a need ("the kitchenette", "somewhere to get water"), call find_place
  and use the name it returns as the goal. If it returns a question, ask that question.
- Never invent rooms, doors, distances or directions. Every option you mention must come from a tool result.
- Hard constraints come from the traveller's profile and are never violated (a wheelchair user never takes
  stairs). If no route satisfies them, say so plainly and offer alternate goals or asking staff for help.
- Soft preferences: fewer hazards, fewer turns, no uncertain links, then shorter distance.
- Speak like a calm guide. At most two short sentences per option, distances in meters, no visual-only cues.
- When the choice is genuinely the traveller's (e.g. a long accessible detour vs a different entrance),
  ask one clear question instead of deciding.
- To change the plan call commit_reroute. The traveller approves it before it takes effect; if they reject
  it, do not retry the same call, ask what they would prefer.
"""


def _j(x):
    return json.dumps(x, separators=(",", ":"), default=str)


def _node_id(g, ref):
    """pano id or pano label -> id, or None."""
    if not ref:
        return None
    if ref in g.nodes:
        return ref
    for n in g.nodes.values():
        if n.label == ref:
            return n.id
    return None


def _edge_ids(g, ref):
    """'a|b' or 'a,b' (ids or labels) -> (a, b) if that edge exists."""
    if not ref:
        return None
    for sep in ("|", ",", "-"):
        if sep in ref:
            a, b = (_node_id(g, s.strip()) for s in ref.split(sep, 1))
            if a and b and g.edge(a, b):
                return a, b
    return None


def _here(nav, g):
    """(x, y, floor, heading) of the traveller, falling back to the route start."""
    if nav.pos:
        x, y, f = nav.pos
        return x, y, f, nav.heading
    n = g.nodes[nav.route.nodes[0]] if nav.route else next(iter(g.nodes.values()))
    return n.x, n.y, n.floor, nav.heading


def _summarize(g, r):
    ms = maneuvers(g, r)
    edges = [g.edge(a, b) for a, b in zip(r.nodes, r.nodes[1:])]
    return {
        "length_m": round(r.length, 1),
        "cost": round(r.cost, 1),
        "floor_changes": [{"via": m.get("via"), "to": m.get("to_floor") or g.nodes[m["to_node"]].floor}
                          for m in ms if m["type"] == "floor"],
        "hazards": sum(len(e.hazards) for e in edges),
        "uncertain_links": sum(1 for e in edges if e.tags & {"uncertain", "vertical_unknown"}),
        "turns": sum(1 for m in ms if m["type"] in ("turn", "bear", "uturn")),
        "first_steps": [m["text"] for m in ms if m.get("text")][:3],
        "end": g.nodes[r.nodes[-1]].room_label or g.nodes[r.nodes[-1]].label,
    }


def make_tools(nav, g):
    """tools bound to one live navigator session."""

    @tool(parse_docstring=True)
    def nav_status() -> str:
        """Where the traveller is and what the current plan is. Call this first.

        Returns:
            JSON with location, goal, profile, remaining metres, next maneuver, blocks and reroute count.
        """
        x, y, f, _ = _here(nav, g)
        nid, d = snap(g, x, y, f)
        n = g.nodes[nid]
        nxt = nav.ms[nav.mi] if nav.route and nav.mi < len(nav.ms) else None
        floor_label = next((fl["label"] for fl in g.floors if fl["id"] == f), f)
        return _j({
            "at": {"room": n.room_label, "floor": floor_label, "floor_id": f, "nearest_pano": n.label,
                   "pano_id": nid, "pano_distance_m": round(d, 1)},
            "goal": nav.goal, "profile": nav.profile,
            "profile_rules": PROFILES[nav.profile].describe() if nav.profile in PROFILES else None,
            "remaining_m": round(max(0.0, nav.route.length - nav.progress), 1) if nav.route else None,
            "next": nxt and nxt.get("text"),
            "blocked_nodes": sorted(g.nodes[b].label for b in nav.blocked_nodes if b in g.nodes),
            "blocked_edges": sorted(f"{g.nodes[a].label}|{g.nodes[b].label}" for a, b in nav.blocked_edges),
            "reroutes": nav.reroutes, "done": nav.done,
        })

    @tool(parse_docstring=True)
    def list_nearby(radius_m: float = 10.0) -> str:
        """Panos, rooms and connections (elevators, stairs, doorways) near the traveller on their floor.

        Args:
            radius_m: Search radius in metres.

        Returns:
            JSON with nearby panos, rooms and connections, nearest first.
        """
        x, y, f, _ = _here(nav, g)
        near = sorted(((dist((x, y), n.xy), n) for n in g.nodes.values() if n.floor == f), key=lambda t: t[0])
        near = [(d, n) for d, n in near if d <= radius_m]
        panos = [{"pano": n.label, "id": n.id, "room": n.room_label, "m": round(d, 1),
                  "tags": sorted(n.tags)} for d, n in near[:20]]
        rooms = []
        for _, n in near:
            if n.room_label and n.room_label not in rooms and n.room_label != "Void":
                rooms.append(n.room_label)
        conns = []
        for d, n in near:
            for kind in ("elevator", "stairs"):
                if kind in n.tags:
                    conns.append({"kind": kind, "pano": n.label, "m": round(d, 1),
                                  "blocked": n.id in nav.blocked_nodes})
        ids = {n.id for _, n in near}
        for e in g.edges():
            if "door" in e.tags and e.a in ids and e.b in ids:
                a, b = g.nodes[e.a], g.nodes[e.b]
                mid = ((a.x + b.x) / 2, (a.y + b.y) / 2)
                conns.append({"kind": "doorway", "between": f"{a.label}|{b.label}",
                              "rooms": [a.room_label, b.room_label], "m": round(dist((x, y), mid), 1),
                              "blocked": (e.a, e.b) in nav.blocked_edges or (e.b, e.a) in nav.blocked_edges})
        conns.sort(key=lambda c: c["m"])
        return _j({"panos": panos, "rooms": rooms, "connections": conns})

    @tool(parse_docstring=True)
    def compare_options(goal: str = "", block_node: str = "", block_edge: str = "",
                        profiles: str = "blind,wheelchair,default") -> str:
        """What-if: route from where the traveller stands under several profiles, with an extra block applied.
        Does not change the live plan.

        Args:
            goal: Destination (room label, pano label or id). Empty means the current goal.
            block_node: Pano id or label to treat as unusable, e.g. a broken elevator.
            block_edge: Link to treat as unusable, as 'A|B' (pano ids or labels), e.g. a locked door.
            profiles: Comma-separated profiles to compare.

        Returns:
            JSON keyed by profile: length, cost, floor changes, hazards, uncertain links, turns, first steps,
            or the reason no route exists.
        """
        x, y, f, _ = _here(nav, g)
        start, _ = snap(g, x, y, f)
        bn, be = set(nav.blocked_nodes), set(nav.blocked_edges)
        if block_node:
            nid = _node_id(g, block_node)
            if not nid:
                return _j({"error": f"unknown pano {block_node!r}"})
            bn.add(nid)
        if block_edge:
            e = _edge_ids(g, block_edge)
            if not e:
                return _j({"error": f"no link {block_edge!r}"})
            be.add(e)
        if start in bn:
            # standing at the blocked spot: start from the nearest usable pano instead
            alt = sorted((dist(g.nodes[start].xy, n.xy), n.id) for n in g.nodes.values()
                         if n.floor == f and n.id not in bn and n.id != start)
            if alt:
                start = alt[0][1]
        want = [p.strip() for p in profiles.split(",") if p.strip() in PROFILES] or [nav.profile]
        res = alternatives(g, start, goal or nav.goal, tuple(want), blocked_edges=be, blocked_nodes=bn)
        return _j({p: (_summarize(g, r) if not isinstance(r, str) else {"no_route": r}) for p, r in res.items()})

    @tool(parse_docstring=True)
    def alternate_goals(query: str) -> str:
        """Other places that could serve the same need (another restroom, another entrance), ranked by
        route length for the traveller's profile with current blocks.

        Args:
            query: What the traveller needs, e.g. 'restroom', 'exit', 'conference room'.

        Returns:
            JSON list of reachable places, nearest first, plus unreachable ones.
        """
        x, y, f, _ = _here(nav, g)
        start, _ = snap(g, x, y, f)
        q = query.lower().strip()
        words = next((v for k, v in SYNONYMS.items() if q in v or k in q), [q])
        rooms = {}
        for n in g.nodes.values():
            lab = (n.room_label or "").lower()
            lm = " ".join(str(l.get("text", "")).lower() for l in n.landmarks)
            if n.room and (any(w in lab for w in words) or any(w in lm for w in words)):
                rooms.setdefault(n.room, n)
        out, unreachable = [], []
        for rid, n in rooms.items():
            goals = resolve_goal(g, rid)
            try:
                r = plan(g, start, goals, nav.profile, nav.blocked_edges, nav.blocked_nodes)
                out.append({"room": n.room_label, "room_id": rid, "floor": n.floor,
                            "length_m": round(r.length, 1), "spoken": meters(r.length)})
            except NoRoute as ex:
                unreachable.append({"room": n.room_label, "room_id": rid, "why": str(ex)})
        out.sort(key=lambda o: o["length_m"])
        return _j({"reachable": out, "unreachable": unreachable})

    @tool(parse_docstring=True)
    def commit_reroute(block_node: str = "", block_edge: str = "", goal: str = "", profile: str = "",
                       reason: str = "") -> str:
        """Change the live plan. The traveller must approve this before it runs.

        Args:
            block_node: Pano id or label that is unusable from now on.
            block_edge: Link that is unusable from now on, as 'A|B'.
            goal: New destination, if the traveller agreed to go somewhere else.
            profile: New profile name, if the traveller asked for one.
            reason: One short sentence the traveller will hear, e.g. 'The elevator is out of service.'

        Returns:
            JSON with the navigator's spoken events and the new route summary.
        """
        nid = _node_id(g, block_node) if block_node else None
        eid = _edge_ids(g, block_edge) if block_edge else None
        if block_node and not nid:
            return _j({"error": f"unknown pano {block_node!r}"})
        if block_edge and not eid:
            return _j({"error": f"no link {block_edge!r}"})
        if goal:
            if not resolve_goal(g, goal):
                return _j({"error": f"no place matches {goal!r}"})
            nav.goal = goal
        if profile:
            if profile not in PROFILES:
                return _j({"error": f"unknown profile {profile!r}"})
            nav.profile = profile
        if eid:
            nav.blocked_edges.add(eid)
        x, y, f, h = _here(nav, g)
        why = reason or "Changing the route."
        if nid:
            ev = nav.block(x, y, f, nid, heading=h, why=why)
        else:
            ev = nav._reroute(x, y, f, h, why)
        ok = not any(e["type"] == "no_route" for e in ev)
        return _j({"ok": ok, "events": [e["text"] for e in ev],
                   "route": _summarize(g, nav.route) if ok and nav.route else None})

    from ..places import find_place_tool  # named places ("the kitchenette", "water") instead of raw ids
    return [nav_status, list_nearby, compare_options, alternate_goals, find_place_tool(nav, g), commit_reroute]


def _ahead(nav, g, look=4):
    """the link the traveller is about to use: the first doorway in the next few links of the route,
    else the very next link. 'a|b' pano ids, or None."""
    if not nav.route or len(nav.route.nodes) < 2:
        return None
    x, y, f, _ = _here(nav, g)
    here, _ = snap(g, x, y, f)
    nodes = nav.route.nodes
    i = nodes.index(here) if here in nodes else min(range(len(nodes)), key=lambda k: dist((x, y), g.nodes[nodes[k]].xy))
    links = list(zip(nodes[i:], nodes[i + 1:]))[:look]
    if not links:
        return None
    door = next(((a, b) for a, b in links if "door" in (g.edge(a, b).tags if g.edge(a, b) else ())), None)
    a, b = door or links[0]
    return f"{a}|{b}"


def prefetch(nav, g, tools):
    """what the agent always asks for first, run before its first turn: the same tools, no model.
    saves three round trips while the traveller stands still."""
    t = {x.name: x for x in tools}
    out = []
    for name, args in (("nav_status", {}), ("list_nearby", {"radius_m": 8.0})):
        try:
            out.append(f"{name}: {t[name].invoke(args)}")
        except Exception as ex:
            out.append(f"{name}: unavailable ({type(ex).__name__})")
    ahead = _ahead(nav, g)
    if ahead:
        try:
            res = t["compare_options"].invoke({"block_edge": ahead, "profiles": nav.profile})
            out.append(f"compare_options with the link just ahead blocked (block_edge={ahead}): {res}")
        except Exception as ex:
            out.append(f"compare_options: unavailable ({type(ex).__name__})")
    return "\n".join(out)


_PREFETCH = {}  # id(agent) -> callable giving the context for its next report


def create_exception_agent(nav, g, user_profile_text="", model=None, checkpointer=None):
    if model is None:
        from .llm import model as default_model
        model = default_model(live=True)
    prompt = SYSTEM_PROMPT
    if user_profile_text:
        prompt += f"\nAbout this traveller (from their settings): {user_profile_text}\n"
    tools = make_tools(nav, g)
    agent = create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=prompt,
        middleware=[TodoListMiddleware()],
        interrupt_on={"commit_reroute": True},
        checkpointer=checkpointer or MemorySaver(),
        name="nudge-exception",
    )
    _PREFETCH[id(agent)] = lambda: prefetch(nav, g, tools)
    return agent


def _cfg(thread_id):
    return {"configurable": {"thread_id": thread_id}}


# ops dashboard tracing (nudge/traces.py); never allowed to break a reroute

def _label(agent):
    name = getattr(agent, "name", "") or ""
    return {"nudge-exception": "exception agent", "nudge-explorer": "explorer agent"}.get(name, name or "agent")


def _traced(cfg, who):
    try:
        from ..traces import config_with
        return config_with(cfg, who)
    except Exception:
        return cfg


def _trace(event, who, **f):
    try:
        from ..traces import clip, publish
        publish("agent", event=event, agent=who, depth=0, **{k: clip(v, 200) for k, v in f.items()})
    except Exception:
        pass


def _trace_reply(who, out):
    if out.get("pending"):
        p = out["pending"]
        _trace("hitl_pending", who, tool=p.get("name"), args=p.get("args"))
    if out.get("say"):
        _trace("say", who, text=" ".join(out["say"]))


def _reply(result, since):
    """lines to speak from ai messages after index `since`, plus the pending approval if any."""
    say = []
    for m in result["messages"][since:]:
        if isinstance(m, AIMessage) and m.text:
            say += [ln.strip() for ln in m.text.splitlines() if ln.strip()]
    pending = None
    intr = result.get("__interrupt__")
    if intr:
        reqs = intr[0].value.get("action_requests", [])
        if reqs:
            a = reqs[0]
            pending = {"name": a["name"], "args": a["args"], "description": a.get("description", "")}
            if a["args"].get("reason") and not say:
                say.append(a["args"]["reason"])
    return {"say": say, "pending": pending}


def handle_event(agent, thread_id, event_text):
    """run the agent on an exception like 'elevator E1 is out of service'."""
    cfg = _cfg(thread_id)
    try:
        since = len(agent.get_state(cfg).values.get("messages", []))
    except Exception:
        since = 0
    who = _label(agent)
    _trace("event", who, text=event_text)
    content = event_text
    ctx = _PREFETCH.get(id(agent))
    if ctx:
        try:
            content = f"{event_text}\n\nAlready looked up for you:\n{ctx()}"
        except Exception:
            pass  # the agent can still look everything up itself
    result = agent.invoke({"messages": [HumanMessage(content=content)]}, config=_traced(cfg, who))
    out = _reply(result, since + 1)
    _trace_reply(who, out)
    return out


def approve(agent, thread_id, decision="approve", message=None):
    """resume after the traveller approves or rejects the pending commit_reroute."""
    cfg = _cfg(thread_id)
    since = len(agent.get_state(cfg).values.get("messages", []))
    d = {"type": decision}
    if decision == "reject" and message:
        d["message"] = message
    who = _label(agent)
    _trace("hitl_decision", who, decision=decision)
    result = agent.invoke(Command(resume={"decisions": [d]}), config=_traced(cfg, who))
    out = _reply(result, since)
    _trace_reply(who, out)
    return out


def new_thread():
    return str(uuid4())
