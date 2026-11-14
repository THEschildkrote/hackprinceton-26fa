from nudge import walkgraph
from nudge.instruct import maneuvers, orient
from nudge.navigator import Navigator, snap
from nudge.router import NoRoute, plan
from nudge.sim import run, walk
from tests.synthetic import building

import pytest


@pytest.fixture(scope="module")
def g():
    return walkgraph.build(building())


def lab(g, ids):
    return [g.nodes[i].label for i in ids]


def test_graph_connected_and_no_wall_crossing(g):
    assert len(set(g.components().values())) == 1, g.warnings
    for e in g.edges():
        a, b = g.nodes[e.a], g.nodes[e.b]
        if "vertical" in e.tags or "uncertain" in e.tags:
            continue
        assert not g.blocked_line(a.xy, b.xy, a.floor), (a.label, b.label)
    assert g.edge("p_L3", "p_K2") is None  # through the kitchen wall


def test_rooms_and_vertical_links(g):
    assert g.nodes["p_K1"].room_label == "Kitchen"
    assert g.nodes["p_O1"].room_label == "Office"
    assert "elevator" in g.edge("p_E1", "p_E2").tags
    assert "stairs" in g.edge("p_S1", "p_S2").tags
    assert "door" in g.edge("p_L6", "p_K1").tags


def test_floor_assignment_by_z():
    g2 = walkgraph.build(building(with_floor_ids=False))
    assert g2.nodes["p_C1"].floor == "f2" and g2.nodes["p_K1"].floor == "f1"


def test_wheelchair_takes_elevator(g):
    r = plan(g, "p_K3", "Conference Room", "wheelchair")
    names = lab(g, r.nodes)
    assert "E1" in names and "S1" not in names
    assert g.nodes[r.nodes[-1]].room_label == "Conference Room"


def test_room_goal_and_left_turn(g):
    r = plan(g, "p_K3", "Office", "blind")
    ms = maneuvers(g, r)
    assert ms[0]["type"] == "depart" and ms[-1]["type"] == "arrive"
    # heading south out of the kitchen, the office is to the east: a left turn
    assert any(m["type"] in ("turn", "bear") and m["side"] == "left" for m in ms), [m["text"] for m in ms]


def test_orient():
    import math
    assert orient(0.0, math.pi / 2)[0].startswith("Turn left")
    assert orient(0.0, -math.pi / 2)[0].startswith("Turn right")
    assert orient(0.0, math.pi)[0] == "Turn around."


def test_hazard_avoidance(g):
    base = plan(g, "p_L1", "p_O3", "blind")
    first_edge = (base.nodes[1], base.nodes[2]) if len(base.nodes) > 2 else (base.nodes[0], base.nodes[1])
    a, b = g.nodes[first_edge[0]], g.nodes[first_edge[1]]
    hz = {"hazards": [{"x": (a.x + b.x) / 2, "y": (a.y + b.y) / 2, "floor": a.floor, "radius": 0.3,
                       "kind": "protruding_object", "note": "wall sconce at head height"}]}
    g2 = walkgraph.build(building(), hz)
    r = plan(g2, "p_L1", "p_O3", "blind")
    hazard_edges = [g2.edge(x, y) for x, y in zip(r.nodes, r.nodes[1:]) if "hazard" in g2.edge(x, y).tags]
    assert not hazard_edges or r.cost < base.cost + 40


def test_snap_respects_walls(g):
    # standing in the kitchen right behind the lobby wall: snap to a kitchen pano, not the closer lobby one
    nid, _ = snap(g, 2.0, 6.4, "f1")
    assert g.nodes[nid].room_label == "Kitchen"


def test_navigator_walks_to_arrival(g):
    nav = Navigator(g, "Conference Room", "wheelchair")
    r = plan(g, "p_K3", "Conference Room", "wheelchair")
    k3 = g.nodes["p_K3"]
    ev = run(nav, walk(g, r, step=0.4, wobble=0.3), start=(k3.x, k3.y, k3.floor, -1.57))
    kinds = [e["type"] for _, e in ev]
    assert kinds[0] == "route" and "orient" in kinds
    assert "arrive" in kinds and "reroute" not in kinds, kinds
    assert "floor" in [m["type"] for m in nav.ms]


def test_navigator_reroutes_when_user_wanders(g):
    nav = Navigator(g, "Office", "blind")
    r = plan(g, "p_K3", "Office", "blind")
    pts = list(walk(g, r, step=0.4))
    # walk a bit, then wander to the far corner of the lobby
    path = pts[:6] + [(1.0, 1.0, "f1", 0.0)] * 3
    k3 = g.nodes["p_K3"]
    ev = run(nav, path, start=(k3.x, k3.y, k3.floor, None))
    assert any(e["type"] == "reroute" for _, e in ev)
    # the new route starts from the lobby corner
    assert g.nodes[nav.route.nodes[0]].room_label == "Lobby"
    # and can still be followed to the end
    ev2 = [e for p in walk(g, nav.route, step=0.4) for e in nav.update(*p)]
    assert any(e["type"] == "arrive" for e in ev2)


def test_block_elevator(g):
    e1 = g.nodes["p_E1"]
    nav = Navigator(g, "Conference Room", "wheelchair")
    nav.start(e1.x, e1.y - 2, "f1")
    ev = nav.block(e1.x, e1.y - 2, "f1", "p_E1", why="The elevator is out of service.")
    assert ev[0]["type"] == "no_route"  # wheelchair can't take the stairs
    nav = Navigator(g, "Conference Room", "blind")
    nav.start(e1.x, e1.y - 2, "f1")
    ev = nav.block(e1.x, e1.y - 2, "f1", "p_E1", why="The elevator is out of service.")
    assert ev[0]["type"] == "reroute"
    assert "p_S1" in nav.route.nodes


def test_no_route_errors(g):
    with pytest.raises(NoRoute):
        plan(g, "p_K3", "Swimming Pool", "blind")
