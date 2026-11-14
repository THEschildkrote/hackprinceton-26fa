"""outdoor leg without vexcel: osm footprint + usgs elevation, outside houston/la or when vexcel refuses."""
import json
from pathlib import Path

import pytest

from nudge import handoff, opendata, outdoor, vexcel
from nudge.agents import tools as T
import tests.synthetic_outdoor as so

PRINCETON = (40.3490893, -74.6554659)


def _block(monkeypatch, tmp_path, where):
    """the synthetic city block moved to `where`: vexcel refuses, osm building + footways, usgs elevation."""
    monkeypatch.setattr(so, "LAT0", where[0])
    monkeypatch.setattr(so, "LON0", where[1])
    b = so.Block()

    def overpass(q):
        if "building" in q:
            ring = [{"lon": x, "lat": y} for x, y in b.footprint_lonlat + b.footprint_lonlat[:1]]
            return {"elements": [{"type": "way", "id": 9001, "tags": {"building": "university", "name": "Test Hall"},
                                  "geometry": ring}]}
        return b.osm()

    def no_vexcel(method, path, params, body):
        b.calls.append((method, path))
        return 403, "application/json", b'{"status":403,"message":"Unauthorized by Geofencing permission"}'

    monkeypatch.setattr(outdoor, "OVERPASS_TRANSPORT", overpass)
    monkeypatch.setattr(vexcel, "TRANSPORT", no_vexcel)
    monkeypatch.setattr(opendata, "USGS_TRANSPORT", lambda lon, lat: 60.0 + b.elevation([(lon, lat)])[0])
    monkeypatch.setattr(opendata, "USGS_CACHE", tmp_path / "usgs")
    from tests.synthetic import building
    import nudge.fetch as fetch

    def load(sid, refresh=False):
        m = building()
        m["geolocation"] = {"lat": str(where[0]), "long": str(where[1])}
        return m
    monkeypatch.setattr(fetch, "load", load)
    return b


@pytest.fixture
def princeton_block(monkeypatch, tmp_path):
    T._graph_cached.cache_clear()
    handoff._SITES.pop("synthetic-princeton", None)
    yield _block(monkeypatch, tmp_path, PRINCETON)
    T._graph_cached.cache_clear()
    handoff._SITES.pop("synthetic-princeton", None)


def test_coverage_boxes():
    assert opendata.in_vexcel_coverage(34.05, -118.25) and opendata.in_vexcel_coverage(29.76, -95.37)
    assert not opendata.in_vexcel_coverage(*PRINCETON)


def test_princeton_site_comes_from_open_data(princeton_block):
    site = handoff.load_site("synthetic-princeton", refresh=True)
    assert site.source == "osm+usgs"
    assert site.og.footprint and len(site.og.footprint) >= 4
    assert any("openstreetmap" in w for w in site.warnings)
    # no vexcel call was made outside its coverage
    assert not [c for c in princeton_block.calls if c[0] in ("GET", "POST")]
    assert site.geo.rmse_m < 1.0, site.geo.to_dict()
    assert site.portals, "an entrance candidate"
    p = site.portals[0]
    assert p["confirmed"] is False and p["evidence"].get("basis")
    # usgs elevation reached the outdoor graph
    assert any("grade_max" in e.info for e in site.og.edges())


def test_princeton_door_to_door(princeton_block):
    site = handoff.load_site("synthetic-princeton", refresh=True)
    start = princeton_block.latlon(next(iter(so.NODES)))
    plan = handoff.plan_door_to_door(start, "synthetic-princeton", "Conference Room", "blind", site=site)
    assert plan["outdoor"]["length_m"] > 0 and plan["indoor"]["length_m"] > 0
    assert "not confirmed from imagery" in plan["transition"]["text"]
    handoff._SITES.pop("R1FX6EmdcDt", None)
    T._graph_cached.cache_clear()
    texts = [m["text"] for m in plan["maneuvers"] if m.get("text")]
    assert any("meter" in t for t in texts) and not any(" feet" in t or " ft" in t for t in texts), texts


def test_vexcel_refusal_inside_coverage_falls_back(monkeypatch):
    def forbidden(method, path, params, body):
        return 403, "application/json", b'{"status":403}'
    monkeypatch.setattr(vexcel, "TRANSPORT", forbidden)
    monkeypatch.setattr(opendata, "footprint", lambda lat, lon, radius_m=80: [(0, 0), (0, 1e-4), (1e-4, 1e-4), (0, 0)])
    ring, src, note = opendata.footprint_any(34.05, -118.25)
    assert src == "osm" and "vexcel footprint unavailable" in note and len(ring) == 4


def test_vexcel_403_in_la_falls_back_to_open_data(monkeypatch, tmp_path):
    T._graph_cached.cache_clear()
    b = _block(monkeypatch, tmp_path, (34.05, -118.25))
    try:
        site = handoff.load_site("synthetic-la-403", refresh=True)
        assert site.source == "osm+usgs"
        assert any("vexcel footprint unavailable" in w for w in site.warnings)
        assert any(c[0] == "GET" or c[0] == "POST" for c in b.calls)  # vexcel was asked first, and refused
        assert site.portals and any("grade_max" in e.info for e in site.og.edges())
    finally:
        T._graph_cached.cache_clear()
        handoff._SITES.pop("synthetic-la-403", None)


