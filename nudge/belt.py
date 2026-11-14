"""the belt: a raspberry pi that runs its own on-body runtime and taps the wearer.

everything upstream of the steps is ours: the building, the route, the words. the belt does the walking part
itself. the phone's steps and heading go straight to the pi, the pi keeps the position and fires the taps. we
hand it the route and read back where the wearer is.

    NUDGE_BELT_URL=http://172.20.10.2:8000 python -m uvicorn nudge.server:app --port 8765

- nav start       -> POST /route                  the route's points, every floor, as /api/route returns them
- nav calibrate   -> POST /calibrate, /start      the wearer stands on the first point, facing the second
- while walking   -> GET  /state                  their position, in our model frame, fed to the navigator;
                                                  their steps and the belt's taps, for the dashboard
- a reroute       -> POST /route "reroute": true  the belt keeps its position and calibration and carries on
- /api/belt/floor -> POST /floor                  the wearer is on the next floor, where the route goes on
- /api/belt/stop  -> POST /stop                   taps both sides

everything here is best effort, like the collar: a belt that is off, out of range or refusing must never
turn a nav request into a 500. the route still plans and speaks; you just can't feel it. the last error is
kept for GET /api/belt.

at a change of floor the belt stops the wearer and waits (its state is FLOOR). it cannot tell when they are
upstairs, so someone says so: /api/belt/floor here, or Next floor in the belt's own viewer.
"""
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request

TIMEOUT_S = 3.0
POLL_S = 0.5
WATCH_S = 2.0   # how often an idle belt is asked whether it is there, once a dashboard has asked
ONLINE_S = 6.0  # no answer for this long: the belt is off or out of range

# the pi is on the phone's hotspot or a cable: never send it through a proxy
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_lock = threading.Lock()
_state = {"session": None, "floor": None, "error": None, "link_error": None, "route": None, "walking": False,
          "thread": None, "points": None, "steps": None, "tap_seq": None, "seen_at": None, "last": None,
          "watch": None}


class BeltError(Exception):
    pass


