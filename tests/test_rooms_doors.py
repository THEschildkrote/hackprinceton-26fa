"""room changes: doorway waypoints, door widths, wall clearance, room and door cues, door steps on the device."""
import math

import pytest

from nudge import walkgraph
from nudge.device import encode_step, route_packet
from nudge.geom import dist, project
from nudge.instruct import door_points, maneuvers
from nudge.navigator import Navigator
from nudge.router import NoRoute, plan
from nudge.sim import walk
from tests.synthetic import building


@pytest.fixture(scope="module")
def g():
    return walkgraph.build(building())


def test_route_goes_through_door_middle_square_on(g):
    # k1 (6.5, 7) -> l6 (6.5, 5.2) crosses the 1 m kitchen door centred at (6.5, 6)
    pts = g.edge_points("p_K1", "p_L6")
    assert (6.5, 6.0) in pts
    assert g.edge_doors("p_K1", "p_L6")[0]["width"] == 1.0
    # l2 (5, 1.5) -> o1 (9.5, 2.5) crosses the lobby/office door at x=8 off-centre; the line is pulled to its
    # middle (8, 2.5) and crosses square-on: the points just before and after are on the door's normal
    pts = g.edge_points("p_L2", "p_O1")
    i = pts.index((8.0, 2.5))
    assert pts[i - 1] == (7.5, 2.5) and pts[i + 1] == (8.5, 2.5)
    # still clear of every wall, and the length is the polyline's
    assert all(not g.blocked_line(a, b, "f1") for a, b in zip(pts, pts[1:]))
    assert abs(g.edge("p_L2", "p_O1").length - sum(dist(a, b) for a, b in zip(pts, pts[1:]))) < 1e-9


def test_narrow_door_wheelchair_hard_stop_blind_hears_it():
    g = walkgraph.build(building(kitchen_door_w=0.6))
    e = g.edge("p_K1", "p_L6")
    assert e is not None and "narrow_door" in e.tags and e.doors[0]["width"] == 0.6
    with pytest.raises(NoRoute):
        plan(g, "p_L1", "Kitchen", "wheelchair")  # 0.7 m < 32 in clear width
    r = plan(g, "p_L1", "Kitchen", "blind")
    text = " ".join(m["text"] for m in maneuvers(g, r))
    assert "narrow doorway" in text


def test_wide_door_not_called_narrow(g):
    r = plan(g, "p_L1", "Kitchen", "blind")
    text = " ".join(m["text"] for m in maneuvers(g, r))
    assert "through a doorway" in text and "narrow" not in text


def _hall(stub_end_y, panos_y):
    """one 10 x 4 m hall with a free-standing wall stub from (5, 0) up to (5, stub_end_y)."""
    V = [((0, 0), "a"), ((10, 0), "b"), ((10, 4), "c"), ((0, 4), "d"), ((5, 0), "s0"), ((5, stub_end_y), "s1")]
    verts = [{"id": vid, "position": {"x": x, "y": y}} for (x, y), vid in V]
    E = [("e0", "a", "b"), ("e1", "b", "c"), ("e2", "c", "d"), ("e3", "d", "a"), ("es", "s0", "s1")]
    edges = [{"id": eid, "type": "wall", "vertices": [{"id": u}, {"id": v}]} for eid, u, v in E]
    return {"id": "hall", "floors": [{"id": "f1", "label": "Floor 1", "sequence": 0, "vertices": verts, "edges": edges}],
            "rooms": [{"id": "r", "floor": {"id": "f1"}, "classifications": [{"label": "Hallway"}],
                       "boundary": {"edges": [{"id": "e0"}, {"id": "e1"}, {"id": "e2"}, {"id": "e3"}]}}],
            "panoLocations": [{"id": "pa", "label": "A", "position": {"x": 2, "y": panos_y, "z": 1.5}, "floor": {"id": "f1"}},
                              {"id": "pb", "label": "B", "position": {"x": 8, "y": panos_y, "z": 1.5}, "floor": {"id": "f1"}}]}


def test_line_brushing_a_wall_end_is_nudged_clear():
    # straight a-b passes 0.2 m over the stub's end at (5, 2.8)
    g = walkgraph.build(_hall(2.8, 3.0))
    e = g.edge("pa", "pb")
    assert e.via, "expected a clearance waypoint"
    assert e.min_clear >= walkgraph.CLEAR_M and "tight" not in e.tags
    pts = g.edge_points("pa", "pb")
    stub = ((5, 0), (5, 2.8))
    closest = min(min(project(p, *stub)[2] for p in [pa, pb]) for pa, pb in zip(pts, pts[1:]))
    assert closest >= 0  # sanity
    # every walking point keeps its distance from the stub's end
    for a, b in zip(pts, pts[1:]):
        for t in [i / 20 for i in range(21)]:
            s = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            if dist(s, (2, 3.0)) > 0.25 and dist(s, (8, 3.0)) > 0.25:
                assert project(s, *stub)[2] >= walkgraph.CLEAR_M - 1e-6


