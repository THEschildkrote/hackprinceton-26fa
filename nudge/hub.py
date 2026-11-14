"""traveller status and event fan-out, shared by every channel (voice, imessage caretakers, mobile, web).

  register_traveller(tid, name=None) -> 6-digit pairing code (stable per traveller)
  attach_session(tid, nav_session_id, sid, goal, profile)
  publish(tid, event)            event: {type, text, place?, floor?, remaining_m?, phase?}
  subscribe(fn) -> unsubscribe   fn(tid, event) for every publish
  status(tid) -> {name, state, place, floor, goal, remaining_m, eta_min, last_update_ts, recent}
  pair(handle, code) -> tid|None, caretakers(tid) -> [handles], set_space(handle, space_id, agent_id)

state lives in one process (the api server); it is persisted to cache/hub.json so pairings survive restarts.
"""
import json
import math
import random
import threading
import time

from .mp import ROOT

PATH = ROOT / "cache" / "hub.json"
WALK_MPS = 0.9           # cautious cane-walking pace, for eta
STUCK_S = 180            # no progress for this long while walking -> "stuck"
STUCK_MIN_M = 0.5        # progress smaller than this doesn't count
STATES = ("idle", "walking", "arrived", "off_route", "unmapped", "help", "rerouting")

now = time.time          # tests swap this for a fake clock
_lock = threading.RLock()
_subs = []
_data = None


def _load():
    global _data
    if _data is None:
        try:
            _data = json.loads(PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _data = {}
        _data.setdefault("travellers", {})
        _data.setdefault("caretakers", {})
    return _data


def _save():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_data, indent=1, default=str), encoding="utf-8")
    tmp.replace(PATH)


def reset(path=None):
    """forget everything (tests)."""
    global _data, PATH
    with _lock:
        if path is not None:
            PATH = path
        _data = {"travellers": {}, "caretakers": {}}
        _subs.clear()


def _traveller(tid, create=True):
    d = _load()["travellers"]
    t = d.get(tid)
    if t is None and create:
        t = d[tid] = {"name": None, "code": None, "state": "idle", "place": None, "floor": None, "goal": None,
                      "remaining_m": None, "last_update_ts": None, "recent": [], "session": None, "sid": None,
                      "profile": None, "progress_m": None, "progress_ts": None, "stuck_sent": False}
    return t


def _new_code():
    taken = {t.get("code") for t in _load()["travellers"].values()}
    while True:
        c = f"{random.randint(0, 999999):06d}"
        if c not in taken:
            return c


def set_trip(traveller_id, trip):
    """a caretaker asks for a trip; the traveller's phone (on their back) picks it up and starts."""
    with _lock:
        t = _traveller(traveller_id, create=False)
        if t is None:
            return None
        t["trip"] = {**trip, "ts": now()}
        _save()
        return t["trip"]


def get_trip(traveller_id):
    with _lock:
        t = _traveller(traveller_id, create=False)
        return (t or {}).get("trip")


def ack_trip(traveller_id):
    with _lock:
        t = _traveller(traveller_id, create=False)
        if t is None:
            return False
        t["trip"] = None
        _save()
        return True


def traveller_for_code(code):
    with _lock:
        for tid, t in _load()["travellers"].items():
            if t.get("code") == str(code).strip():
                return tid, t.get("name")
    return None, None


def register_traveller(traveller_id, name=None):
    with _lock:
        t = _traveller(traveller_id)
        if name:
            t["name"] = name
        if not t.get("code"):
            t["code"] = _new_code()
        _save()
        return t["code"]


def attach_session(traveller_id, nav_session_id, sid, goal, profile):
    with _lock:
        t = _traveller(traveller_id)
        t.update(session=nav_session_id, sid=sid, goal=goal, profile=profile, state="walking",
                 progress_m=0.0, progress_ts=now(), stuck_sent=False, last_update_ts=now())
        _save()


def traveller_for_session(nav_session_id):
    with _lock:
        for tid, t in _load()["travellers"].items():
            if t.get("session") == nav_session_id:
                return tid
    return None


