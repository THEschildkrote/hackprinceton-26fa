import json
import math
import shutil

import pytest

from nudge import handoff, outdoor, vexcel
from nudge.georef import ENU, outline, register
from nudge.router import plan
from tests.synthetic_outdoor import TH, TX, TY, Block, oid


@pytest.fixture
def blk(monkeypatch):
    b = Block()
    monkeypatch.setattr(vexcel, "TRANSPORT", b.transport)
    monkeypatch.setattr(outdoor, "OVERPASS_TRANSPORT", b.overpass_transport)
    return b


@pytest.fixture
def og(blk):
    return outdoor.build(LAT, LON, footprint_lonlat=vexcel.footprint(LAT, LON))


LAT, LON = 34.05, -118.25


def tags(g, a, b):
    return g.edge(oid(a), oid(b)).tags


# vexcel client

def test_vexcel_requests_follow_the_spec(blk):
    fp = vexcel.footprint(LAT, LON)
    assert len(fp) >= 4
    method, path, params, _ = blk.calls[-1]
    assert (method, path) == ("GET", "/structure/geometry/extract")
    assert params["wkt"] == f"POINT({LON} {LAT})" and params["primary-structure"] == "true"
    zs = vexcel.elevation([blk.lonlat("s2"), blk.lonlat("e0"), blk.lonlat("p")])
    assert zs[0] == pytest.approx(0.0, abs=1e-6) and zs[1] == pytest.approx(0.1, abs=1e-3)
    _, path, params, _ = blk.calls[-1]
    assert path == "/dtm/elevation" and params["wkt"].startswith("LINESTRING(") and params["dem-priority"] == vexcel.DEM
    dets = vexcel.detect(LAT, LON, ["pole", "crosswalk"])
    assert {d["label"] for d in dets} == {"pole", "crosswalk"}
    _, path, _, body = blk.calls[-1]
    assert path == "/elements/detect" and body["model-input"]["elements"][0]["label"] == "pole"
    assert vexcel.oblique_image(LAT, LON, "south")[:2] == b"\xff\xd8"
    _, path, params, _ = blk.calls[-1]
    assert params["product-type"] == "oblique-south"


# georef

def test_georef_recovers_the_transform(blk):
    fp_enu = [blk.enu.to_enu(*p) for p in blk.footprint_lonlat]
    geo = register(outline(blk.gi), fp_enu, enu=blk.enu)
    err = abs((geo.theta_deg - TH + 180) % 360 - 180)
    assert err < 3.0, geo.to_dict()
    assert math.dist((geo.tx, geo.ty), (TX, TY)) < 0.5, geo.to_dict()
    assert geo.ok and not geo.mirrored and geo.rmse_m < 0.3
    # round trips
    e, n = geo.model_to_enu(3.5, -0.8)
    assert math.dist((e, n), blk.model_to_enu(3.5, -0.8)) < 0.5
    x, y = geo.lonlat_to_model(*blk.lonlat("e0"))
    assert math.dist((x, y), (3.5, -0.8)) < 0.5


def test_georef_flags_symmetric_footprints(blk):
    fp_enu = [blk.enu.to_enu(*p) for p in blk.footprint_lonlat]
    assert not register(outline(blk.gi), fp_enu, enu=blk.enu).ambiguous
    # a plain rectangle fits at theta and theta + 180: say so instead of guessing
    rect = [(0, 0), (20, 0), (20, 8), (0, 8)]
    c, s = math.cos(math.radians(40)), math.sin(math.radians(40))
    fp = [(c * x - s * y + 5, s * x + c * y - 3) for x, y in rect]
    geo = register(rect, fp)
    assert geo.ambiguous and geo.alternatives
    assert any(abs((a["theta_deg"] - geo.theta_deg) % 360 - 180) < 5 for a in geo.alternatives)


def test_enu_round_trip():
    enu = ENU(29.76, -95.37)
    lon, lat = enu.to_lonlat(120.0, -45.0)
    e, n = enu.to_enu(lon, lat)
    assert math.dist((e, n), (120.0, -45.0)) < 1e-6


# outdoor graph

def test_outdoor_tags_from_osm_dtm_and_detections(og):
    assert {"crossing", "unsignalised", "curb"} <= tags(og, "c1", "k1")
    assert "curb" in tags(og, "k1", "s1")
    assert {"crossing", "signals"} <= tags(og, "c2", "k2") and "unsignalised" not in tags(og, "c2", "k2")
    assert og.edge(oid("k2"), oid("s3")).info.get("curb_ramp") and "curb" not in tags(og, "k2", "s3")
    assert "steep" in tags(og, "s3", "p") and "steep" in tags(og, "p", "e0")
    assert not tags(og, "s2", "e0") & {"steep", "too_steep"}
    assert og.edge(oid("s2"), oid("e0")).info["rise_m"] == pytest.approx(0.1, abs=0.01)
    assert "hazard" in tags(og, "s2", "s3")  # the pole
    assert "marked_crossing" in tags(og, "c2", "k2")
    assert "entrance" in og.nodes[oid("e0")].tags
    assert all(n.floor == outdoor.FLOOR for n in og.nodes.values())


def names(route):
    rev = {oid(k): k for k in ("f0", "f1", "f2", "c1", "k1", "c2", "k2", "s1", "s2", "s3", "s4", "e0", "p")}
    return [rev[n] for n in route.nodes]


