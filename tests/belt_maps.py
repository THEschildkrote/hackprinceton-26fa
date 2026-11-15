"""two drawn floor plans for the belt end-to-end tests (test_app_belt.py): a straight line and one corner.
they are test fixtures only, served by monkeypatching nudge.fetch.load; the product has no demo maps, so every
run is a regular run in a scanned building.
    test-line     10 m dead straight to the finish, across an open floor
    test-corner   a corridor: 6 m, a 90 degree right turn, 6 m
each map is one room, the finish is a scan point named GOAL and the start one named "entrance", which is what the
phone asks to start from.
"""
import os

LINE, CORNER = "test-line", "test-corner"
NAMES = {LINE: "Test: straight line", CORNER: "Test: one corner"}
GOAL = "Finish"
START = "entrance"

START_GAP_M = 2.5   # first scan point after the start. more than navigator.START_SLACK_M (2.0), or the
                    # route would begin there and the walk would come up that much short
HALF_W = 3.5        # the line's open floor is 7 m wide
SIDE_M = 2.75       # side scan points: further than Navigator.off_route_m (2.0), so veering onto them is off route


def _builder():
    from nudge.fetch import fixture
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
    """the model for one of the test sids."""
    return line(10.0) if sid == LINE else corner(6.0, 6.0, "right")
