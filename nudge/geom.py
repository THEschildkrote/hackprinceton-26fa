"""2d geometry in the matterport model frame (metres, x/y horizontal, z up)."""
import math


def dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _orient(a, b, c):
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)


def segments_cross(p1, p2, q1, q2):
    """proper or touching intersection of segments p1p2 and q1q2."""
    o1, o2 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    o3, o4 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True

    def on(a, b, c):
        return min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9 and \
            min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9

    return (o1 == 0 and on(p1, p2, q1)) or (o2 == 0 and on(p1, p2, q2)) or \
        (o3 == 0 and on(q1, q2, p1)) or (o4 == 0 and on(q1, q2, p2))


def point_in_poly(p, poly):
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def project(p, a, b):
    """(t, closest point, distance) of p onto segment ab, t in [0, 1]."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    c = (a[0] + t * dx, a[1] + t * dy)
    return t, c, dist(p, c)


def poly_len(pts):
    return sum(dist(a, b) for a, b in zip(pts, pts[1:]))


def heading(a, b):
    """radians, ccw from +x."""
    return math.atan2(b[1] - a[1], b[0] - a[0])


def turn_angle(h1, h2):
    """signed degrees from heading h1 to h2; positive is a left turn (ccw, z up)."""
    return (math.degrees(h2 - h1) + 180) % 360 - 180
