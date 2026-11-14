"""what the haptic device gets: metres and degrees, no words.

conventions
- metres are horizontal walking metres, rounded to 0.1. floor changes carry no walking metres;
  their vertical travel is `rise` (metres, + = up), the elevator/stair edge length is not walking.
- degrees are signed integers in the model frame, + = left (ccw, z up), - = right.
- `deg` on a maneuver is the turn you make when you reach it; `m` is the metres to walk to reach it
  from the previous maneuver.

ascii lines (serial / one ble packet, always < 20 bytes):
  route step:  "<i>,<act>,<dir>,<deg>,<m>"    e.g. "2,T,L,90,4.5"
  floor step:  "<i>,F,<U|D>,<E|S|X>,<floor#>"  e.g. "6,F,U,E,1"     (floor# is the index in g.floors)
  live:        "N,<m_next>,<deg_next>,<act><dir>,<flags>"  e.g. "N,2.4,-15,TR,0"
               flags: bit0 off route, bit1 arrived. deg_next is "" when heading is unknown.
"""
from .instruct import CUES

ACT = {"depart": "D", "turn": "T", "bear": "B", "uturn": "U", "straight": "S", "walk": "W",
       "floor": "F", "arrive": "A"}
ACT_BACK = {v: k for k, v in ACT.items()}
VIA = {"elevator": "E", "stairs": "S"}
VIA_BACK = {"E": "elevator", "S": "stairs", "X": "unknown"}
MAX_M = 999.9


def _dir(m):
    side = m.get("side")
    return "L" if side == "left" else "R" if side == "right" else "S"


def _m(x):
    return round(min(MAX_M, max(0.0, x)), 1)


def route_packet(g, route, maneuvers):
    floor_ix = {f["id"]: i for i, f in enumerate(g.floors)}
    out = []
    for i, m in enumerate(maneuvers):
        p = {"i": i, "act": m["type"], "dir": _dir(m), "deg": m.get("turn_deg") or 0,
             "m": _m(m.get("distance", 0.0)), "cue": CUES.get(m.get("cue"), [])}
        if m["type"] == "floor":
            p.update(dir="S", deg=0, m=0.0, floor=m["to_floor"], floor_i=floor_ix.get(m["to_floor"], 0),
                     via=m.get("via", "unknown"), rise=round(m.get("rise_m", 0.0), 1))
        out.append(p)
    return out


def live_packet(nav, heading=None):
    lv = nav.live(heading)
    nxt = lv["next"] or {}
    return {"m_next": lv["distance_to_next_m"], "deg_next": lv["bearing_to_next_deg"],
            "act_next": nxt.get("type"), "dir_next": _dir(nxt) if nxt else "S",
            "off": bool(lv["off"]), "done": bool(lv["done"])}


# ascii

def encode_step(p):
    if p["act"] == "floor":
        ud = "U" if p.get("rise", 0) >= 0 else "D"
        s = f"{p['i']},F,{ud},{VIA.get(p.get('via'), 'X')},{p.get('floor_i', 0)}"
    else:
        s = f"{p['i']},{ACT[p['act']]},{p['dir']},{int(p['deg'])},{_m(p['m'])}"
    assert len(s.encode()) < 20, s
    return s


def decode_step(s):
    f = s.split(",")
    i, act = int(f[0]), ACT_BACK[f[1]]
    if act == "floor":
        return {"i": i, "act": act, "dir": "S", "deg": 0, "m": 0.0, "up": f[2] == "U",
                "via": VIA_BACK[f[3]], "floor_i": int(f[4])}
    return {"i": i, "act": act, "dir": f[2], "deg": int(f[3]), "m": float(f[4])}


def encode_live(p):
    m = "" if p["m_next"] is None else _m(p["m_next"])
    d = "" if p["deg_next"] is None else int(p["deg_next"])
    act = ACT.get(p["act_next"], "-") if p["act_next"] else "-"
    flags = (1 if p["off"] else 0) | (2 if p["done"] else 0)
    s = f"N,{m},{d},{act}{p['dir_next']},{flags}"
    assert len(s.encode()) < 20, s
    return s


def decode_live(s):
    _, m, d, ad, flags = s.split(",")
    fl = int(flags)
    return {"m_next": float(m) if m else None, "deg_next": int(d) if d else None,
            "act_next": ACT_BACK.get(ad[0]), "dir_next": ad[1], "off": bool(fl & 1), "done": bool(fl & 2)}
