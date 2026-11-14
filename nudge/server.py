"""debug api + static web. run: python -m uvicorn nudge.server:app --port 8765"""
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import walkgraph
from .instruct import maneuvers
from .mp import ROOT
from .navigator import Navigator, vibration
from .router import PROFILES, NoRoute, plan

app = FastAPI(title="nudge debug")
_graphs, _sessions = {}, {}


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
    ev = nav.start(req.x, req.y, req.floor, req.heading)
    return {"session": sid, "events": _events(ev), "state": _state(nav)}


@app.post("/api/nav/update")
def nav_update(req: UpdateReq):
    nav = _nav(req.session)
    ev = nav.update(req.x, req.y, req.floor, req.heading)
    return {"events": _events(ev), "state": _state(nav)}


@app.post("/api/nav/block")
def nav_block(req: BlockReq):
    nav = _nav(req.session)
    ev = nav.block(req.x, req.y, req.floor, req.a, req.b, req.heading)
    return {"events": _events(ev), "state": _state(nav)}


@app.post("/api/reload/{sid}")
def reload(sid: str):
    _graphs.pop(sid, None)
    return {"ok": True}


@app.get("/")
def index():
    return RedirectResponse("/debug.html")


app.mount("/", StaticFiles(directory=ROOT / "web"), name="web")
