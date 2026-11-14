"""the maya demo: a pre-visit briefing, where the demo walk starts, the path to walk, what the agents found, and the
caretaker view (the exact texts sam would get on imessage, for the projector).

maya is blind and has an appointment at djf insurance, a real current costar tenant of 1255 s atlantic blvd,
a 1917 victorian office with no accessibility renovation on record. her brother sam follows along on imessage.
"""
import json
import math
import re
import threading
import time

from .instruct import meters
from .mp import ROOT

DEMO = {"MgGNVWsyXtH": {"traveller": "maya", "name": "Maya", "goal": "DJF Insurance", "caretaker": "Sam"}}
TARGET_OUTDOOR_M = 24.0
_STARTS = {}


def _site(sid):
    from . import handoff
    return handoff.load_site(sid)


def _facts(sid):
    try:
        from . import costar
        r = costar.lookup(sid)
        if r.get("error"):
            return {}
        f = costar.accessibility_facts(r["record"])
        return f.get("facts") or {}
    except Exception:
        return {}


def street_name(facts):
    addr = facts.get("address") or ""
    m = re.search(r"\d+\s+(?:[NSEW]\s+)?(.+?)(?:,|$)", addr)
    if not m:
        return None
    s = m.group(1)
    for a, b in (("Blvd", "Boulevard"), ("Ave", "Avenue"), ("St", "Street"), ("Rd", "Road"), ("Dr", "Drive")):
        s = re.sub(rf"\b{a}\b\.?", b, s)
    return s


def _lonlat(og, x, y):
    lon, lat = og.enu.to_lonlat(x, y)
    return lat, lon


def demo_start(sid):
    """a sidewalk point about 24 m (walking) from the entrance with no street to cross, so the walk is the real one."""
    if sid in _STARTS:
        return _STARTS[sid]
    from . import outdoor
    from .router import NoRoute, plan
    site = _site(sid)
    og, portal = site.og, site.best_portal()
    prof = outdoor.profile("blind")
    best = None
    for n in og.nodes.values():
        if n.id == portal["outdoor_node"]:
            continue
        try:
            r = plan(og, n.id, portal["outdoor_node"], prof)
        except NoRoute:
            continue
        tags = set().union(*(og.edge(a, b).tags for a, b in zip(r.nodes, r.nodes[1:])))
        if tags & {"crossing", "driveway_crossing", "roadway"}:
            continue
        score = abs(r.length - TARGET_OUTDOOR_M)
        if best is None or score < best[0]:
            best = (score, n, r.length)
    if best is None:
        return None
    lat, lon = _lonlat(og, best[1].x, best[1].y)
    _STARTS[sid] = {"lat": round(lat, 7), "lon": round(lon, 7), "outdoor_m": round(best[2], 1)}
    return _STARTS[sid]


def _route_stats(sid, goal, start, profile="blind"):
    from .handoff import plan_door_to_door
    site = _site(sid)
    p = plan_door_to_door((start["lat"], start["lon"]), sid, goal, profile, site=site)
    og, gi = site.og, site.gi
    ro = p["outdoor"]["route"]["nodes"]
    crossings = sum(1 for a, b in zip(ro, ro[1:]) if og.edge(a, b).tags & {"crossing"})
    ri = p["indoor"]["route"]["nodes"]
    doors = [d["width"] for a, b in zip(ri, ri[1:]) for d in (gi.edge(a, b).doors or []) if d.get("width")]
    floors = [m for m in p["indoor"]["maneuvers"] if m.get("type") == "floor"]
    return {"plan": p, "crossings": crossings, "door_widths": doors, "floor_changes": floors}