def url():
    u = (os.environ.get("NUDGE_BELT_URL") or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "http://" + u
    return u or None


def enabled():
    return url() is not None


def _call(method, path, body=None):
    base = url()
    if not base:
        raise BeltError("NUDGE_BELT_URL is not set")
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:  # the belt answered and said no; it says why
        try:
            why = json.loads(ex.read().decode()).get("error")
        except Exception:
            why = None
        raise BeltError(f"{method} {path}: {why or 'HTTP ' + str(ex.code)}") from None
    except (urllib.error.URLError, OSError, ValueError) as ex:
        raise BeltError(f"{method} {path}: {getattr(ex, 'reason', ex)}") from None


def _try(fn, *args):
    """run one belt command; never raise. returns its result, or None with the reason kept in `error`
    until the next command succeeds."""
    try:
        out = fn(*args)
        _state["error"] = None
        return out
    except BeltError as ex:
        _state["error"] = str(ex)
        return None


def _poll():
    """the belt's /state, or None. a failed poll is a link problem, kept apart from a refused command."""
    try:
        st = _call("GET", "/state")
        _state.update(link_error=None, seen_at=time.time(), last=st)
        return st
    except BeltError as ex:
        _state["link_error"] = str(ex)
        return None


def model_heading(belt_heading_deg):
    """the belt's heading is compass style in the route frame: degrees clockwise from +y.
    ours is radians counter-clockwise from +x (geom.heading)."""
    h = math.radians(90.0 - belt_heading_deg)
    return (h + math.pi) % (2 * math.pi) - math.pi


def _points(nav):
    return nav.route.to_dict(nav.g)["points"]


def send_route(session, nav):
    """give the belt this session's route. the belt has one wearer, so this takes it over."""
    if not enabled() or not getattr(nav, "route", None):
        return None
    points = _points(nav)

    def go():
        if (_call("GET", "/state").get("state")) == "NAVIGATING":
            _call("POST", "/stop")  # it refuses a new route while it is guiding
        return _call("POST", "/route", {"route": {"points": points}})

    out = _try(go)
    with _lock:
        _state.update(session=session, floor=points[0][2], route=out, walking=False, points=points,
                      steps=None, tap_seq=None)
    return out


def reroute(session, nav):
    """the navigator changed its route mid-walk: hand the belt the new one. the belt keeps its position and
    its calibration and carries on from where the wearer is, so nobody has to stop. each new route is
    offered once; a belt runtime too old to take one refuses, and the refusal is kept as the error."""
    if not enabled() or _state["session"] != session or not getattr(nav, "route", None):
        return None
    points = _points(nav)
    if points == _state["points"]:
        return None
    _state["points"] = points
    if len(points) < 2:
        return None  # "you are already there": nothing to walk, and the belt would only refuse it
    out = _try(_call, "POST", "/route", {"route": {"points": points}, "reroute": True})
    if out is not None:
        _state["route"] = out
    return out


def begin(session):
    """the wearer is on the first point facing the second: line the belt up and let it guide."""
    if not enabled() or _state["session"] != session:
        return None

    def go():
        cal = _call("POST", "/calibrate")
        _call("POST", "/start")
        return cal

    out = _try(go)
    _state["walking"] = out is not None
    return out


def stop():
    out = _try(_call, "POST", "/stop") if enabled() else None
    _state["walking"] = False
    return out


def next_floor():
    """the wearer is on the next floor, standing where the route goes on. the belt puts them there and
    guides again; the floor it reports from then on is the new one."""
    return _try(_call, "POST", "/floor") if enabled() else None


def wearer(height_cm):
    """someone else is wearing it: the belt scales its step length to their height."""
    return _try(_call, "POST", "/wearer", {"height_cm": height_cm}) if enabled() else None


def _floor(st):
    """the floor the wearer is on: the belt says which stretch of the route it is walking. an older belt
    runtime does not, and only ever walks the first."""
    floor = (st.get("route") or {}).get("floor")
    return _state["floor"] if floor is None else floor


def position():
    """(x, y, floor, heading, belt_state) where the belt thinks the wearer is, or None."""
    st = _poll() if enabled() else None
    if not st or not st.get("pose"):
        return None
    p = st["pose"]
    return p["x"], p["y"], _floor(st), model_heading(p["heading_deg"]), st.get("state")


def cue(tap):
    """a belt tap in the collar's cue words, which the dashboard already knows how to show."""
    kind, side, why = tap.get("kind"), tap.get("side"), tap.get("reason")
    if kind == "arrived":
        return "arrive"
    if kind == "stop":
        return "floor" if why == "floor" else "stop"
    if why == "overturn":
        return f"overturn_{side}"  # turned too far and is being called back
    return side if kind == "double_tap" else f"steer_{side}"


def _telemetry(st, tell):
    """what the belt's phone and pods did since the last look, in the dashboard's own words: `step` like a
    phone's steps, `band` like a collar's cues. the first look only takes the counts, so the belt's past is
    not played back."""
    sensor, pose = st.get("sensor") or {}, st.get("pose") or {}
    heading = pose.get("heading_deg", sensor.get("heading_deg"))
    if st.get("sensor_ok"):
        tell("presence", heading_deg=heading)
    count = sensor.get("step_count")
    if isinstance(count, int):
        last = _state["steps"]
        if last is not None and count > last:
            tell("step", steps=count - last, heading_deg=None if heading is None else round(heading, 1),
                 step_m=(st.get("localizer") or {}).get("step_length_m"))
        _state["steps"] = count
    taps = sorted((t for t in st.get("taps") or [] if isinstance(t.get("seq"), int)), key=lambda t: t["seq"])
    newest = taps[-1]["seq"] if taps else 0
    last = _state["tap_seq"]
    if last is not None and newest >= last:
        for t in taps:
            if t["seq"] > last:
                tell("band", cue=cue(t), mode="belt")
    _state["tap_seq"] = newest  # also when it went down: the belt restarted and counts from one again


def follow(session, apply, tell=None, nav_of=None):
    """keep calling apply(x, y, floor, heading) with the belt's position while this session walks.

    tell(kind, **fields), if given, hears about the belt's steps and taps. nav_of(), if given, returns the
    session's navigator: when its route changes mid-walk, the new one goes to the belt.

    one daemon thread, started once per session. it stops when the session is replaced, when detach()
    is called, or after the belt reports ARRIVED (one last position first, so the navigator arrives too).
    """
    if not enabled():
        return None

    def loop():
        while _state["session"] == session and _state["thread"] is me:
            st = _poll()
            if st:
                state, pose = st.get("state"), st.get("pose")
                try:
                    if tell is not None:
                        _telemetry(st, tell)
                    if pose and state in ("NAVIGATING", "ARRIVED"):
                        apply(pose["x"], pose["y"], _floor(st), model_heading(pose["heading_deg"]))
                    if nav_of is not None and state in ("NAVIGATING", "FLOOR"):
                        nav = nav_of()
                        if nav is not None:
                            reroute(session, nav)
                except Exception as ex:  # a navigator hiccup must not kill the feed
                    _state["error"] = f"apply: {ex}"
                if state == "ARRIVED":
                    _state["walking"] = False
                    return
            time.sleep(POLL_S)

    me = threading.Thread(target=loop, name="nudge-belt", daemon=True)
    with _lock:
        _state["thread"] = me
    me.start()
    return me


def detach():
    """forget the session; the follow thread ends on its next turn. does not stop the belt."""
    with _lock:
        _state.update(session=None, floor=None, route=None, walking=False, thread=None, error=None,
                      link_error=None, points=None, steps=None, tap_seq=None)


def watch():
    """ask an idle belt now and then whether it is there, so the dashboard can say so before a walk
    starts. started by the first dashboard that asks; while a walk is followed, that thread does the asking."""
    if not enabled() or _state["watch"] is not None:
        return

    def loop():
        while _state["watch"] is me:
            t = _state["thread"]
            if enabled() and not (t is not None and t.is_alive()):
                _poll()
            time.sleep(WATCH_S)

    me = threading.Thread(target=loop, name="nudge-belt-watch", daemon=True)
    with _lock:
        if _state["watch"] is None:
            _state["watch"] = me
            me.start()


def summary():
    """the belt for the dashboard, without a network call: is it answering, and what it last said."""
    seen, last = _state["seen_at"], _state["last"] or {}
    age = None if seen is None else time.time() - seen
    return {"enabled": enabled(), "online": age is not None and age < ONLINE_S,
            "age_s": None if age is None else round(age, 1), "session": _state["session"],
            "state": last.get("state"), "sensor_ok": last.get("sensor_ok"),
            "floor": (last.get("route") or {}).get("floor"),
            "error": _state["error"] or _state["link_error"]}


def status():
    out = {"enabled": enabled(), "url": url(), "session": _state["session"], "walking": _state["walking"],
           "route": _state["route"], "error": _state["error"], "belt": None}
    if enabled():
        st = _poll()
        out["error"] = _state["error"] or _state["link_error"]
        if st:
            out["belt"] = {k: st.get(k) for k in ("state", "calibrated", "sensor_ok", "paused", "pose", "target",
                                                   "distance_m")}
            out["belt"]["floor"] = (st.get("route") or {}).get("floor")
    return out