def _apply(t, e):
    """update status from one event; returns extra flags to put on the event."""
    flags = {}
    typ = e.get("type")
    if e.get("floor") is not None:
        if t.get("floor") is not None and e["floor"] != t["floor"]:
            flags["floor_changed"] = True
            flags["from_floor"] = t["floor"]
        t["floor"] = e["floor"]
    if e.get("place"):
        t["place"] = e["place"]
    if e.get("remaining_m") is not None:
        t["remaining_m"] = round(float(e["remaining_m"]), 1)
    if typ == "route":
        t["state"] = "walking"
    elif typ == "arrive":
        t["state"], t["remaining_m"] = "arrived", 0.0
    elif typ == "unmapped":
        t["state"] = "unmapped"
    elif typ == "remapped":
        t["state"] = "walking"
    elif typ == "reroute":
        t["state"] = "off_route" if "off the route" in (e.get("text") or "").lower() else "rerouting"
    elif typ in ("help", "stuck"):
        t["state"] = "help" if typ == "help" else t["state"]
    elif typ in ("leg", "now", "soon", "room", "door") and t["state"] in ("off_route", "rerouting"):
        t["state"] = "walking"
    elif typ == "ended":
        t["state"] = "idle"
    return flags


def publish(traveller_id, event):
    if not event:
        return
    with _lock:
        t = _traveller(traveller_id)
        e = {k: v for k, v in dict(event).items() if k != "vibrate"}
        e.setdefault("ts", now())
        e.update(_apply(t, e))
        t["last_update_ts"] = e["ts"]
        t["recent"] = (t["recent"] + [{k: e.get(k) for k in ("type", "text", "place", "floor", "ts")}])[-10:]
        _save()
        subs = list(_subs)
    for fn in subs:
        try:
            fn(traveller_id, e)
        except Exception:
            pass  # one broken channel must not break the others


def subscribe(fn):
    with _lock:
        _subs.append(fn)

    def unsubscribe():
        with _lock:
            if fn in _subs:
                _subs.remove(fn)
    return unsubscribe


def heartbeat(traveller_id, progress_m):
    """called on every position update; publishes "stuck" once if a walking traveller stops progressing."""
    stuck = None
    with _lock:
        t = _traveller(traveller_id)
        ts = now()
        if t["state"] not in ("walking", "off_route", "rerouting"):
            t["progress_ts"] = ts
            return
        if t.get("progress_m") is None or progress_m > t["progress_m"] + STUCK_MIN_M or progress_m < t["progress_m"] - 5:
            t.update(progress_m=progress_m, progress_ts=ts, stuck_sent=False)
            return
        if not t["stuck_sent"] and ts - (t.get("progress_ts") or ts) >= STUCK_S:
            t["stuck_sent"] = True
            mins = round((ts - t["progress_ts"]) / 60)
            stuck = {"type": "stuck", "text": f"No progress for about {mins} minutes."}
    if stuck:
        publish(traveller_id, stuck)


def status(traveller_id):
    with _lock:
        t = _traveller(traveller_id, create=False)
        if t is None:
            return None
        rem = t.get("remaining_m")
        eta = None if rem is None or t["state"] == "arrived" else max(1, math.ceil(rem / WALK_MPS / 60))
        return {"name": t.get("name"), "state": t["state"], "place": t.get("place"), "floor": t.get("floor"),
                "goal": t.get("goal"), "remaining_m": rem, "eta_min": eta, "last_update_ts": t.get("last_update_ts"),
                "recent": list(t["recent"]), "session": t.get("session"), "sid": t.get("sid")}


def pair(caretaker_handle, code):
    code = "".join(ch for ch in str(code) if ch.isdigit())
    with _lock:
        for tid, t in _load()["travellers"].items():
            if t.get("code") == code:
                c = _load()["caretakers"].setdefault(caretaker_handle, {"verbosity": "important"})
                c["traveller"] = tid
                _save()
                return tid
    return None


def caretakers(traveller_id):
    with _lock:
        return [h for h, c in _load()["caretakers"].items() if c.get("traveller") == traveller_id]


def caretaker(handle):
    with _lock:
        c = _load()["caretakers"].get(handle)
        return dict(c) if c else None


def set_space(caretaker_handle, space_id, agent_id):
    with _lock:
        c = _load()["caretakers"].setdefault(caretaker_handle, {"verbosity": "important"})
        c.update(space=space_id, agent=agent_id)
        _save()


def set_verbosity(caretaker_handle, level):
    with _lock:
        c = _load()["caretakers"].setdefault(caretaker_handle, {})
        c["verbosity"] = "all" if level == "all" else "important"
        _save()
