"""debug api + static web. run: python -m uvicorn nudge.server:app --port 8765"""
import json
import os
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import walkgraph
from .instruct import maneuvers
from .mp import ROOT
from .navigator import Navigator, vibration
from .router import PROFILES, NoRoute, plan

app = FastAPI(title="nudge debug")
_graphs, _sessions = {}, {}
sessions = _sessions  # session id -> Navigator; the device router reads it via get_navigator
_session_sid = {}     # session id -> building sid


def get_navigator(session_id):
    """live Navigator for a session id, or None. pass this to the device router."""
    return _sessions.get(session_id)


def _load_model(sid):
    try:
        from .fetch import load
    except ImportError:
        load = None
    if load:
        return load(sid)
    if sid == "synthetic":
        from tests.synthetic import building
        return building()
    raise HTTPException(404, f"no loader for {sid}")


def graph_for(sid):
    # annotated graph: once a dossier exists its hazards and landmarks show up as tags
    try:
        from .agents.tools import graph
        return graph(sid)
    except ImportError:
        pass
    except Exception as ex:
        raise HTTPException(502, f"could not load {sid}: {ex}")
    if sid not in _graphs:
        try:
            _graphs[sid] = walkgraph.build(_load_model(sid))
        except HTTPException:
            raise
        except Exception as ex:
            raise HTTPException(502, f"could not load {sid}: {ex}")
    return _graphs[sid]


def _events(evs):
    for e in evs:
        e["vibrate"] = vibration(e.get("cue"))
    return evs


class StartReq(BaseModel):
    sid: str
    to: str
    profile: str = "blind"
    x: float
    y: float
    floor: str
    heading: float | None = None


class UpdateReq(BaseModel):
    session: str
    x: float
    y: float
    floor: str
    heading: float | None = None


class BlockReq(BaseModel):
    session: str
    x: float
    y: float
    floor: str
    a: str
    b: str | None = None
    heading: float | None = None


@app.get("/api/profiles")
def profiles():
    return {k: p.describe() for k, p in PROFILES.items()}


@app.get("/api/graph/{sid}")
def get_graph(sid: str, profile: str | None = None):
    g = graph_for(sid)
    d = g.to_dict()
    d["sid"] = sid
    prof = PROFILES.get(profile or "")
    if prof:  # mark edges this profile can never use
        for e in d["edges"]:
            e["avoided"] = bool(set(e["tags"]) & prof.avoid)
    return d


@app.post("/api/route")
def route(body: dict):
    # accept "from" (reserved in python) or "start"
    sid, to = body.get("sid"), body.get("to")
    start = body.get("from") or body.get("start")
    profile = body.get("profile") or "blind"
    if not (sid and to and start):
        raise HTTPException(400, "need sid, from, to")
    g = graph_for(sid)
    try:
        r = plan(g, start, to, profile)
    except NoRoute as ex:
        raise HTTPException(404, str(ex))
    return {"route": r.to_dict(g), "maneuvers": maneuvers(g, r)}


def _state(nav):
    st = nav.state()
    r = getattr(nav, "route", None)
    cum, prog = getattr(nav, "cum", None), getattr(nav, "progress", 0.0)
    if r and cum:  # the edge the walker is on or about to take
        for i, (a, b) in enumerate(zip(r.nodes, r.nodes[1:])):
            if cum[i + 1] > prog + 0.05:
                st["next_edge"] = [a, b]
                break
    return st


def _device(nav):
    """what the haptic band gets right now: the packet and its ascii line (< 20 bytes)."""
    from .device import encode_live, live_packet
    try:
        pk = live_packet(nav)
        return {"packet": pk, "line": encode_live(pk)}
    except Exception as ex:  # no route yet, etc.
        return {"packet": None, "line": None, "error": str(ex)}


def _nav(session):
    s = _sessions.get(session)
    if not s:
        raise HTTPException(404, "unknown session")
    return s


@app.post("/api/nav/start")
def nav_start(req: StartReq):
    g = graph_for(req.sid)
    nav = Navigator(g, req.to, req.profile)
    sid = uuid.uuid4().hex[:10]
    _sessions[sid] = nav
    _session_sid[sid] = req.sid
    ev = nav.start(req.x, req.y, req.floor, req.heading)
    return {"session": sid, "events": _events(ev), "state": _state(nav), "device": _device(nav)}