def test_line_hugging_a_wall_is_tight():
    # both panos 0.15 m from the top wall: no nudge keeps the whole line 0.35 m off it
    g = walkgraph.build(_hall(1.0, 3.85))
    e = g.edge("pa", "pb")
    assert "tight" in e.tags and e.min_clear < walkgraph.CLEAR_M


def _events(g, start, goal, profile="blind", ann_wobble=0.0, step=0.3):
    r = plan(g, start, goal, profile)
    nav = Navigator(g, goal, profile)
    s = g.nodes[start]
    out = list(nav.start(s.x, s.y, s.floor, 0.0))
    for x, y, f, h in walk(g, r, step=step, wobble=ann_wobble):
        out += nav.update(x, y, f, h)
        if nav.done:
            break
    return nav, out


def test_room_and_door_cues_on_the_way_out_of_the_kitchen(g):
    nav, ev = _events(g, "p_K3", "Office")
    rooms = [e["room"] for e in ev if e["type"] == "room"]
    assert rooms == ["Lobby", "Office"], [e["text"] for e in ev]
    doors = [e for e in ev if e["type"] == "door"]
    assert len(doors) == 2 and all(e["text"].startswith("Doorway ahead") for e in doors)
    # each door cue comes before entering the room behind it
    kinds = [e["type"] for e in ev]
    assert kinds.index("door") < kinds.index("room")
    assert any(e["type"] == "arrive" for e in ev) and nav.reroutes == 0


def test_room_cue_reads_the_entry_landmark():
    g0 = walkgraph.build(building())
    r = plan(g0, "p_K3", "Office", "blind")
    entry = next(n for n in r.nodes if g0.nodes[n].room_label == "Lobby")
    ann = {"nodes": {entry: {"landmarks": [{"text": "carpet changes to tile", "sense": "underfoot"}]}}}
    g = walkgraph.build(building(), ann)
    _, ev = _events(g, "p_K3", "Office")
    lobby = next(e for e in ev if e["type"] == "room" and e["room"] == "Lobby")
    assert lobby["text"] == "Entering the lobby. Carpet changes to tile."


def test_narrow_door_cue():
    g = walkgraph.build(building(kitchen_door_w=0.6))
    _, ev = _events(g, "p_K3", "Lobby")
    assert any(e["type"] == "door" and e["text"].startswith("Narrow doorway ahead") for e in ev)


def test_door_points_and_device_door_steps(g):
    r = plan(g, "p_K3", "Conference Room", "blind")
    dp = door_points(g, r)
    assert [round(d["width"], 1) for d in dp] == [1.0, 1.0, 2.0, 2.0]
    assert all(a["along"] <= b["along"] for a, b in zip(dp, dp[1:]))
    pk = route_packet(g, r, maneuvers(g, r))
    door_steps = [p for p in pk if p.get("door")]
    assert len(door_steps) == len(dp) and all(p["act"] == "straight" and p["deg"] == 0 for p in door_steps)
    for p in pk:
        line = encode_step(p)
        assert len(line.encode()) < 20
    # walking metres between steps add up to the route's walking length (0.05 rounding per step)
    walk_m = sum(g.edge(a, b).length for a, b in zip(r.nodes, r.nodes[1:]) if "vertical" not in g.edge(a, b).tags)
    assert abs(sum(p["m"] for p in pk) - walk_m) <= 0.05 * len(pk)


def test_live_bearing_points_at_the_door_middle(g):
    # standing at l2 (5, 1.5), facing +x, heading for the office through the door at (8, 2.5):
    # the lookahead sits on the polyline toward (7.5, 2.5), slightly left, never at the jamb
    nav = Navigator(g, "p_O1", "blind")
    l2 = g.nodes["p_L2"]
    nav.start(l2.x, l2.y, "f1", heading=0.0)
    lv = nav.live()
    want = round(math.degrees(math.atan2(2.5 - 1.5, 7.5 - 5.0)))
    assert 0 < lv["bearing_to_next_deg"] <= want


def test_block_the_pano_you_stand_on_reroutes(g):
    # coordinator fix: the blocked pano is never a start, even when you're standing on it
    l4 = g.nodes["p_L4"]
    nav = Navigator(g, "Office", "blind")
    nav.start(l4.x, l4.y, "f1")
    ev = nav.block(l4.x, l4.y, "f1", "p_L4", why="Something is in the way.")
    assert ev[0]["type"] == "reroute", ev
    assert "p_L4" not in nav.route.nodes and nav.route.nodes[0] != "p_L4"
