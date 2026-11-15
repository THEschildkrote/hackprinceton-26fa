"""Draw the venue's upper floor with a route on it and the start marked. PNG, from the team server's graph."""
import json
import math
import sys
from collections import Counter

from PIL import Image, ImageDraw, ImageFont

GRAPH, OUT = sys.argv[1], sys.argv[2]
ROUTE = sys.argv[3].split(",")            # node ids, in walking order
TITLE = sys.argv[4] if len(sys.argv) > 4 else "Route"
FLOOR = "h0ysdfsrzyt6errbx1x8g92wc"

g = json.load(open(GRAPH))
nodes = {n["id"]: n for n in g["nodes"]}
floor = next(f for f in g["floors"] if f["id"] == FLOOR)
walls = floor["walls"]
rooms = [r for r in g["rooms"].values() if r["floor"] == FLOOR]
print("wall kinds:", Counter(w[2] for w in walls))

xs = [p[0] for w in walls for p in w[:2]] + [n["x"] for n in nodes.values() if n["floor"] == FLOOR]
ys = [p[1] for w in walls for p in w[:2]] + [n["y"] for n in nodes.values() if n["floor"] == FLOOR]
x0, x1, y0, y1 = min(xs) - 1.5, max(xs) + 1.5, min(ys) - 1.5, max(ys) + 1.5
S = 52                                    # pixels per metre
TOP = 150
W, H = int((x1 - x0) * S), int((y1 - y0) * S) + TOP + 110
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)


def font(size, bold=False):
    try:
        return ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def P(x, y):
    return ((x - x0) * S, TOP + (y1 - y) * S)   # y up in the model, down on the page


def centroid(poly):
    return sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)


def area(poly):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


# rooms: a light fill, then the walls on top
for r in rooms:
    void = r["label"].lower() == "void"
    d.polygon([P(*p) for p in r["poly"]], fill="#e9e9e9" if void else "#f3f6fb")
for a, b, kind in walls:
    if kind == "wall":
        d.line([P(*a), P(*b)], fill="#2b3442", width=4)
    else:                                   # a door or an opening: drawn light, so it reads as a way through
        d.line([P(*a), P(*b)], fill="#9db7d6", width=3)

# room names, biggest rooms only, so the page stays readable
seen = set()
for r in sorted(rooms, key=lambda r: -area(r["poly"])):
    label = r["label"]
    if label.lower() == "void" or area(r["poly"]) < 6:
        continue
    cx, cy = centroid(r["poly"])
    key = (label, round(cx / 6), round(cy / 6))
    if key in seen:
        continue
    seen.add(key)
    f = font(22)
    tw = d.textlength(label, font=f)
    d.text((P(cx, cy)[0] - tw / 2, P(cx, cy)[1] - 12), label, fill="#6b7888", font=f)

# every scan position, small: these are the places the planner can route through
for n in nodes.values():
    if n["floor"] == FLOOR:
        x, y = P(n["x"], n["y"])
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill="#b9c4d2")

# place names: the scan names each position's surroundings, so name each cluster of positions once
pts = [(nodes[i]["x"], nodes[i]["y"]) for i in ROUTE]
by_name = {}
for n in nodes.values():
    if n["floor"] == FLOOR and n.get("room_label"):
        by_name.setdefault(n["room_label"], []).append((n["x"], n["y"]))
