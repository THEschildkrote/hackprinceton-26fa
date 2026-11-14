"""stalls: a traveller who stops, backs up or shuffles in place mid-route probably met something.

one traveller: the tracker's own numbers say it (no steps for a while, progress going backwards, lots of
steps and no progress), so Isaac asks "is something in the way?" and the answer goes to the exception agent.

several travellers: a stretch where two different people stalled within the last 20 minutes, or where one
said something was in the way, gets a temporary routing penalty for everyone (the "stall" tag). nobody had
to report anything. a traveller saying "keep going" takes their stall back out of the count.

    detect(session, tracker, steps, now) -> {"kind", "edge", "where", ...} or None, at most once per episode
    record(sid, stall, traveller) / resolve(sid, traveller, outcome) / active(sid) / apply(graph, sid)
"""
import json
import threading
import time
from collections import deque

from .mp import ROOT

STOP_S = 8.0           # standing still this long, mid-route and not waiting for a lift, is a stall
BACK_M = 1.5           # progress going backwards by this much within the window
SHUFFLE_STEPS = 10     # this many steps within SHUFFLE_S ...
SHUFFLE_S = 12.0
SHUFFLE_M = 1.5        # ... for less than this much progress
WINDOW_S = 20.0
COOLDOWN_S = 45.0      # never ask twice in quick succession
COOLDOWN_M = 4.0       # or twice about the same spot
EDGE_GAP_M = 3.0       # no asking right at the start or the end of a trip

TTL_S = 20 * 60        # a stall counts toward a penalty for this long
MIN_TRAVELLERS = 2     # distinct people stalling on one stretch before it is penalised
TAG = "stall"

QUESTION = "Is something in the way? Tell me what it is, or say keep going."

_FILE = ROOT / "cache" / "stalls.json"
_lock = threading.Lock()
_watch = {}            # session -> per-trip detector state
_records = None        # [{id, sid, edge, x, y, floor, t, traveller, kind, outcome}]
_tagged = {}           # sid -> {edge}: what apply() put the tag on, so it can take it off again


# --- one traveller ----------------------------------------------------------------------------------

def _edge_ahead(tr, ahead_m=1.0):
    """the route edge just ahead of the traveller, as a sorted (a, b), or None."""
    nav = tr.nav
    if not nav.route or not nav.path:
        return None
    _, _, _, j = tr._point(tr.along + ahead_m)
    i = nav.path[j][4]
    nodes = nav.route.nodes
    if i < 0 or i + 1 >= len(nodes):
        return None
    return tuple(sorted((nodes[i], nodes[i + 1])))


def _where(g, edge):
    rooms = [g.nodes[n].room_label for n in edge if g.nodes[n].room_label and g.nodes[n].room_label != "Void"]
    return f"in the {rooms[0]}" if rooms else "on the route"


def detect(session, tr, steps, now=None):
    """call once per tracker update. returns a stall the first time one shows, else None."""
    now = time.monotonic() if now is None else now
    nav = tr.nav
    st = _watch.get(session)
    if st is None or st["route"] is not nav.route:
        # a new route (a reroute, often after a stall) starts a fresh window; the cooldown carries over
        asked_t = st["asked_t"] if st else -1e9
        st = _watch[session] = {"route": nav.route, "hist": deque(), "last_step": None, "asked_t": asked_t,
                                "asked_along": -1e9, "episode": False, "walked": False}
    hist = st["hist"]
    hist.append((now, max(0, int(steps)), tr.along))
    while hist and now - hist[0][0] > WINDOW_S:
        hist.popleft()
    if steps > 0:
        st["last_step"], st["episode"] = now, False
        if tr.along > 1.0:
            st["walked"] = True
    remaining = nav.route.length - tr.along if nav.route else 0.0
    if (nav.route is None or nav.done or tr.pending is not None or tr.mode != "route" or not st["walked"]
            or tr.along < EDGE_GAP_M or remaining < EDGE_GAP_M or st["episode"]
            or now - st["asked_t"] < COOLDOWN_S or abs(tr.along - st["asked_along"]) < COOLDOWN_M):
        return None
    kind = None
    if st["last_step"] is not None and now - st["last_step"] >= STOP_S:
        kind = "stop"
    elif max(a for _, _, a in hist) - tr.along >= BACK_M:
        kind = "backup"
    else:
        recent = [(t, n, a) for t, n, a in hist if now - t <= SHUFFLE_S]
        if recent and sum(n for _, n, _ in recent) >= SHUFFLE_STEPS and tr.along - recent[0][2] < SHUFFLE_M:
            kind = "shuffle"
    if kind is None:
        return None
    edge = _edge_ahead(tr)
    if edge is None:
        return None
    st.update(asked_t=now, asked_along=tr.along, episode=True)
    x, y, floor = tr.pos
    return {"kind": kind, "edge": edge, "x": round(x, 2), "y": round(y, 2), "floor": floor,
            "where": _where(nav.g, edge), "along": round(tr.along, 1)}


