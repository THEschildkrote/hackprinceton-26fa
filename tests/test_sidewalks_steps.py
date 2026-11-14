"""inferred sidewalks where osm only maps street centrelines, raised-entry step estimates, and entrance
confirmations that feed the portal (wheelchair refusal, spoken steps)."""
import math
import types

import pytest

from nudge import handoff, outdoor
from nudge.mp import ROOT
from nudge.router import NoRoute, plan

LAT0, LON0 = 34.0, -118.0
M = 111_320


def ll(x, y):
    return {"lat": LAT0 + y / M, "lon": LON0 + x / (M * math.cos(math.radians(LAT0)))}


def way(wid, nodes, pts, tags):
    return {"type": "way", "id": wid, "nodes": nodes, "geometry": [ll(*p) for p in pts], "tags": tags}


@pytest.fixture
def street(monkeypatch):
    """a primary street along y=0 with a residential side street going south at x=0 (a t junction), no mapped
    sidewalks; the building sits north of the primary."""
    monkeypatch.setattr(outdoor, "OVERPASS_TRANSPORT", lambda q: {"elements": []})
    osm = {"elements": [
        way(1, [10, 11, 12, 13], [(-80, 0), (-40, 0), (0, 0), (60, 0)],
            {"highway": "primary", "name": "Main Street", "lanes": "4"}),
        way(2, [12, 20], [(0, 0), (0, -60)], {"highway": "residential", "name": "Side Street"}),
    ]}
    return outdoor.build(LAT0, LON0, osm=osm, elevation=lambda pts: [0.0] * len(pts), detections=[])


def test_inferred_sidewalks_keep_a_same_side_walk_crossing_free(street):
    g = street
    hw = {(e.info or {}).get("highway") for e in g.edges()}
    assert "primary" not in hw and "residential" not in hw, "the centrelines are not walked on"
    assert any("sidewalk_inferred" in e.tags for e in g.edges())
    # 4 lanes -> 6.6 m half width + 1.5 m: the north sidewalk sits about 8.1 m north of the centreline
    north = [n for n in g.nodes.values() if n.id.startswith("sw:") and n.y > 0]
    assert north and all(abs(n.y - 8.1) < 0.6 for n in north)
    a = min(north, key=lambda n: math.dist(n.xy, (-70, 8)))
    b = min(north, key=lambda n: math.dist(n.xy, (50, 8)))
    r = plan(g, a.id, b.id, outdoor.profile("blind"))
    assert not any("crossing" in g.edge(x, y).tags for x, y in zip(r.nodes, r.nodes[1:]))
    # crossing to the south side happens only at the junction, and is named
    south = min((n for n in g.nodes.values() if n.id.startswith("sw:") and n.y < 0), key=lambda n: math.dist(n.xy, (-70, -8)))
    r2 = plan(g, a.id, south.id, outdoor.profile("blind"))
    crossings = [g.edge(x, y) for x, y in zip(r2.nodes, r2.nodes[1:]) if "crossing" in g.edge(x, y).tags]
    assert crossings and all(e.info.get("cross_road") == "Main Street" for e in crossings)
    assert all("unsignalised" in e.tags for e in crossings)


def test_dangling_footway_joins_the_inferred_sidewalk(monkeypatch):
    monkeypatch.setattr(outdoor, "OVERPASS_TRANSPORT", lambda q: {"elements": []})
    osm = {"elements": [
        way(1, [10, 11], [(-50, 0), (50, 0)], {"highway": "residential", "name": "Elm"}),
        way(2, [30, 31], [(5, 25), (5, 6)], {"highway": "footway"}),  # a path from a door that stops short of the curb
    ]}
    g = outdoor.build(LAT0, LON0, osm=osm, elevation=lambda pts: [0.0] * len(pts), detections=[])
    assert any("connector" in e.tags for e in g.adj["out:31"].values())


