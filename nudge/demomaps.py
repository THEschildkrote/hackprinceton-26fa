"""two made-up floor plans for a live demo on any clear patch of floor: a straight line and one corner.

we may not get to demo inside a scanned building. these need no scan, no keys and no network: the map is
drawn here, in metres, and loaded like any other building. the traveller stands at the start, faces the
way they will walk, calibrates, and walks. everything after that is the real navigator, tracker and belt.

    demo-line     dead straight to the finish, across an open floor
    demo-corner   a corridor with one 90 degree turn

the sids do not start with "synthetic" on purpose: that prefix borrows the test building's costar record,
and its made-up tenants would show up as destinations here.

sizes come from the environment, read when the map is first loaded (restart the server to change them):

    NUDGE_DEMO_LINE_M=10        metres from the start to the finish
    NUDGE_DEMO_CORNER_M=6,6     metres before the turn, metres after it
    NUDGE_DEMO_TURN=right       or left

what the traveller gets, with nothing changed in the navigator:
- facing more than 15 degrees off the line: a light tap on the side to turn toward, faster the further off.
- walking off at 50 degrees or more: the line map has scan points 2.75 m either side, so they visibly leave
  the route; "You are off the route", the off-route cue, and a new route from where they are.
- the corner: "Turn right now" with the turn cue. the corridor has no way on past the turn, so someone who
  misses it is held at the corner and tapped toward the turn until they take it.

it follows the compass, not the feet: side-stepping while still facing forward changes nothing.

each map is one room. a second room for the finish would put a room boundary on the route, and every
passable boundary a route crosses is spoken as "through a doorway". so the finish is a scan point named
GOAL instead (a goal matches a scan point's label before it matches a room), and the start is one named
"entrance", which is what the phone asks to start from.
"""
import os

LINE, CORNER = "demo-line", "demo-corner"
NAMES = {LINE: "Demo: straight line", CORNER: "Demo: one corner"}
GOAL = "Finish"
START = "entrance"

START_GAP_M = 2.5   # first scan point after the start. more than navigator.START_SLACK_M (2.0), or the
                    # route would begin there and the walk would come up that much short
HALF_W = 3.5        # the line's open floor is 7 m wide
SIDE_M = 2.75       # side scan points: further than Navigator.off_route_m (2.0), so veering onto them is off route


def _builder():
    from .fetch import fixture
    return fixture("synthetic")._Builder()


def _num(name, default, lo, hi):
    try:
        return max(lo, min(hi, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def line(length_m=10.0):
    """start at (3.5, 1), finish length_m further up the y axis."""
    L = max(6.0, float(length_m))
    cx, y0 = HALF_W, 1.0
    b = _builder()
    b.floor("f1", 0)
    b.room("f1", GOAL, [(0, 0), (2 * HALF_W, 0), (2 * HALF_W, y0 + L + 1), (0, y0 + L + 1)])
    b.pano(START, cx, y0, 1.5, "f1")
    y, r = y0 + START_GAP_M, 0
    while y <= y0 + L - 2.0:
        for c, x in enumerate((cx - SIDE_M, cx, cx + SIDE_M)):
            b.pano(f"F{r}{c}", x, y, 1.5, "f1")
        y, r = y + 1.5, r + 1
    b.pano(GOAL, cx, y0 + L, 1.5, "f1")
    m = b.model()
    m["id"], m["name"] = LINE, NAMES[LINE]
    return m


def corner(first_m=6.0, second_m=6.0, turn="right"):
    """start at (1, 1), walk first_m up the y axis, turn, walk second_m. the corridor is 2 m wide."""
    a, s = max(4.0, float(first_m)), max(4.0, float(second_m))
    yc = 1.0 + a                     # the y of the second leg's centre line
    xe = 1.0 + s                     # the finish, for a right turn
    left = str(turn).strip().lower().startswith("l")
    fx = (lambda x: 2.0 - x) if left else (lambda x: x)   # a left turn is the same map mirrored about x = 1
    b = _builder()
    b.floor("f1", 0)
    b.room("f1", GOAL, [(fx(x), y) for x, y in
                        [(0, 0), (2, 0), (2, yc - 1), (xe + 1, yc - 1), (xe + 1, yc + 1), (0, yc + 1)]])
    pts = [(START, 1.0, 1.0)]
    y, i = 1.0 + START_GAP_M, 1
    while y <= yc - 1.75:            # stop short of the inside corner, so nothing links diagonally across it
        pts.append((f"C{i}", 1.0, y))
        y, i = y + 1.5, i + 1
    pts.append((f"C{i}", 1.0, yc))   # the corner itself
    x, i = 3.5, i + 1
    while x <= xe - 1.5:
        pts.append((f"C{i}", x, yc))
        x, i = x + 2.0, i + 1
    pts.append((GOAL, xe, yc))
    for lab, x, y in pts:
        b.pano(lab, fx(x), y, 1.5, "f1")
    m = b.model()
    m["id"], m["name"] = CORNER, NAMES[CORNER]
    return m


def model(sid):
    """the model for one of the demo sids, sized from the environment."""
    if sid == LINE:
        return line(_num("NUDGE_DEMO_LINE_M", 10.0, 6.0, 40.0))
    if sid == CORNER:
        raw = (os.environ.get("NUDGE_DEMO_CORNER_M") or "6,6").replace(" ", "").split(",")
        try:
            a, s = float(raw[0]), float(raw[1] if len(raw) > 1 else raw[0])
        except ValueError:
            a, s = 6.0, 6.0
        return corner(max(4.0, min(20.0, a)), max(4.0, min(20.0, s)), os.environ.get("NUDGE_DEMO_TURN", "right"))
    raise KeyError(sid)
