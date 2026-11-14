"""the belt: a raspberry pi that runs its own on-body runtime and taps the wearer.

everything upstream of the steps is ours: the building, the route, the words. the belt does the walking part
itself. the phone's steps and heading go straight to the pi, the pi keeps the position and fires the taps. we
hand it the route and read back where the wearer is.

    NUDGE_BELT_URL=http://172.20.10.2:8000 python -m uvicorn nudge.server:app --port 8765

- nav start      -> POST /route                the route's points, exactly as /api/route returns them
- nav calibrate  -> POST /calibrate, /start    the wearer stands on the first point, facing the second
- while walking  -> GET  /state                their position, in our model frame, fed to the navigator
- /api/belt/stop -> POST /stop                 taps both sides

everything here is best effort, like the collar: a belt that is off, out of range or refusing must never
turn a nav request into a 500. the route still plans and speaks; you just can't feel it. the last error is
kept for GET /api/belt.

what the belt cannot do yet: change floor (it follows the route up to the first floor change) and take a
new route mid-walk (it has to be stopped and re-calibrated at the start of the new one).
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

# the pi is on the phone's hotspot or a cable: never send it through a proxy
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_lock = threading.Lock()
_state = {"session": None, "floor": None, "error": None, "link_error": None, "route": None, "walking": False,
          "thread": None}


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
        _state["link_error"] = None
        return st
    except BeltError as ex:
        _state["link_error"] = str(ex)
        return None


def model_heading(belt_heading_deg):
    """the belt's heading is compass style in the route frame: degrees clockwise from +y.
    ours is radians counter-clockwise from +x (geom.heading)."""
    h = math.radians(90.0 - belt_heading_deg)
    return (h + math.pi) % (2 * math.pi) - math.pi


def send_route(session, nav):
    """give the belt this session's route. the belt has one wearer, so this takes it over."""
    if not enabled() or not getattr(nav, "route", None):
        return None
    points = nav.route.to_dict(nav.g)["points"]

    def go():
        if (_call("GET", "/state").get("state")) == "NAVIGATING":
            _call("POST", "/stop")  # it refuses a new route while it is guiding
        return _call("POST", "/route", {"route": {"points": points}})

    out = _try(go)
    with _lock:
        _state.update(session=session, floor=points[0][2], route=out, walking=False)
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


def wearer(height_cm):
    """someone else is wearing it: the belt scales its step length to their height."""
    return _try(_call, "POST", "/wearer", {"height_cm": height_cm}) if enabled() else None


def position():
    """(x, y, floor, heading, belt_state) where the belt thinks the wearer is, or None."""
    st = _poll() if enabled() else None
    if not st or not st.get("pose"):
        return None
    p = st["pose"]
    return p["x"], p["y"], _state["floor"], model_heading(p["heading_deg"]), st.get("state")


def follow(session, apply):
    """keep calling apply(x, y, floor, heading) with the belt's position while this session walks.

    one daemon thread, started once per session. it stops when the session is replaced, when detach()
    is called, or after the belt reports ARRIVED (one last position first, so the navigator arrives too).
    """
    if not enabled():
        return None

    def loop():
        while _state["session"] == session and _state["thread"] is me:
            pos = position()
            if pos and pos[4] in ("NAVIGATING", "ARRIVED"):
                try:
                    apply(*pos[:4])
                except Exception as ex:  # a navigator hiccup must not kill the feed
                    _state["error"] = f"apply: {ex}"
                if pos[4] == "ARRIVED":
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
                      link_error=None)


def status():
    out = {"enabled": enabled(), "url": url(), "session": _state["session"], "walking": _state["walking"],
           "route": _state["route"], "error": _state["error"], "belt": None}
    if enabled():
        st = _poll()
        out["error"] = _state["error"] or _state["link_error"]
        if st:
            out["belt"] = {k: st.get(k) for k in ("state", "calibrated", "sensor_ok", "paused", "pose", "target",
                                                   "distance_m")}
    return out