@app.post("/api/nav/update")
def nav_update(req: UpdateReq):
    nav = _nav(req.session)
    ev = nav.update(req.x, req.y, req.floor, req.heading)
    return {"events": _events(ev), "state": _state(nav), "device": _device(nav)}


@app.post("/api/nav/block")
def nav_block(req: BlockReq):
    nav = _nav(req.session)
    ev = nav.block(req.x, req.y, req.floor, req.a, req.b, req.heading)
    return {"events": _events(ev), "state": _state(nav), "device": _device(nav)}


@app.post("/api/reload/{sid}")
def reload(sid: str):
    _graphs.pop(sid, None)
    try:
        from .agents.tools import _graph_cached
        _graph_cached.cache_clear()
    except ImportError:
        pass
    return {"ok": True}


# dossier runs: the deep agent working ahead of the visitor, streamed as structured events

_runs = {}  # sid -> {"id", "status", "events", "error", "mode"}
_runs_lock = threading.Lock()
DOSSIER_FILES = ("annotations.json", "briefing.md", "audit.md", "site.md")


def _dir(sid):
    from .agents.tools import dossier_dir
    return dossier_dir(sid)


@app.get("/api/dossier/{sid}")
def get_dossier(sid: str):
    from .agents.tools import validate
    d = _dir(sid)
    out = {"sid": sid, "files": sorted(str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*") if p.is_file())}
    for name in DOSSIER_FILES:
        p = d / name
        key = name.replace(".", "_")
        if not p.exists():
            out[key] = None
        elif name.endswith(".json"):
            try:
                out[key] = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as ex:
                out[key] = {"error": f"invalid json: {ex}"}
        else:
            out[key] = p.read_text(encoding="utf-8")
    try:
        out["validation"] = validate(sid)
    except Exception as ex:
        out["validation"] = {"ok": False, "errors": [str(ex)]}
    run = _runs.get(sid)
    out["run"] = run and {k: run[k] for k in ("id", "status", "mode", "error")} | {"events": len(run["events"])}
    return out


@app.get("/api/dossier/{sid}/events")
def dossier_events(sid: str, after: int = 0):
    run = _runs.get(sid)
    if not run:
        return {"id": None, "status": "idle", "events": [], "next": 0}
    ev = run["events"][after:]
    return {"id": run["id"], "status": run["status"], "mode": run["mode"], "error": run["error"],
            "events": ev, "next": after + len(ev)}


def _has_llm_key():
    from .agents import llm
    return llm.has_credentials(llm.spec()) and llm.has_credentials(llm.spec("vision"))


def _start_run(sid, mode, target):
    with _runs_lock:
        cur = _runs.get(sid)
        if cur and cur["status"] == "running":
            raise HTTPException(409, f"a dossier run is already going for {sid} ({cur['id']})")
        run = {"id": uuid.uuid4().hex[:10], "status": "running", "events": [], "error": None, "mode": mode}
        _runs[sid] = run

    def go():
        try:
            target(sid, run)
            run["status"] = "done"
            run["events"].append({"kind": "done", "who": "lead", "text": "dossier finished"})
        except Exception as ex:
            run["status"], run["error"] = "error", f"{type(ex).__name__}: {ex}"
            run["events"].append({"kind": "error", "who": "lead", "text": run["error"]})
        finally:
            _clear_graph_cache()
    threading.Thread(target=go, daemon=True).start()
    return {"id": run["id"], "status": "running", "mode": mode}


def _clear_graph_cache():
    try:
        from .agents.tools import _graph_cached
        _graph_cached.cache_clear()
    except ImportError:
        pass


@app.post("/api/dossier/{sid}/run")
def dossier_run(sid: str):
    if not _has_llm_key():
        raise HTTPException(400, "no model credentials: put OPENROUTER_API_KEY (free models) or ANTHROPIC_API_KEY in .env. "
                                 "Use the demo run to see the agent offline.")
    return _start_run(sid, "live", lambda s, run: _stream_agent(s, run, model=None))


@app.post("/api/dossier/{sid}/demo")
def dossier_demo(sid: str):
    if sid != "synthetic":
        raise HTTPException(400, "the scripted demo only knows the synthetic building")
    return _start_run(sid, "demo", _demo)


def _stream_agent(sid, run, model=None):
    """drive the real dossier agent and turn its stream into ui events."""
    from .agents.dossier import create_dossier_agent, kickoff
    agent = create_dossier_agent(sid, model)
    config = {"configurable": {"thread_id": run["id"]}, "recursion_limit": 400}
    active = []  # subagents currently running, innermost last
    emit = run["events"].append
    emit({"kind": "start", "who": "lead", "text": f"dossier lead starts on {sid}"})
    for ns, update in agent.stream({"messages": [{"role": "user", "content": kickoff(sid)}]}, config=config,
                                   stream_mode="updates", subgraphs=True):
        who = active[-1] if ns and active else "lead"
        for node, val in (update or {}).items():
            if not isinstance(val, dict):
                continue
            for msg in val.get("messages") or []:
                for call in getattr(msg, "tool_calls", None) or []:
                    emit(_call_event(who, call))
                    if call["name"] == "task" and not ns:
                        active.append(call["args"].get("subagent_type", "subagent"))
                if getattr(msg, "type", "") == "tool" and getattr(msg, "name", "") == "task" and not ns:
                    done = active.pop() if active else "subagent"
                    emit({"kind": "returned", "who": done, "text": _text(msg)[:400]})
                elif not ns and getattr(msg, "type", "") == "ai" and not getattr(msg, "tool_calls", None) and _text(msg):
                    # subagent final words arrive again as the task result; only the lead speaks here
                    emit({"kind": "say", "who": who, "text": _text(msg)[:600]})


def _text(msg):
    c = getattr(msg, "content", "")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def _call_event(who, call):
    name, args = call["name"], dict(call.get("args") or {})
    if name == "write_todos":
        todos = [{"content": t.get("content"), "status": t.get("status")} for t in args.get("todos", [])]
        done = sum(t["status"] == "completed" for t in todos)
        return {"kind": "plan", "who": who, "todos": todos, "text": f"plan {done}/{len(todos)}"}
    if name == "task":
        return {"kind": "delegate", "who": who, "subagent": args.get("subagent_type"),
                "text": str(args.get("description", ""))[:300]}
    if name in ("write_file", "edit_file"):
        return {"kind": "write", "who": who, "path": args.get("file_path"), "text": f"{name} {args.get('file_path')}"}
    if name == "look":
        return {"kind": "look", "who": who, "pano": args.get("pano_id"), "yaw": args.get("yaw_deg"),
                "pitch": args.get("pitch_deg", 0), "text": f"look from {args.get('pano_id')} at {args.get('yaw_deg')} deg"}
    args.pop("sid", None)
    return {"kind": "tool", "who": who, "name": name, "args": args,
            "text": f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})"}


def _demo(sid, run):
    """scripted offline run: a fake chat model drives the real dossier agent, real tools, real files."""
    import shutil
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    d = _dir(sid)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    _clear_graph_cache()
    script = _demo_script(sid)

    class Scripted(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, *a, **kw):
            time.sleep(DEMO_PACE_S)
            return super()._generate(*a, **kw)

    _stream_agent(sid, run, model=Scripted(messages=iter(script)))


DEMO_PACE_S = 0.6


def _demo_script(sid):
    """the turns a real run would take on the synthetic building, built from the real tool outputs."""
    from langchain_core.messages import AIMessage

    from .agents.tools import audit
    from .perceive import glass_suspects, overhang_candidates

    from .agents.tools import graph
    from .geom import project

    o = overhang_candidates(sid, "f1")[0]
    gl = glass_suspects(sid, "f1")[0]
    gx, gy = (gl["a"][0] + gl["b"][0]) / 2, (gl["a"][1] + gl["b"][1]) / 2
    g0 = graph(sid, annotated=False)

    def reach(x, y):
        # radius that just reaches the nearest walked edge, so the router sees the hazard
        d = min(project((x, y), g0.nodes[e.a].xy, g0.nodes[e.b].xy)[2] for e in g0.edges()
                if "vertical" not in e.tags and g0.nodes[e.a].floor == "f1")
        return round(min(1.5, max(0.5, d + 0.15)), 2)
    hazards = [
        {"x": round(o["x"], 2), "y": round(o["y"], 2), "floor": "f1", "radius": reach(o["x"], o["y"]), "kind": "protruding_object",
         "note": "wall sconce at head height; a cane will pass under it", "z_low_m": round(o["z_low_m"], 2),
         "z_high_m": round(o["z_high_m"], 2), "confidence": 0.9,
         "evidence": {"pano_id": o["nearest_pano"], "bearing_deg": o["bearing_deg"], "candidate_id": o["id"]}},
        {"x": round(gx, 2), "y": round(gy, 2), "floor": "f1", "radius": reach(gx, gy), "kind": "glass_wall",
         "note": "glass wall with no frame; keep to the opposite side", "confidence": 0.8,
         "evidence": {"pano_id": gl["nearest_pano"], "bearing_deg": gl["bearing_deg"], "candidate_id": gl["id"]}},
    ]
    turns = audit(sid, "blind")["turn_points_without_landmarks"]
    by_room = {
        "Lobby": ("hard tile underfoot; footsteps echo in the open lobby", "underfoot"),
        "Office": ("carpet starts at the office doorway", "underfoot"),
        "Kitchen": ("refrigerator hum along the far wall", "sound"),
        "Elevator": ("call button panel at waist height beside the opening", "touch"),
        "Stairs": ("handrail begins at the first step", "touch"),
        "Conference Room": ("carpet and a steady air vent hum overhead", "air"),
    }
    nodes = {}
    for t in turns:
        lm = by_room.get(t["room"])
        if lm and t["floor"] == "f1" and t["pano_id"] not in nodes:
            nodes[t["pano_id"]] = {"landmarks": [{"text": lm[0], "sense": lm[1], "pano_id": t["pano_id"]}]}
        if len(nodes) >= 4:
            break
    f1_turns = list(nodes)
    ann_text = json.dumps({"nodes": {}, "hazards": hazards}, indent=1)
    nodes_text = json.dumps(nodes, indent=1)
    todos = ["Survey the approach (site-surveyor)", "Scout hazards floor by floor (hazard-scout)",
             "Merge hazards and validate", "Write landmarks at turn points (landmark-writer)",
             "Audit routes for blind and wheelchair", "Write audit and briefing"]

    def plan(k):
        return {"todos": [{"content": c, "status": "completed" if i < k else "in_progress" if i == k else "pending"}
                          for i, c in enumerate(todos)]}

    def call(name, args, text=""):
        call.n += 1
        return AIMessage(content=text, tool_calls=[{"id": f"d{call.n}", "name": name, "args": args}])
    call.n = 0

    site = ("# Site\n\n- Entrance: the lobby, at grade, no steps found in the scan.\n"
            "- Approach: surface lot with 2 accessible stalls (CoStar).\n"
            "- Built 1984, before the 1993 ADA new-construction date: expect older fixtures.\n"
            "- Directory: Acme Dental, Office suite on floor 1.\n")
    hz_f1 = json.dumps({"floor": "f1", "hazards": hazards, "rejected": []}, indent=1)
    lm_f1 = json.dumps({"floor": "f1", "landmarks": nodes}, indent=1)
    audit_md = ("# Audit\n\nEvery labelled room is reachable for blind and wheelchair travellers. "
                "Two hazards on floor 1 are now avoided or announced. Floor 2 had no candidates.\n")
    briefing = ("This is a two-floor office building, entered through the lobby at street level. "
                "The floor is hard tile there, and your footsteps will echo. "
                "One thing to know: a wall sconce sticks out at head height near the lobby, above where a cane reaches; "
                "I'll warn you before you pass it. There is also a frameless glass wall on the first floor. "
                "The office has carpet from its doorway. "
                "To go upstairs, the elevator is beside the office, with the call buttons at waist height. "
                "The stairs have a handrail from the first step.")

    return [
        call("write_todos", plan(0), "Planning the dossier."),
        call("building_overview", {"sid": sid}),
        call("task", {"subagent_type": "site-surveyor", "description": f"Survey the approach to {sid}: entrances, parking, building facts. Write /dossier/site.md."}),
        call("aerial_view", {"sid": sid, "half_m": 60}),
        call("costar_record", {"sid": sid}),
        call("write_file", {"file_path": "/dossier/site.md", "content": site}),
        AIMessage(content="Wrote /dossier/site.md. Entrance is the lobby at grade; built 1984."),
        call("write_todos", plan(1)),
        call("task", {"subagent_type": "hazard-scout", "description": f"Scout floor f1 of {sid} for hazards. Confirm every candidate with look(). Write /dossier/hazards/f1.json."}),
        call("overhang_candidates", {"sid": sid, "floor": "f1"}),
        call("look", {"sid": sid, "pano_id": o["nearest_pano"], "yaw_deg": o["bearing_deg"]}, "Checking the overhang candidate."),
        call("look", {"sid": sid, "pano_id": o["nearest_pano"], "yaw_deg": o["bearing_deg"], "pitch_deg": 20, "fov_deg": 60}),
        call("glass_suspects", {"sid": sid, "floor": "f1"}),
        call("look", {"sid": sid, "pano_id": gl["nearest_pano"], "yaw_deg": gl["bearing_deg"]}, "Checking the glass suspect."),
        call("write_file", {"file_path": "/dossier/hazards/f1.json", "content": hz_f1}),
        AIMessage(content="Floor f1: confirmed a wall sconce at head height and a frameless glass wall."),
        call("task", {"subagent_type": "hazard-scout", "description": f"Scout floor f2 of {sid} for hazards. Write /dossier/hazards/f2.json."}),
        call("overhang_candidates", {"sid": sid, "floor": "f2"}),
        call("glass_suspects", {"sid": sid, "floor": "f2"}),
        call("write_file", {"file_path": "/dossier/hazards/f2.json", "content": json.dumps({"floor": "f2", "hazards": [], "rejected": []})}),
        AIMessage(content="Floor f2: no candidates."),
        call("write_todos", plan(2)),
        call("write_file", {"file_path": "/dossier/annotations.json", "content": ann_text}),
        call("validate_annotations", {"sid": sid}),
        call("audit_routes", {"sid": sid, "profile": "blind"}),
        call("write_todos", plan(3)),
        call("task", {"subagent_type": "landmark-writer", "description": f"Write non-visual landmarks for {sid} floor f1 at turn points {', '.join(f1_turns)}. Write /dossier/landmarks/f1.json."}),
        *[m for p in f1_turns[:2] for m in (call("look", {"sid": sid, "pano_id": p, "yaw_deg": 0}),
                                            call("look", {"sid": sid, "pano_id": p, "yaw_deg": 0, "pitch_deg": -60}))],
        call("write_file", {"file_path": "/dossier/landmarks/f1.json", "content": lm_f1}),
        AIMessage(content=f"Wrote landmarks for {len(nodes)} turn points on f1."),
        call("edit_file", {"file_path": "/dossier/annotations.json", "old_string": '"nodes": {}',
                           "new_string": '"nodes": ' + nodes_text}),
        call("validate_annotations", {"sid": sid}),
        call("write_todos", plan(4)),
        call("audit_routes", {"sid": sid, "profile": "blind"}),
        call("audit_routes", {"sid": sid, "profile": "wheelchair"}),
        call("write_todos", plan(5)),
        call("write_file", {"file_path": "/dossier/audit.md", "content": audit_md}),
        call("write_file", {"file_path": "/dossier/briefing.md", "content": briefing}),
        call("write_todos", {"todos": [{"content": c, "status": "completed"} for c in todos]}),
        AIMessage(content="Dossier done: 2 hazards confirmed, landmarks at 4 turn points, every room reachable."),
    ]


# tour demo: config, first-person views, exceptions

@app.get("/api/config")
def config():
    from .mp import secret
    return {"matterport_sdk_key": secret("MATTERPORT_SDK_KEY", required=False) or None}


@app.get("/api/look/{sid}")
def look_view(sid: str, pano: str | None = None, yaw: float = 0.0, pitch: float = 0.0, fov: float = 90.0,
              size: int = 640, x: float | None = None, y: float | None = None, floor: str | None = None):
    """jpeg view from a pano. give pano, or x/y/floor to snap to the nearest walkable one."""
    from . import perceive
    from .navigator import snap
    g = graph_for(sid)
    if not pano:
        if x is None or y is None:
            raise HTTPException(400, "need pano or x, y, floor")
        pano, _ = snap(g, x, y, floor or next(iter(g.nodes.values())).floor)
    size = max(128, min(1024, size))
    try:
        jpeg, meta = perceive.look(sid, pano, yaw, pitch, fov, size)
    except Exception as ex:
        raise HTTPException(502, f"could not render {pano}: {ex}")
    return Response(jpeg, media_type="image/jpeg",
                    headers={"X-Pano": str(meta.get("pano_id", pano)), "Cache-Control": "max-age=300"})


class ExceptionReq(BaseModel):
    session: str
    text: str


class DecideReq(BaseModel):
    session: str
    decision: str = "approve"


_exc = {}  # session -> {"agent", "thread"}


def _here(nav):
    if nav.pos:
        return nav.pos
    n = nav.g.nodes[nav.route.nodes[0]]
    return n.x, n.y, n.floor


def _fallback_block(nav, text):
    """no model available: block the thing the user named on the route ahead, deterministically."""
    g, r = nav.g, nav.route
    ahead = [(a, b) for i, (a, b) in enumerate(zip(r.nodes, r.nodes[1:])) if nav.cum[i + 1] > nav.progress + 0.05]
    low = text.lower()
    target, what = None, "the way ahead"
    for words, tag in ((("elevator", "lift"), "elevator"), (("stair",), "stairs")):
        if any(w in low for w in words):
            for a, b in ahead:
                if tag in g.edge(a, b).tags or tag in g.nodes[b].tags:
                    target, what = (a if tag in g.nodes[a].tags else b, None), f"the {tag}"
                    break
            break
    if target is None and ("door" in low or "locked" in low or "blocked" in low):
        for a, b in ahead:
            if "door" in g.edge(a, b).tags:
                target, what = (a, b), "that doorway"
                break
    if target is None and ahead:
        target = ahead[0]
    if target is None:
        return [], ["You're almost there; there's nothing ahead to avoid."]
    x, y, fl = _here(nav)
    ev = nav.block(x, y, fl, target[0], target[1], nav.heading,
                   why=f"Okay, I'll avoid {what}.")
    return ev, [e["text"] for e in ev if e.get("text")]


@app.post("/api/nav/exception")
def nav_exception(req: ExceptionReq):
    nav = _nav(req.session)
    if not nav.route:
        raise HTTPException(409, "no active route")
    from .agents import llm
    if llm.has_credentials(llm.spec()):
        from .agents.exception import create_exception_agent, handle_event, new_thread
        ctx = _exc.get(req.session)
        if not ctx:
            ctx = _exc[req.session] = {"agent": create_exception_agent(nav, nav.g, f"travels as: {nav.profile}"),
                                       "thread": new_thread()}
        try:
            r = handle_event(ctx["agent"], ctx["thread"], req.text)
        except Exception as ex:
            ev, say = _fallback_block(nav, req.text)
            return {"mode": "fallback", "say": [f"The assistant isn't reachable ({type(ex).__name__})."] + say,
                    "pending": None, "events": _events(ev), "state": _state(nav), "device": _device(nav)}
        return {"mode": "agent", "say": r.get("say") or [], "pending": r.get("pending"), "events": [],
                "state": _state(nav), "device": _device(nav)}
    ev, say = _fallback_block(nav, req.text)
    return {"mode": "fallback", "say": say, "pending": None, "events": _events(ev), "state": _state(nav),
            "device": _device(nav)}


@app.post("/api/nav/exception/decide")
def nav_exception_decide(req: DecideReq):
    nav = _nav(req.session)
    ctx = _exc.get(req.session)
    if not ctx:
        raise HTTPException(409, "nothing pending for this session")
    from .agents.exception import approve
    decision = "approve" if req.decision.lower().startswith(("a", "y")) else "reject"
    r = approve(ctx["agent"], ctx["thread"], decision)
    return {"mode": "agent", "say": r.get("say") or [], "pending": r.get("pending"), "state": _state(nav),
            "device": _device(nav)}


@app.get("/")
def index():
    return RedirectResponse("/debug.html")


app.mount("/", StaticFiles(directory=ROOT / "web"), name="web")