def fake_gi(entry_z, low_z):
    nodes = {"e": types.SimpleNamespace(floor="f2", z=entry_z), "l": types.SimpleNamespace(floor="f1", z=low_z)}
    return types.SimpleNamespace(floors=[{"id": "f1"}, {"id": "f2"}], nodes=nodes, sid=None)


def test_raised_entry_floor_gives_a_step_estimate():
    gi = fake_gi(1.2, -1.5)  # floors 2.7 m apart, entry on the upper one
    s = handoff._step_info(types.SimpleNamespace(elevation=None), (0, 0), None, gi, "f2")
    assert s["step"] is True and abs(s["rise_m"] - 0.89) < 0.02 and s["risers"] == 5
    assert "estimated" in s["step_basis"]
    # entry on the lowest floor: no raised-floor evidence, the dtm decides (here: no dtm -> unknown)
    s = handoff._step_info(types.SimpleNamespace(elevation=None), (0, 0), None, gi, "f1")
    assert s["step"] == "unknown" and s["rise_m"] is None


def portal(**kw):
    return {"pano_id": "p1", "outdoor_node": "o1", "width_m": 0.51, "step": "unknown", "confirmed": False,
            "evidence": {}, "room": None, **kw}


def test_confirmed_steps_block_wheelchair_with_a_clear_message():
    site = handoff.Site("x", None, None, None, portals=[portal()])
    rec = handoff.confirmation_record("p1", accessible=True, note="5 curved steps up, curved railing, no ramp",
                                      steps=5, ramp=False, direction="west")
    assert rec["accessible"] is False and rec["step"] is True and rec["risers"] == 5
    handoff.apply_confirmations(site, [rec])
    assert site.best_portal("blind")["pano_id"] == "p1"
    with pytest.raises(NoRoute, match="about 5 steps and no ramp; ask for the accessible entrance or call ahead"):
        site.best_portal("wheelchair")
    t = handoff.transition_text(site.portals[0])
    assert "There are about 5 steps up with a curved railing, then the door, 0.5 meters wide." in t
    assert "not confirmed" not in t


def test_railing_only_when_the_note_says_so_and_ramp_keeps_wheelchair():
    site = handoff.Site("x", None, None, None, portals=[portal()])
    handoff.apply_confirmations(site, [handoff.confirmation_record("p1", note="three steps", steps=3, ramp=True)])
    t = handoff.transition_text(site.portals[0])
    assert "about 3 steps up, and a ramp" in t and "railing" not in t
    assert site.best_portal("wheelchair")["pano_id"] == "p1"
    # an unconfirmed estimate says "probably" and doesn't block anyone
    est = portal(step=True, risers=5, rise_m=0.89)
    assert "There are probably about 5 steps up" in handoff.transition_text(est)
    assert not handoff.blocks_wheelchair(est)


REAL = ROOT / "cache" / "models" / "MgGNVWsyXtH.json"


@pytest.mark.skipif(not (REAL.exists() and (ROOT / "cache" / "vexcel").exists() and (ROOT / "cache" / "osm").exists()
                         and (ROOT / "cache" / "doorways" / "MgGNVWsyXtH.json").exists()),
                    reason="real building caches missing (needs Matterport + Vexcel keys)")
def test_real_atlantic_walk_stays_on_the_building_side():
    site = handoff.load_site("MgGNVWsyXtH", refresh=True)
    og, p = site.og, site.portals[0]
    # a start on the inferred atlantic sidewalk about 18 m south of the door, building (west) side
    atl = [n for n in og.nodes.values() if (n.id.startswith("sw:") or "~" in n.id) and n.label.startswith("South Atlantic")]
    dx, dy = p["door_enu"]
    st = min((n for n in atl if dx - 2 < n.x < dx + 25), key=lambda n: math.dist(n.xy, (dx + 10, dy - 18)))
    r = plan(og, st.id, p["outdoor_node"], outdoor.profile("blind"))
    assert not any("crossing" in og.edge(a, b).tags for a, b in zip(r.nodes, r.nodes[1:]))
    assert r.length < 40
    assert p["step"] is True and p["risers"] >= 3
