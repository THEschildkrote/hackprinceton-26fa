"""doorways in scans that don't type them: wall gaps, point-cloud doorways, and the real building."""
import json
import pathlib

import numpy as np
import pytest

from nudge import walkgraph

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL = ROOT / "cache" / "models" / "MgGNVWsyXtH.json"


def two_rooms(wall_pieces, sid="syn-doors"):
    """rooms A (0..4) and B (4..8) on one floor; wall_pieces are the segments on the shared line x=4."""
    V, E = {}, []

    def v(p):
        k = f"v{p[0]}_{p[1]}"
        V[k] = {"id": k, "position": {"x": p[0], "y": p[1]}}
        return k

    def edge(a, b):
        eid = f"e{len(E)}"
        E.append({"id": eid, "type": "wall", "vertices": [{"id": v(a)}, {"id": v(b)}]})
        return {"id": eid}

    a_edges = [edge((4, 0), (0, 0)), edge((0, 0), (0, 4)), edge((0, 4), (4, 4))]
    b_edges = [edge((4, 0), (8, 0)), edge((8, 0), (8, 4)), edge((8, 4), (4, 4))]
    shared = [edge(p, q) for p, q in wall_pieces]
    rooms = [{"id": "rA", "floor": {"id": "f1"}, "classifications": [{"label": "Office"}],
              "boundary": {"edges": a_edges + shared}},
             {"id": "rB", "floor": {"id": "f1"}, "classifications": [{"label": "Lobby"}],
              "boundary": {"edges": b_edges + shared}}]
    return {"id": sid, "name": sid, "floors": [{"id": "f1", "label": "Floor 1", "sequence": 0,
                                               "vertices": list(V.values()), "edges": E}],
            "rooms": rooms,
            "panoLocations": [{"id": "pA", "label": "A", "position": {"x": 2.0, "y": 2.0, "z": 1.5}, "floor": {"id": "f1"}},
                              {"id": "pB", "label": "B", "position": {"x": 6.0, "y": 2.0, "z": 1.5}, "floor": {"id": "f1"}}]}


def test_wall_gap_is_a_door_with_its_width():
    g = walkgraph.build(two_rooms([((4, 0), (4, 1.55)), ((4, 2.45), (4, 4))]), use_doorways=False)
    e = g.edge("pA", "pB")
    assert e is not None and "door" in e.tags and "room_change" in e.tags
    assert len(e.doors) == 1 and e.doors[0]["width"] == pytest.approx(0.9, abs=0.01)
    assert e.doors[0]["x"] == pytest.approx(4.0) and e.doors[0]["y"] == pytest.approx(2.0)
    assert "narrow_door" not in e.tags  # 0.9 m clears the 0.815 m ada width


def test_narrow_gap_is_flagged_for_wheelchairs():
    g = walkgraph.build(two_rooms([((4, 0), (4, 1.65)), ((4, 2.35), (4, 4))]), use_doorways=False)
    e = g.edge("pA", "pB")
    assert e.doors[0]["width"] == pytest.approx(0.7, abs=0.01) and "narrow_door" in e.tags


def test_open_plan_boundary_is_not_a_door():
    g = walkgraph.build(two_rooms([]), use_doorways=False)
    e = g.edge("pA", "pB")
    assert e is not None and "door" not in e.tags and not e.doors
    assert any(w[2] == "open_plan" for w in g.walls("f1"))


def wall_cloud(gap=(1.55, 2.45)):
    """dense points on every wall face, nothing in the doorway on x=4 between gap[0] and gap[1]."""
    rng = np.random.default_rng(0)
    pts = []
    for (x0, y0), (x1, y1) in [((0, 0), (8, 0)), ((0, 4), (8, 4)), ((0, 0), (0, 4)), ((8, 0), (8, 4)), ((4, 0), (4, 4))]:
        t = rng.random(20000)
        x, y = x0 + t * (x1 - x0), y0 + t * (y1 - y0)
        z = rng.random(20000) * 2.5
        keep = ~((x == 4) & (y > gap[0]) & (y < gap[1])) if x0 == x1 == 4 else np.ones_like(t, bool)
        pts.append(np.stack([x[keep], y[keep], z[keep]], 1))
    floor = rng.random((20000, 2)) * [8, 4]
    pts.append(np.column_stack([floor, np.zeros(len(floor))]))
    return np.concatenate(pts).astype(np.float32)


def test_cloud_finds_a_doorway_in_a_wall_typed_separator(tmp_path, monkeypatch):
    # the real-scan case: the shared edge is one continuous "wall", the cloud shows the opening
    model = two_rooms([((4, 0), (4, 4))], sid="syn-cloud")
    g = walkgraph.build(model, use_doorways=False)
    assert g.edge("pA", "pB") is None or "uncertain" in g.edge("pA", "pB").tags
    found = walkgraph.cloud_doorways(g, wall_cloud(), {"f1": (0.0, 2.6)})["f1"]
    doors = [d for d in found if d["kind"] == "door"]
    assert len(doors) == 1 and doors[0]["width"] == pytest.approx(0.9, abs=0.08) and not doors[0]["exterior"]
    monkeypatch.setattr(walkgraph, "DOORWAYS", tmp_path)
    (tmp_path / "syn-cloud.json").write_text(json.dumps({"floors": {"f1": found}}), encoding="utf-8")
    g2 = walkgraph.build(model)
    e = g2.edge("pA", "pB")
    assert e is not None and "uncertain" not in e.tags and "door" in e.tags
    assert e.doors[0]["width"] == pytest.approx(0.9, abs=0.08)


@pytest.mark.skipif(not REAL.exists(), reason="real scan not cached (needs Matterport keys)")
def test_real_building_doors_and_connectivity():
    m = json.loads(REAL.read_text(encoding="utf-8"))
    if walkgraph.load_doorways("MgGNVWsyXtH") is None:
        cloud = ROOT / "cache" / "clouds" / "MgGNVWsyXtH_s10.npy"
        if not cloud.exists():
            pytest.skip("no point cloud cached")
        walkgraph.compute_doorways("MgGNVWsyXtH", model=m, pts=np.load(cloud))
    g = walkgraph.build(m)
    doors = [d for e in g.edges() for d in e.doors]
    assert len({(d["x"], d["y"]) for d in doors}) >= 10
    assert len(set(g.components().values())) == 1
    widths = [d["width"] for d in doors]
    assert all(0.45 <= w <= walkgraph.OPEN_PLAN_M for w in widths)


@pytest.mark.skipif(not (REAL.exists() and (ROOT / "cache" / "vexcel").exists() and (ROOT / "cache" / "osm").exists()),
                    reason="needs the real scan plus cached vexcel/osm responses")
def test_real_building_has_a_street_facing_entrance():
    from nudge import handoff
    site = handoff.load_site("MgGNVWsyXtH", refresh=True)
    assert site.portals, site.warnings
    front = site.portals[0]
    assert front["onto_entry_room"] and front["faces"] == "east"  # 1255 s atlantic blvd faces atlantic, east
