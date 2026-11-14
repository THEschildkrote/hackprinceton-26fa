"""the hackathon venue (rabinowitz hall, princeton): real scan, floors by elevation, joined foyer, real stairs."""
import shutil

import pytest

from nudge import buildings, walkgraph
from nudge.mp import ROOT

SID = "R1FX6EmdcDt"
MODEL = ROOT / "cache" / "models" / f"{SID}.json"
REAL_DOSSIER = ROOT / "cache" / "dossiers" / SID

pytestmark = pytest.mark.skipif(not MODEL.exists(), reason="venue scan not cached (needs the matterport key once)")


@pytest.fixture(scope="module")
def g():
    """the venue graph with its real dossier copied into the tests' own dossier folder."""
    from nudge.agents import tools as T
    if REAL_DOSSIER.exists():
        shutil.copytree(REAL_DOSSIER, T.DOSSIERS / SID, dirs_exist_ok=True)
    T._graph_cached.cache_clear()
    return T.graph(SID)


def test_floors_follow_elevation_not_pano_gaps(g):
    # the lowest matterport floor is an unscanned basement; panos must land on ground and upper, not shift down
    counts = {}
    for n in g.nodes.values():
        counts[n.floor] = counts.get(n.floor, 0) + 1
    assert set(counts) == {"pd0i1c4yq9w0y2zsb07ed23sb", "h0ysdfsrzyt6errbx1x8g92wc"}, counts
    assert sorted(counts.values()) == [30, 36]
    labels = {f["id"]: f["label"] for f in g.floors}
    assert labels["h0ysdfsrzyt6errbx1x8g92wc"] == "the upper floor"  # spoken names from reference/extra_models.csv


def test_venue_is_one_connected_piece(g):
    assert len(set(g.components().values())) == 1, g.warnings


@pytest.mark.skipif(not (REAL_DOSSIER / "annotations.json").exists(), reason="no venue dossier yet")
def test_floors_join_through_the_real_stairs(g):
    vertical = [e for e in g.edges() if "vertical" in e.tags]
    assert vertical and all(e.tags & {"stairs", "elevator"} for e in vertical)
    assert {"stairs", "elevator"} <= {t for e in vertical for t in e.tags}
    assert not any("vertical_unknown" in e.tags for e in vertical)


def test_building_search_finds_the_venue():
    for q in ("rabinowitz", "princeton", "Rabinowitz Hall"):
        assert buildings.search(q)[0]["sid"] == SID, q
    assert SID in [b["sid"] for b in buildings.featured()[:2]]
    assert buildings.label(SID) == "Rabinowitz Hall, Princeton"


def test_demo_building_graph_unchanged_by_elevation_rule():
    # the elevation rule only overrides the gap split when panos would sit >2.5 m from their floor
    p = ROOT / "cache" / "models" / "MgGNVWsyXtH.json"
    if not p.exists():
        pytest.skip("demo scan not cached")
    from nudge.fetch import load
    m = load("MgGNVWsyXtH")
    with_elev = walkgraph.build(m)
    no_elev = walkgraph.build({**m, "floors": [{k: v for k, v in f.items() if k != "elevation_m"} for f in m["floors"]]})
    assert {n: x.floor for n, x in with_elev.nodes.items()} == {n: x.floor for n, x in no_elev.nodes.items()}


@pytest.mark.skipif(not (REAL_DOSSIER / "places").exists() and not (REAL_DOSSIER / "places.json").exists(),
                    reason="venue places not indexed yet")
def test_named_places_resolve_and_route_across_floors(g):
    from nudge import places
    from nudge.router import plan
    entrance = next(n for n in g.nodes.values() if n.label == "27")
    here = (entrance.x, entrance.y, entrance.floor)
    assert places.resolve(g, "take me to the stairs", here=here)["candidates"][0]["name"] == "Grand staircase"
    sem = next(p for p in places.load(SID)["places"] if p["id"] == "zone:seminar")
    goal = set(places.goal_nodes(sem))
    blind = plan(g, entrance.id, goal, "blind")
    chair = plan(g, entrance.id, goal, "wheelchair")
    tags = lambda r: {t for a, b in zip(r.nodes, r.nodes[1:]) for t in g.edge(a, b).tags}
    assert "stairs" in tags(blind) and "elevator" in tags(chair) and "stairs" not in tags(chair)