def forget(session):
    _watch.pop(session, None)


# --- everyone: the shared record --------------------------------------------------------------------

def _load():
    global _records
    if _records is None:
        try:
            _records = json.loads(_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _records = []
        for r in _records:
            r["edge"] = tuple(r["edge"])
    return _records


def _save():
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(_records), encoding="utf-8")
    except OSError:
        pass  # the record is a convenience; losing it on a full disk must not break a walk


def record(sid, stall, traveller):
    """a stall seen on `sid`. returns the stored record."""
    with _lock:
        recs = _load()
        rec = {"id": f"{int(time.time() * 1000)}", "sid": sid, "edge": tuple(stall["edge"]), "x": stall["x"],
               "y": stall["y"], "floor": stall["floor"], "t": time.time(), "traveller": traveller,
               "kind": stall["kind"], "outcome": None}
        recs.append(rec)
        cut = time.time() - TTL_S * 3
        recs[:] = [r for r in recs if r["t"] >= cut]
        _save()
        return rec


def resolve(sid, traveller, outcome):
    """the traveller answered: "obstacle" (something was there) or "clear" (nothing, carry on)."""
    with _lock:
        for r in reversed(_load()):
            if r["sid"] == sid and r["traveller"] == traveller and r["outcome"] is None:
                r["outcome"] = outcome
                _save()
                return r
    return None


def active(sid, now=None):
    """stretches of `sid` with live stalls: [{edge, x, y, floor, travellers, obstacle, penalised, expires_s}]."""
    now = time.time() if now is None else now
    by = {}
    with _lock:
        for r in _load():
            if r["sid"] != sid or now - r["t"] > TTL_S or r["outcome"] == "clear":
                continue
            s = by.setdefault(r["edge"], {"edge": list(r["edge"]), "x": r["x"], "y": r["y"], "floor": r["floor"],
                                          "travellers": set(), "obstacle": False, "last": 0.0})
            s["travellers"].add(r["traveller"])
            s["obstacle"] |= r["outcome"] == "obstacle"
            s["last"] = max(s["last"], r["t"])
    out = []
    for s in by.values():
        n = len(s["travellers"])
        out.append({"edge": s["edge"], "x": s["x"], "y": s["y"], "floor": s["floor"], "travellers": n,
                    "obstacle": s["obstacle"], "penalised": s["obstacle"] or n >= MIN_TRAVELLERS,
                    "expires_s": max(0, round(TTL_S - (now - s["last"])))})
    return out


def apply(g, sid, now=None):
    """put the stall tag on penalised stretches of this graph and take it off expired ones. cheap; call it
    before any route is planned. returns the penalised edges."""
    want = {tuple(s["edge"]) for s in active(sid, now) if s["penalised"]}
    had = _tagged.get(sid, set())
    for a, b in had - want:
        e = g.edge(a, b)
        if e is not None:
            e.tags.discard(TAG)
    for a, b in want - had:
        e = g.edge(a, b)
        if e is not None:
            e.tags.add(TAG)
    _tagged[sid] = want
    return want


def reset():
    """forget everything (tests)."""
    global _records
    with _lock:
        _records = []
        _watch.clear()
        _tagged.clear()