def off_route(x, y):
    """how far a spot is from the route line, in metres."""
    best = math.inf
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        u = min(max(((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy), 0.0), 1.0)
        best = min(best, math.hypot(x - (ax + u * dx), y - (ay + u * dy)))
    return best


ends = {nodes[ROUTE[0]].get("room_label"), nodes[ROUTE[-1]].get("room_label")}
for name, where in by_name.items():
    if name in ends:
        continue                            # the start and the end are named by their own markers
    cx, cy = centroid(where)
    # keep the name clear of the route line: try the spot itself, then a few places beside it
    for ox, oy in ((0, 0), (3.4, 0), (-3.4, 0), (0, -1.8), (0, 1.8), (3.4, -1.8), (3.4, 1.8), (5.0, 0)):
        if off_route(cx + ox, cy + oy) >= 2.0:
            cx, cy = cx + ox, cy + oy
            break
    f = font(24, True)
    tw = d.textlength(name, font=f)
    px, py = P(cx, cy)
    px = min(max(px - tw / 2, 8), W - tw - 8)
    d.rectangle([px - 6, py - 30, px + tw + 6, py + 2], fill="white")
    d.text((px, py - 28), name, fill="#44546a", font=f)

# the route
d.line([P(*p) for p in pts], fill="#1668d6", width=10, joint="curve")
for p in pts[1:-1]:
    x, y = P(*p)
    d.ellipse([x - 7, y - 7, x + 7, y + 7], fill="white", outline="#1668d6", width=3)

# the sharpest turn on the way, with how far it is from the start
best, walked, at = 0.0, 0.0, None
for i in range(1, len(pts) - 1):
    walked += math.dist(pts[i - 1], pts[i])
    h1 = math.atan2(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
    h2 = math.atan2(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
    turn = (math.degrees(h2 - h1) + 180) % 360 - 180
    if abs(turn) > abs(best):
        best, at, far = turn, pts[i], walked
if at is not None and abs(best) >= 30:
    tx, ty = P(*at)
    note = f"turn {'right' if best > 0 else 'left'} here, {far:.0f} m from the start"
    f = font(24, True)
    tw = d.textlength(note, font=f)
    d.rectangle([tx + 18, ty - 44, tx + tw + 30, ty - 10], fill="white", outline="#1668d6", width=2)
    d.text((tx + 24, ty - 42), note, fill="#1668d6", font=f)

# the end
ex, ey = P(*pts[-1])
d.ellipse([ex - 20, ey - 20, ex + 20, ey + 20], fill="#f2b632", outline="#7a5600", width=4)
end_name = nodes[ROUTE[-1]].get("room_label") or "end"
d.text((ex + 28, ey - 16), f"END: {end_name}", fill="#7a5600", font=font(28, True))

# the start: a ring, and an arrow along the way to face
sx, sy = P(*pts[0])
fx, fy = P(*pts[1])
ang = math.atan2(fy - sy, fx - sx)
d.ellipse([sx - 34, sy - 34, sx + 34, sy + 34], outline="#d62828", width=7)
d.ellipse([sx - 12, sy - 12, sx + 12, sy + 12], fill="#d62828")
L = 120
tx, ty = sx + L * math.cos(ang), sy + L * math.sin(ang)
d.line([(sx, sy), (tx, ty)], fill="#d62828", width=9)
for side in (-1, 1):
    a2 = ang + math.pi + side * 0.45
    d.line([(tx, ty), (tx + 34 * math.cos(a2), ty + 34 * math.sin(a2))], fill="#d62828", width=9)
start_name = nodes[ROUTE[0]].get("room_label") or "start"
label = f"START HERE: {start_name}"
f = font(30, True)
tw = d.textlength(label, font=f)
lx = min(max(sx + 46, 10), W - tw - 20)
d.rectangle([lx - 8, sy + 18, lx + tw + 8, sy + 62], fill="white", outline="#d62828", width=3)
d.text((lx, sy + 22), label, fill="#d62828", font=f)
d.text((lx, sy + 68), "face along the red arrow", fill="#d62828", font=font(24))

# title, legs, scale
legs = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
d.text((24, 18), TITLE, fill="#111", font=font(40, True))
d.text((24, 70), f"Upper floor  ·  {sum(legs):.1f} m  ·  about {round(sum(legs) / 0.5)} steps", fill="#333", font=font(28))
d.text((24, 108), "Blue line: the route. Grey dots: scan positions. Pale lines in walls: doors and openings.",
       fill="#666", font=font(22))
bx, by = 30, H - 60
d.line([(bx, by), (bx + 5 * S, by)], fill="#111", width=5)
for k in (0, 5):
    d.line([(bx + k * S, by - 9), (bx + k * S, by + 9)], fill="#111", width=5)
d.text((bx + 5 * S + 14, by - 15), "5 m", fill="#111", font=font(26))
img.save(OUT)
print("saved", OUT, img.size)
