"""several scans of one building joined by confirmed portals; each scan keeps its own frame."""
import json
import shutil

import pytest

from nudge import building as B
from nudge.device import encode_step, route_packet
from nudge.instruct import maneuvers
from nudge.navigator import Navigator
from nudge.router import plan
from nudge.sim import walk

SIDS = ("synthetic-lobby", "synthetic")
NOTE = "the elevator bank is at the back of the lobby, on the right"


@pytest.fixture
def clean():
    for s in SIDS:
        shutil.rmtree(B.DOSSIERS / s, ignore_errors=True)
    yield
    for s in SIDS:
        shutil.rmtree(B.DOSSIERS / s, ignore_errors=True)


@pytest.fixture(scope="module")
def graphs():
    return {s: B.scan_graph(s) for s in SIDS}


def elevator_portal(graphs):
    c = next(c for c in B.candidates_portals(list(graphs), graphs) if c["b"] == "synthetic:p_E1")
    return {**c, "confirmed": True, "note": NOTE}


def test_siblings_and_floor_numbers():
    assert {s["sid"] for s in B.siblings("synthetic")} == set(SIDS)
    assert B.floor_number("35th Floor - 30,799sqft") == 35
    assert B.floor_number("Level 2") == 2 and B.floor_number("Lobby") == 0 and B.floor_number("Suite 400") is None
    real = B.siblings("zYdLbVFphGC")  # a real houston office floor; at least itself
    assert any(s["sid"] == "zYdLbVFphGC" for s in real)


def test_candidates_rank_elevators_first(graphs):
    cands = B.candidates_portals(list(graphs), graphs)
    top = cands[0]
    assert top["kind"] == "elevator" and top["a"] == "synthetic-lobby:p_EL" and top["direction"] == "up"
    assert {c["b"] for c in cands if c["kind"] == "elevator"} == {"synthetic:p_E1", "synthetic:p_E2"}
    assert all(c["a"].split(":")[0] != c["b"].split(":")[0] for c in cands)


def test_merge_keeps_frames_apart(graphs):
    g = B.merge(graphs, [elevator_portal(graphs)])
    assert g.frame("synthetic-lobby:g") == "synthetic-lobby" and g.frame("synthetic:f1") == "synthetic"
    assert len(g.nodes) == sum(len(x.nodes) for x in graphs.values())
    e = g.edge("synthetic-lobby:p_EL", "synthetic:p_E1")
    assert {"portal", "vertical", "elevator"} <= e.tags and e.length == 8.0
    # no other link crosses scans
    for x in g.edges():
        if "portal" not in x.tags:
            assert x.a.split(":")[0] == x.b.split(":")[0]


def test_route_and_walk_from_street_to_conference_room(graphs):
    g = B.merge(graphs, [elevator_portal(graphs)])
    r = plan(g, "synthetic-lobby:p_V1", "Conference Room", "wheelchair")
    assert r.nodes[0] == "synthetic-lobby:p_V1" and g.nodes[r.nodes[-1]].room_label == "Conference Room"
    ms = maneuvers(g, r)
    floors = [m for m in ms if m["type"] == "floor"]
    assert len(floors) == 1, "lobby elevator and the building's elevator are one ride"
    assert floors[0]["portal"] and floors[0]["text"].startswith("Take the elevator up to") and NOTE in floors[0]["text"].lower()
    # device: every step still precise metres in one scan's frame, lines < 20 bytes
    for p in route_packet(g, r, ms):
        assert len(encode_step(p).encode()) < 20
    # walk it: positions come in as (x, y, floor, sid)
    nav = Navigator(g, "Conference Room", "wheelchair")
    v1 = g.nodes["synthetic-lobby:p_V1"]
    ev = list(nav.start(v1.x, v1.y, "g", 0.0, sid="synthetic-lobby"))
    for x, y, f, h in walk(g, r, step=0.4):
        sid, floor = f.split(":", 1)
        ev += nav.update(x, y, floor, h, sid=sid)
        if nav.done:
            break
    kinds = [e["type"] for e in ev]
    assert "arrive" in kinds and "reroute" not in kinds and "unmapped" not in kinds, [e["text"] for e in ev]
    rooms = [e["room"] for e in ev if e["type"] == "room"]
    assert rooms[:2] == ["Lobby", "Elevator"] and rooms[-1] == "Conference Room"


def test_way_back_down_says_down(graphs):
    g = B.merge(graphs, [elevator_portal(graphs)])
    r = plan(g, "synthetic:p_K3", "synthetic-lobby:p_V1", "blind")
    floor = next(m for m in maneuvers(g, r) if m["type"] == "floor")
    assert floor["text"].startswith("Take the elevator down to Lobby")


def test_building_graph_reads_confirmed_portals(clean, graphs):
    p = elevator_portal(graphs)
    d = B.DOSSIERS / "synthetic"
    d.mkdir(parents=True, exist_ok=True)
    (d / "portals.json").write_text(json.dumps({"portals": [p], "rejected": []}), encoding="utf-8")
    g = B.building_graph("synthetic-lobby")
    assert "synthetic:p_E1" in g.nodes and g.edge("synthetic-lobby:p_EL", "synthetic:p_E1")
    # unconfirmed portals don't count
    (d / "portals.json").write_text(json.dumps({"portals": [{**p, "confirmed": False}]}), encoding="utf-8")
    g = B.building_graph("synthetic-lobby")
    assert set(n.split(":")[0] for n in g.nodes) == {"synthetic-lobby"}


def test_off_the_map_between_scans_is_unmapped(graphs):
    # the lobby scan alone, user walks out past its north wall where no scan exists
    g = B.merge({"synthetic-lobby": graphs["synthetic-lobby"]}, [])
    nav = Navigator(g, "Elevator", "blind")
    nav.start(6.0, 1.6, "g", 0.0, sid="synthetic-lobby")
    assert nav.update(6.0, 5.5, "g", sid="synthetic-lobby") == [] or not nav.unmapped
    ev = nav.update(6.0, 7.5, "g", sid="synthetic-lobby")
    assert ev and ev[0]["type"] == "unmapped" and ev[0]["can_return"]