def test_wheelchair_avoids_steep_and_curb_blind_takes_shorter(og):
    wc = plan(og, oid("f0"), oid("e0"), outdoor.profile("wheelchair"))
    bl = plan(og, oid("f0"), oid("e0"), outdoor.profile("blind"))
    assert "k1" not in names(wc) and "p" not in names(wc), names(wc)
    assert names(wc)[-3:] == ["s3", "s2", "e0"]
    assert "k1" not in names(bl) and "p" in names(bl), names(bl)
    assert bl.length < wc.length
    for r in (wc, bl):
        for a, b in zip(r.nodes, r.nodes[1:]):
            assert "curb" not in og.edge(a, b).tags


def test_outdoor_text_mentions_crossing_and_slope(og):
    from nudge.instruct import maneuvers
    r = plan(og, oid("f0"), oid("e0"), outdoor.profile("wheelchair"))
    ms = outdoor.describe(og, r, maneuvers(og, r))
    text = " ".join(m["text"] for m in ms)
    assert "cross the street at the signal" in text.lower()
    assert "gentle ramp up" in text.lower()
    assert "meter" in text and "feet" not in text


# handoff

@pytest.fixture
def site(blk, og):
    fp_enu = [blk.enu.to_enu(*p) for p in blk.footprint_lonlat]
    geo = register(outline(blk.gi), fp_enu, enu=blk.enu)
    s = handoff.Site("synthetic", blk.gi, og, geo)
    s.portals = handoff.entrance_candidates(blk.gi, geo, og, "synthetic")
    handoff.register_site(s)
    yield s
    handoff._SITES.pop("synthetic", None)


def test_entrance_candidates(site):
    best = site.best_portal()
    assert best["outdoor_node"] == oid("e0") and best["kind"] == "entrance"
    assert site.gi.nodes[best["pano_id"]].room_label == "Lobby"
    assert best["step"] is False and best["confirmed"] is False and best["faces"] in ("south", "east", "west", "north")
    assert set(best) >= {"outdoor_node", "sid", "pano_id", "kind", "width_m", "step", "confirmed", "evidence"}


def test_door_to_door_plan(site, blk):
    res = handoff.plan_door_to_door(blk.latlon("f0"), "synthetic", "Conference Room", "blind", site=site)
    legs = [m["leg"] for m in res["maneuvers"]]
    assert legs[0] == "outdoor" and "entrance" in legs and legs[-1] == "synthetic"
    tr = next(m for m in res["maneuvers"] if m["type"] == "transition")
    assert tr["text"].startswith("You're at the main entrance.") and "no step" in tr["text"].lower()
    assert "Going inside." in tr["text"]
    assert res["maneuvers"][-1]["type"] == "arrive"
    text = " ".join(m["text"] for m in res["maneuvers"] if m.get("text"))
    assert "meter" in text and "feet" not in text and "steps," not in text
    for line in res["outdoor"]["packets"] + res["indoor"]["packets"]:
        assert len(line.encode()) < 20, line
    assert res["outdoor"]["packets"][-1].split(",")[1] == "A"   # band hears arrive at the door
    assert res["indoor"]["packets"][0].split(",")[1] == "D"     # then a fresh depart inside
    assert res["length_m"] == pytest.approx(res["outdoor"]["length_m"] + res["indoor"]["length_m"], abs=0.2)
    assert "steep slope down" in res["outdoor"]["maneuvers"][-1]["text"].lower()  # notes survive the rename
    wc = handoff.plan_door_to_door(blk.latlon("f0"), "synthetic", "Conference Room", "wheelchair", site=site)
    assert wc["outdoor"]["maneuvers"][-1]["text"].startswith("Walk 3 meters to the main entrance.")
    assert "gentle ramp up" in wc["outdoor"]["maneuvers"][-1]["text"].lower()


def test_door_to_door_live_walk(site, blk):
    from nudge.sim import walk
    nav = handoff.DoorToDoorNavigator(site, "Conference Room", "wheelchair")
    ev = nav.start(*blk.latlon("f0"))
    assert ev[0]["type"] == "route" and ev[0]["phase"] == "outdoor"
    seen = []
    for x, y, _, _ in walk(site.og, nav.outside.route, step=0.8):
        lon, lat = site.og.enu.to_lonlat(x, y)
        seen += nav.update(lat=lat, lon=lon)
        if nav.phase == "indoor":
            break
    assert any(e["type"] == "transition" for e in seen), [e["type"] for e in seen]
    assert nav.inside is not None
    for x, y, fl, _ in walk(site.gi, nav.inside.route, step=0.5):
        seen += nav.update(x=x, y=y, floor=fl)
        if nav.done:
            break
    assert nav.done and seen[-1]["type"] == "arrive"


def test_confirm_entrance_tool_only_accepts_candidates(site):
    from nudge.agents.tools import dossier_dir
    d = dossier_dir("synthetic")
    try:
        bad = json.loads(handoff.confirm_entrance.invoke({"sid": "synthetic", "pano_id": "nope"}))
        assert "error" in bad
        good_id = site.portals[0]["pano_id"]
        ok = json.loads(handoff.confirm_entrance.invoke({"sid": "synthetic", "pano_id": good_id,
                                                         "note": "level automatic doors"}))
        assert ok["ok"] and ok["best"]["confirmed"] and ok["best"]["pano_id"] == good_id
        cands = json.loads(handoff.entrance_candidates_tool.invoke({"sid": "synthetic"}))
        assert cands["candidates"][0]["confirmed"]
        blocks = handoff.facade_view.invoke({"sid": "synthetic", "candidate": 0})
        assert [b["type"] for b in blocks] == ["text", "image"]
    finally:
        (d / "entrances.json").unlink(missing_ok=True)