def briefing(sid, goal=None, start=None, profile="blind"):
    """about six short spoken sentences to hear before setting off. the dossier's briefing wins if it exists."""
    from .agents.tools import dossier_dir
    f = dossier_dir(sid) / "briefing.md"
    if f.exists():
        text = re.sub(r"[#*_>`-]+", " ", f.read_text(encoding="utf-8"))
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)) if len(s.strip()) > 2]
        return {"sentences": sents, "text": " ".join(sents), "source": "dossier"}
    facts = _facts(sid)
    goal = goal or (DEMO.get(sid) or {}).get("goal")
    from .buildings import label
    name = label(sid)
    out = []
    kind = (facts.get("property_type") or "building").lower()
    floors = facts.get("floors")
    yr = facts.get("year_built")
    lead = f"{name} is a" + (f" {floors}-floor" if floors else "") + f" {kind} building" + (f" from {yr}" if yr else "") + "."
    out.append(lead)
    if facts.get("pre_ada"):
        out.append("It has had no accessibility renovation on record, so expect steps and narrow doorways.")
    site = None
    try:
        site = _site(sid)
        portal = site.best_portal(profile)
        street = street_name(facts)
        side = f" on {street}" if street else ""
        if portal.get("step") is True:
            n = portal.get("risers")
            ramp = "a ramp" if portal.get("ramp") else "no ramp"
            out.append(f"The entrance is at the front{side}, up about {n} steps, with {ramp}." if n else
                       f"The entrance is at the front{side}, up a few steps, with {ramp}.")
        else:
            out.append(f"The entrance is at the front{side}.")
    except Exception:
        portal = None
    if site and goal:
        try:
            st = _route_stats(sid, goal, start or demo_start(sid), profile)
            p = st["plan"]
            cross = "no street to cross" if not st["crossings"] else \
                f"{st['crossings']} street crossing{'s' * (st['crossings'] != 1)}"
            out.append(f"From the sidewalk, it's about {meters(p['outdoor']['length_m'])} to the door, with {cross}.")
            inside = f"Inside, {goal} is about {meters(p['indoor']['length_m'])} from the door"
            fl = st["floor_changes"]
            if fl:
                via = fl[0].get("via", "stairs")
                up = "up" if (fl[0].get("rise_m") or 0) > 0 else "down"
                inside += f", {up} one flight of {via}" if via == "stairs" else f", one floor {up} by {via}"
            out.append(inside + ".")
            narrow = [w for w in st["door_widths"] if w < 0.9]
            if narrow:
                out.append(f"Some doorways are narrow; the narrowest on your way is about {min(narrow):.1f} meters. "
                           f"I'll tell you before each one.")
        except Exception:
            pass
    out.append("Say I need help at any time, and I'll let your caretaker know.")
    return {"sentences": out, "text": " ".join(out), "source": "generated", "facts": facts}


def _densify(pts, step=0.8):
    out = []
    for a, b in zip(pts, pts[1:]):
        if a[2] != b[2]:
            out.append(tuple(b))
            continue
        d = math.dist(a[:2], b[:2])
        n = max(1, int(d / step))
        for i in range(n):
            t = i / n
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2]))
    if pts:
        out.append(tuple(pts[-1]))
    return out


def walk_points(nav, og=None, step=0.8):
    """the rest of the live route as positions to feed back, from where the walker is now.
    outdoor (og given) comes back as lat/lon, indoor as model x, y, floor."""
    path = [tuple(p) for p in nav.state().get("path") or []]
    pts = _densify(path, step)
    if nav.pos and pts:
        x, y, fl = nav.pos[:3]
        same = [i for i, p in enumerate(pts) if p[2] == fl] or range(len(pts))
        i0 = min(same, key=lambda i: math.dist(pts[i][:2], (x, y)))
        pts = pts[i0:]
    if og is not None:
        return [{"lat": round(lat, 7), "lon": round(lon, 7)} for lat, lon in (_lonlat(og, p[0], p[1]) for p in pts)]
    return [{"x": round(p[0], 3), "y": round(p[1], 3), "floor": p[2]} for p in pts]


