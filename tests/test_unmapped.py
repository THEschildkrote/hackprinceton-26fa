"""off the map: no invented turns, a way back along your own steps, or stop; the device flag."""
import math

import pytest

from nudge import walkgraph
from nudge.device import decode_live, encode_live, live_packet
from nudge.navigator import STOP_TEXT, Navigator
from tests.synthetic import building


@pytest.fixture
def g():
    return walkgraph.build(building())


def nav_at(g, x, y, floor="f1", goal="Office", heading=math.pi):
    nav = Navigator(g, goal, "blind")
    nav.start(x, y, floor, heading)
    return nav


def test_walk_off_the_map_and_back(g):
    nav = nav_at(g, 1.0, 3.0)  # lobby, west side
    assert not nav.unmapped
    # out through the west wall of the lobby: nothing mapped out there
    ev = nav.update(-1.0, 3.0, "f1", math.pi)
    assert len(ev) == 1 and ev[0]["type"] == "unmapped" and ev[0]["can_return"]
    assert "walk back about 2 meters" in ev[0]["text"]
    assert nav.unmapped
    # deeper in: still no turn instructions of any kind
    ev = nav.update(-3.0, 3.0, "f1", math.pi)
    assert ev == []
    lv = nav.live()
    assert lv["unmapped"] and lv["distance_to_next_m"] == 4.0
    # the way back is behind you: +-180 when facing west
    assert abs(lv["bearing_to_next_deg"]) == 180
    p = live_packet(nav)
    s = encode_live(p)
    assert s.endswith(",4") and len(s) < 20 and decode_live(s)["unmapped"]
    # retrace: back on the map, then a fresh route from here
    assert nav.update(-1.0, 3.0, "f1", 0.0) == []
    ev = nav.update(1.0, 3.0, "f1", 0.0)
    assert [e["type"] for e in ev][:2] == ["remapped", "reroute"]
    assert not nav.unmapped and not nav.live()["unmapped"]


def test_jump_off_the_map_means_stop(g):
    nav = nav_at(g, 1.0, 3.0)
    ev = nav.update(40.0, 40.0, "f1")
    assert ev[0]["type"] == "unmapped" and not ev[0]["can_return"] and ev[0]["text"] == STOP_TEXT
    lv = nav.live()
    assert lv["unmapped"] and lv["distance_to_next_m"] is None and lv["bearing_to_next_deg"] is None


def test_unknown_floor_is_unmapped(g):
    nav = nav_at(g, 1.0, 3.0)
    ev = nav.update(1.0, 3.0, "f7")
    assert ev[0]["type"] == "unmapped" and ev[0]["why"] == "floor" and not ev[0]["can_return"]


def test_start_off_the_map(g):
    nav = Navigator(g, "Office", "blind")
    ev = nav.start(-5.0, -5.0, "f1")
    assert ev == [{"type": "unmapped", "text": STOP_TEXT, "cue": "unmapped", "can_return": False}]
    assert nav.route is None and nav.live()["unmapped"]


def test_wandering_too_far_gives_up_on_retracing(g):
    nav = nav_at(g, 1.0, 3.0)
    nav.update(-1.0, 3.0, "f1")
    ev = []
    y = 3.0
    for _ in range(80):  # pace back and forth well past the 30 m trail limit
        y = -3.0 if y > 0 else 3.0
        ev += nav.update(-2.0, y, "f1")
    assert any(e["type"] == "unmapped" and not e["can_return"] for e in ev)
    assert nav.way_back() is None


def test_inside_a_room_but_far_from_route_is_still_on_map(g):
    # kitchen corner, route goes elsewhere: on the map (in a room) -> plain off-route reroute, not unmapped
    nav = nav_at(g, 9.5, 2.5, goal="Office")
    ev = nav.update(1.0, 11.0, "f1") + nav.update(1.0, 11.0, "f1")
    assert not nav.unmapped and any(e["type"] == "reroute" for e in ev)


def test_hazard_flag_on_device(g):
    ann = {"hazards": [{"x": 4.0, "y": 9.0, "floor": "f1", "radius": 1.0, "kind": "protruding_object",
                        "note": "sconce"}]}
    g2 = walkgraph.build(building(), ann)
    k3 = g2.nodes["p_K3"]
    nav = Navigator(g2, "Office", "blind")
    nav.start(k3.x, k3.y, "f1", 0.0)
    nav.update(k3.x + 0.5, k3.y - 0.5, "f1", 0.0)
    p = live_packet(nav)
    assert p["hazard"] and decode_live(encode_live(p))["hazard"]
