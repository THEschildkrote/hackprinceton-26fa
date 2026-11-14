import math

import pytest

from nudge import walkgraph
from nudge.device import decode_live, decode_step, encode_live, encode_step, live_packet, route_packet
from nudge.instruct import maneuvers
from nudge.navigator import Navigator
from nudge.router import plan
from nudge.sim import walk
from nudge.walkgraph import Node, WalkGraph
from tests.synthetic import building


def chain(pts):
    """open floor, nodes linked in order only, so the route is exactly these points."""
    g = WalkGraph()
    g.floors = [{"id": "f", "label": "F1", "walls": []}]
    for i, (x, y) in enumerate(pts):
        g.add_node(Node(f"n{i}", f"n{i}", x, y, 0.0, "f"))
    for i in range(len(pts) - 1):
        g.link(f"n{i}", f"n{i + 1}")
    return g, plan(g, "n0", f"n{len(pts) - 1}", "default")


@pytest.fixture(scope="module")
def syn():
    return walkgraph.build(building())


# turn direction and angle, by hand

def test_south_then_east_is_left_90():
    g, r = chain([(0, 10), (0, 5), (0, 0), (5, 0), (10, 0)])
    ms = maneuvers(g, r)
    assert [m["type"] for m in ms] == ["depart", "turn", "arrive"]
    assert ms[1]["side"] == "left" and ms[1]["turn_deg"] == 90
    assert ms[1]["distance"] == pytest.approx(10.0) and ms[1]["along"] == pytest.approx(10.0)
    assert ms[2]["distance"] == pytest.approx(10.0) and ms[2]["along"] == pytest.approx(20.0)


def test_east_then_south_is_right_90():
    g, r = chain([(0, 0), (5, 0), (10, 0), (10, -5), (10, -10)])
    ms = maneuvers(g, r)
    assert ms[1]["type"] == "turn" and ms[1]["side"] == "right" and ms[1]["turn_deg"] == -90


def test_jitter_is_straight():
    pts = [(0, 0), (2.5, 0.1), (5, -0.1), (7.5, 0.1), (10, 0)]
    g, r = chain(pts)
    ms = maneuvers(g, r)
    assert [m["type"] for m in ms] == ["depart", "arrive"]
    assert ms[1]["distance"] == pytest.approx(sum(math.dist(a, b) for a, b in zip(pts, pts[1:])))


def test_bear_and_uturn_angles():
    a = math.radians(40)
    g, r = chain([(0, 0), (5, 0), (5 + 5 * math.cos(a), 5 * math.sin(a))])
    m = maneuvers(g, r)[1]
    assert m["type"] == "bear" and m["side"] == "left" and m["turn_deg"] == 40
    b = math.radians(-150)
    g, r = chain([(0, 0), (5, 0), (5 + 5 * math.cos(b), 5 * math.sin(b))])
    m = maneuvers(g, r)[1]
    assert m["type"] == "uturn" and m["turn_deg"] == -150


# metres on the synthetic building

def test_kitchen_to_office_by_hand(syn):
    ms = maneuvers(syn, plan(syn, "p_K3", "Office", "blind"))
    assert [m["type"] for m in ms] == ["depart", "bear", "turn", "arrive"]
    # K3(2,11) -> K2(4,9) -> K1(6.5,7) is one leg after simplification, then due south to (6.5,2.5), then east
    k3k1 = math.hypot(2, 2) + math.hypot(2.5, 2)
    in_heading = math.degrees(math.atan2(-4, 4.5))
    assert ms[1]["side"] == "right" and ms[1]["turn_deg"] == round(-90 - in_heading)  # -48
    assert ms[1]["distance"] == pytest.approx(k3k1, abs=1e-9)
    assert ms[2]["side"] == "left" and ms[2]["turn_deg"] == 90
    assert ms[2]["distance"] == pytest.approx(4.5)
    assert ms[3]["distance"] == pytest.approx(3.0)
    assert ms[3]["along"] == pytest.approx(k3k1 + 7.5)


@pytest.mark.parametrize("profile", ["blind", "wheelchair", "default"])
def test_metres_add_up_across_floors(syn, profile):
    r = plan(syn, "p_K3", "Conference Room", profile)
    ms = maneuvers(syn, r)
    vertical = sum(m.get("vertical_m", 0.0) for m in ms)
    assert vertical > 0
    assert sum(m["distance"] for m in ms) + vertical == pytest.approx(r.length, abs=1e-6)
    horiz = sum(syn.edge(a, b).length for a, b in zip(r.nodes, r.nodes[1:])
                if "vertical" not in syn.edge(a, b).tags)
    assert ms[-1]["walk_along"] == pytest.approx(horiz, abs=1e-6)
    for prev, m in zip(ms, ms[1:]):
        assert m["along"] >= prev["along"] - 1e-9
        assert m["along"] == pytest.approx(prev["along"] + m["distance"] + prev.get("vertical_m", 0.0), abs=1e-6)
        assert m["walk_along"] == pytest.approx(prev["walk_along"] + m["distance"], abs=1e-6)
    fl = next(m for m in ms if m["type"] == "floor")
    assert fl["rise_m"] == pytest.approx(4.0) and fl["to_floor"] == "f2"


# live numbers