def test_usgs_outside_the_us_means_no_slopes(monkeypatch, tmp_path):
    monkeypatch.setattr(opendata, "USGS_TRANSPORT", lambda lon, lat: None)
    monkeypatch.setattr(opendata, "USGS_CACHE", tmp_path)
    el = opendata.USGSElevation(51.5, -0.12)
    with pytest.raises(RuntimeError):
        el([(-0.12, 51.5), (-0.1201, 51.5001)])


def test_partial_scan_registers_inside_a_bigger_footprint():
    """a scan of the stem of a t-shaped building: the hull fit fails, the wall fit finds it."""
    import math
    from nudge.georef import register, register_partial
    T_ = [(19, 0), (31, 0), (31, 20), (50, 20), (50, 35), (0, 35), (0, 20), (19, 20)]
    th, tx, ty = math.radians(30), 12.0, -7.0

    def to_enu(p):
        return (math.cos(th) * p[0] - math.sin(th) * p[1] + tx, math.sin(th) * p[0] + math.cos(th) * p[1] + ty)
    fp = [to_enu(p) for p in T_] + [to_enu(T_[0])]
    # model frame = building frame shifted, so the scan's own origin is arbitrary
    walls = [((19, 0), (31, 0)), ((19, 0), (19, 20)), ((31, 0), (31, 20)), ((19, 18), (23, 18)),
             ((25, 18), (31, 18)), ((25, 0), (25, 10)), ((19, 10), (23, 10))]
    walls = [((a[0] - 25, a[1] - 9), (b[0] - 25, b[1] - 9)) for a, b in walls]
    hull = [(-6, -9), (6, -9), (6, 11), (-6, 11)]
    assert not register(hull, fp).ok or register(hull, fp).inlier_frac < 0.6
    geo = register_partial(walls, fp)
    e, n = geo.model_to_enu(-6, -9)
    assert math.dist((e, n), to_enu((19, 0))) < 1.0, geo.to_dict()
    assert abs((geo.theta_deg - 30 + 180) % 360 - 180) < 2.0
    assert geo.ok and geo.source == "footprint-partial"


def test_open_data_cli_flag_forces_osm_inside_coverage(monkeypatch):
    monkeypatch.setattr(opendata, "FORCE", True)
    monkeypatch.setattr(opendata, "footprint", lambda lat, lon, radius_m=80: [(0, 0), (0, 1e-4), (1e-4, 1e-4), (0, 0)])
    ring, src, note = opendata.footprint_any(34.05, -118.25)
    assert src == "osm"
    assert opendata.elevation_for(34.05, -118.25)[1] == "usgs"


def _venue_cached():
    osm, usgs = Path("cache/osm"), Path("cache/usgs")
    return (osm.exists() and any(osm.iterdir()) and Path("cache/models/R1FX6EmdcDt.json").exists()
            and usgs.exists() and any(usgs.glob("*.json"))
            and Path("cache/dossiers/R1FX6EmdcDt/places.json").exists())


@pytest.mark.skipif(not _venue_cached(), reason="needs the cached rabinowitz scan, osm and usgs responses")
def test_rabinowitz_hall_outdoor_site(monkeypatch):
    """princeton, no vexcel: osm footprint, usgs slopes, the scan's walls fitted inside the h-shaped footprint,
    and the entrance arcade on the washington road side, where an osm footway meets the facade."""
    from nudge import building
    from nudge.mp import ROOT
    # the place catalog names the "Entrance arcade"; tests otherwise get an empty dossier folder (read only here)
    monkeypatch.setattr(T, "DOSSIERS", ROOT / "cache" / "dossiers")
    monkeypatch.setattr(building, "DOSSIERS", ROOT / "cache" / "dossiers")
    handoff._SITES.pop("R1FX6EmdcDt", None)
    T._graph_cached.cache_clear()
    site = handoff.load_site("R1FX6EmdcDt", refresh=True)
    assert site.source == "osm+usgs"
    assert site.geo.source.startswith("footprint-partial") and site.geo.ok and site.geo.rmse_m < 1.5
    front = site.portals[0]
    assert front["room"] == "Entrance arcade" and front["faces"] == "west"
    assert front["evidence"] == {"basis": "indoor entrance zone"} and front["confirmed"] is False
    start = (40.3493295, -74.6561356)  # osm node 8948919411, washington road footway, ~54 m from the door
    plan = handoff.plan_door_to_door(start, "R1FX6EmdcDt", "Grand staircase", "blind", site=site)
    assert 40 < plan["outdoor"]["length_m"] < 120 and plan["indoor"]["length_m"] > 0
    assert "not confirmed from imagery" in plan["transition"]["text"]
    handoff._SITES.pop("R1FX6EmdcDt", None)
    T._graph_cached.cache_clear()
