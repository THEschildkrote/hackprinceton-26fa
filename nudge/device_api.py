"""http side of the haptic band. the phone companion polls these and relays lines over ble.

mount in server.py:
    from .device_api import make_router
    app.include_router(make_router(lambda s: _sessions.get(s)))
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .device import encode_live, encode_step, live_packet, route_packet
from .instruct import maneuvers


class Button(BaseModel):
    event: str


def make_router(get_navigator):
    router = APIRouter(prefix="/api/device")
    last = {}  # session -> (line, seq); seq only moves when the line changes

    def nav_or_404(session):
        nav = get_navigator(session)
        if nav is None or nav.route is None:
            raise HTTPException(404, "unknown session or no route")
        return nav

    @router.get("/{session}/route")
    def route(session: str):
        nav = nav_or_404(session)
        pk = route_packet(nav.g, nav.route, maneuvers(nav.g, nav.route))
        return {"lines": [encode_step(p) for p in pk], "steps": pk, "reroutes": nav.reroutes}

    @router.get("/{session}/live")
    def live(session: str):
        nav = nav_or_404(session)
        p = live_packet(nav)
        line = encode_live(p)
        prev, seq = last.get(session, (None, 0))
        if line != prev:
            seq += 1
            last[session] = (line, seq)
        nxt = nav.ms[nav.mi] if nav.mi < len(nav.ms) else None
        return {"line": line, "seq": seq, "packet": p, "reroutes": nav.reroutes,
                "say": nxt["text"] if nxt else "You have arrived."}

    @router.post("/{session}/button")
    def button(session: str, b: Button):
        nav = nav_or_404(session)
        if b.event == "repeat":
            nxt = nav.ms[nav.mi] if nav.mi < len(nav.ms) else None
            return {"say": nxt["text"] if nxt and nxt["text"] else "You have arrived."}
        if b.event == "lost":
            if nav.pos is None:
                return {"say": "I don't know where you are yet. Stay where you are and ask for help."}
            x, y, floor = nav.pos
            ev = nav._reroute(x, y, floor, nav.heading, "Let me find you.")
            return {"say": " ".join(e["text"] for e in ev if e.get("text")), "reroutes": nav.reroutes}
        raise HTTPException(400, "event must be repeat or lost")

    return router