def test_live_bearing_facing_x_route_goes_y():
    g, _ = chain([(0, 0), (0, 5), (0, 10)])
    nav = Navigator(g, "n2", "default")
    nav.start(0, 0, "f", heading=0.0)
    lv = nav.live()
    assert lv["bearing_to_next_deg"] == 90 and lv["distance_to_next_m"] == 10.0
    assert nav.live(math.pi / 2)["bearing_to_next_deg"] == 0
    assert nav.live(math.pi)["bearing_to_next_deg"] == -90
    assert abs(nav.live(-math.pi / 2)["bearing_to_next_deg"]) == 180
    nav.update(0, 4, "f", math.pi / 2)
    assert nav.live()["distance_to_next_m"] == 6.0 and nav.live()["bearing_to_next_deg"] == 0
    st = nav.state()
    assert st["distance_to_next_m"] == 6.0 and st["bearing_to_next_deg"] == 0


def test_live_bearing_steers_back_to_route():
    g, _ = chain([(0, 0), (0, 5), (0, 10)])
    nav = Navigator(g, "n2", "default")
    nav.start(0, 0, "f", heading=math.pi / 2)
    nav.update(1, 4, "f", math.pi / 2)  # 1 m right of the route, facing along it
    # target is (0, 5.5): atan2(1.5, -1) = 123.7 deg, minus 90 = +34 (left, back toward the line)
    assert nav.live()["bearing_to_next_deg"] == 34


def test_live_before_a_corner():
    g, _ = chain([(0, 10), (0, 5), (0, 0), (5, 0), (10, 0)])
    nav = Navigator(g, "n4", "default")
    nav.start(0, 10, "f", heading=-math.pi / 2)
    nav.update(0, 1, "f", -math.pi / 2)
    lv = nav.live()
    assert lv["distance_to_next_m"] == 1.0  # turn is at (0, 0)
    assert lv["next"]["type"] == "turn" and lv["next"]["turn_deg"] == 90
    # lookahead point (0.5, 0): heading -63.4 deg, from -90 that is +27 (start easing left)
    assert lv["bearing_to_next_deg"] == 27


def test_live_to_elevator_counts_walking_metres(syn):
    nav = Navigator(syn, "Conference Room", "wheelchair")
    o4 = syn.nodes["p_O4"]
    nav.start(o4.x, o4.y, "f1", heading=math.pi / 2)
    lv = nav.live()
    assert lv["next"]["type"] == "walk"
    # O4 (12, 4.5) -> E1 (13, 7), squared up through the middle of the 2 m opening at y=6:
    # (12, 4.5) -> (13, 5.5) -> (13, 6) -> (13, 6.5) -> (13, 7)
    assert lv["distance_to_next_m"] == round(math.hypot(1, 1) + 1.5, 1)


# packets and ascii

def test_route_packet_and_ascii_roundtrip(syn):
    r = plan(syn, "p_K3", "Conference Room", "blind")
    pk = route_packet(syn, r, maneuvers(syn, r))
    assert [p["act"] for p in pk][0] == "depart" and pk[-1]["act"] == "arrive"
    fl = next(p for p in pk if p["act"] == "floor")
    assert fl["via"] == "elevator" and fl["floor"] == "f2" and fl["floor_i"] == 1 and fl["rise"] == 4.0
    assert fl["m"] == 0.0
    for p in pk:
        assert "text" not in p and not any(k in p for k in ("feet", "steps"))
        assert isinstance(p["deg"], int) and round(p["m"], 1) == p["m"]
        line = encode_step(p)
        assert len(line.encode()) < 20
        back = decode_step(line)
        assert back["i"] == p["i"] and back["act"] == p["act"]
        if p["act"] != "floor":
            assert back["dir"] == p["dir"] and back["deg"] == p["deg"] and back["m"] == p["m"]
        else:
            assert back["via"] == "elevator" and back["floor_i"] == 1 and back["up"]


def test_ascii_worst_case_fits():
    for p in ({"i": 99, "act": "uturn", "dir": "L", "deg": -180, "m": 5000.0},
              {"i": 99, "act": "floor", "dir": "S", "deg": 0, "m": 0, "rise": -40.0, "via": "stairs", "floor_i": 12}):
        assert len(encode_step(p)) < 20
    lv = {"m_next": 5000.0, "deg_next": -180, "act_next": "uturn", "dir_next": "R", "off": True, "done": True}
    s = encode_live(lv)
    assert len(s) < 20 and decode_live(s)["m_next"] == 999.9 and decode_live(s)["off"]
    s = encode_live({"m_next": 3.0, "deg_next": None, "act_next": None, "dir_next": "S", "off": False, "done": False})
    assert decode_live(s)["deg_next"] is None


def test_live_packets_over_a_walk(syn):
    r = plan(syn, "p_K3", "Conference Room", "blind")
    nav = Navigator(syn, "Conference Room", "blind")
    k3 = syn.nodes["p_K3"]
    nav.start(k3.x, k3.y, k3.floor, 0.0)
    last_m, prev_floor = None, k3.floor
    for x, y, f, h in walk(syn, r, step=0.4):
        nav.update(x, y, f, h)
        p = live_packet(nav)
        s = encode_live(p)
        assert len(s) < 20
        d = decode_live(s)
        assert d["m_next"] == p["m_next"] and d["deg_next"] == p["deg_next"] and d["done"] == p["done"]
        if p["deg_next"] is not None:
            assert -180 <= p["deg_next"] <= 180
            if f != prev_floor:
                # stepping out of the elevator still facing in: the route leaves the way we came,
                # squared up through the middle of the opening, so "turn around", not exactly 180
                assert abs(p["deg_next"]) >= 135
            else:
                # walking exactly on the route, along its direction: never told to turn hard
                assert abs(p["deg_next"]) <= 90
        prev_floor = f
        if nav.done:
            break
        last_m = p["m_next"]
    assert nav.done and live_packet(nav)["done"]
    assert last_m is not None