def behind_the_scenes(sid):
    """what the agents and data layers found, for the judges' card. plain words, no ids."""
    facts = _facts(sid)
    out = {"address": facts.get("address"), "year_built": facts.get("year_built"), "pre_ada": facts.get("pre_ada"),
           "floors": facts.get("floors"), "building_class": facts.get("building_class"),
           "property_type": facts.get("property_type"),
           "tenants": [t.get("name") for t in facts.get("tenants") or [] if t.get("name")]}
    try:
        site = _site(sid)
        p = site.best_portal()
        out["entrance"] = {"steps": p.get("risers") if p.get("step") else 0, "ramp": bool(p.get("ramp")),
                           "door_width_m": p.get("width_m"), "confirmed_from_imagery": bool(p.get("confirmed")),
                           "note": p.get("note"), "faces": p.get("faces")}
    except Exception:
        out["entrance"] = None
    try:
        doors = json.loads((ROOT / "cache" / "doorways" / f"{sid}.json").read_text(encoding="utf-8"))
        floors = doors.get("floors") or {}
        items = [d for v in (floors.values() if isinstance(floors, dict) else [floors]) for d in v]
        widths = [d.get("width") for d in items if d.get("width") and d.get("kind", "door") == "door"]
        out["doorways"] = {"count": len(widths), "narrowest_m": round(min(widths), 2) if widths else None}
    except Exception:
        out["doorways"] = None
    try:
        from . import places
        cat = places.load(sid)
        out["places"] = sorted({p["name"] for p in cat["places"] if p["kind"] in ("room", "tenant", "entrance")
                                and p["name"] != "Other"})
    except Exception:
        out["places"] = []
    out["facade_image"] = (ROOT / "cache" / f"demo_facade_{sid}.jpg").exists()
    return out


class CaretakerView:
    """the texts a caretaker would get on imessage, for an on-screen phone. same formatting and rate limits as the
    real photon notifier: one virtual caretaker per traveller, verbosity "important"."""

    def __init__(self):
        from . import hub, photon_bot
        self.hub = hub
        self.n = photon_bot.Notifier.__new__(photon_bot.Notifier)  # reuse its message() and limits, no queue
        self.n.send, self.n.sync, self.n.q = None, True, None
        self.n.last, self.n.count, self.n.recent, self.n.offroute, self.n.flagged = {}, {}, {}, {}, {}
        self.feeds = {}    # traveller -> [{"text", "urgent", "ts"}]
        self.watched = set()  # travellers someone has the view open for
        self.cv = threading.Condition()
        self.unsub = hub.subscribe(self.on_event)

    def on_event(self, tid, e):
        from .photon_bot import Voice
        st = self.hub.status(tid) or {}
        r = self.n.message(tid, e, "important", Voice(st.get("name"), "they"))
        if not r:
            return
        urgent, text = r
        handle = f"view:{tid}"
        ts = e.get("ts") or self.hub.now()
        n = self.n
        # the same gates as photon_bot.Notifier.on_event
        if ts - n.recent.get((handle, text), -1e9) < n.DEDUPE_S:
            return
        trip = (handle, st.get("session"))
        if not urgent:
            if ts - n.last.get(handle, -1e9) < n.GAP_S and e.get("type") not in ("arrive", "route"):
                return
            if n.count.get(trip, 0) >= n.PER_TRIP:
                return
            n.last[handle] = ts
            n.count[trip] = n.count.get(trip, 0) + 1
        n.recent[(handle, text)] = ts
        with self.cv:
            self.feeds.setdefault(tid, []).append({"text": text, "urgent": urgent, "ts": ts})
            self.cv.notify_all()

    def messages(self, tid, after=0):
        self.watched.add(tid)
        with self.cv:
            return list(self.feeds.get(tid, []))[after:]

    def reset(self, tid):
        with self.cv:
            self.feeds[tid] = []
        handle = f"view:{tid}"
        n = self.n
        n.last.pop(handle, None)
        for k in [k for k in n.recent if k[0] == handle]:
            n.recent.pop(k, None)
        for k in [k for k in n.count if k[0] == handle]:
            n.count.pop(k, None)
        n.flagged.pop(tid, None)
        n.offroute.pop(tid, None)

    def wait(self, tid, after, timeout=15.0):
        self.watched.add(tid)
        end = time.monotonic() + timeout
        with self.cv:
            while len(self.feeds.get(tid, [])) <= after:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cv.wait(left)
            return list(self.feeds.get(tid, []))[after:]
