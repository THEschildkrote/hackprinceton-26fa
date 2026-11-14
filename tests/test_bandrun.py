"""the loop between the navigator and the servo band.

runs on a virtual clock, so a full two-floor walk takes no real time.
"""
import math

import pytest

from nudge import walkgraph
from nudge.band import NullBand, REST_DEG
from nudge.bandrun import BandRunner, run_walk
from nudge.navigator import Navigator
from nudge.router import plan
from nudge.sim import walk
from tests.synthetic import building

GOAL = "Conference Room"


@pytest.fixture(scope="module")
def g():
    return walkgraph.build(building())


def rig(g, goal=GOAL, profile="blind"):
    """a runner over the synthetic building, plus the positions of a walk to the goal."""
    start = "p_K3"
    route = plan(g, start, goal, profile)
    s = g.nodes[start]
    band = NullBand()
    runner = BandRunner(Navigator(g, goal, profile), band=band)
    return runner, band, list(walk(g, route, step=0.5)), (s.x, s.y, s.floor, 0.0)


def strokes(band):
    """how many times a shoulder was tapped, counting rising edges off rest."""
    n, out = 0, False
    for _, left, right in band.commands:
        now_out = any(v is not None and v > REST_DEG for v in (left, right))
        if now_out and not out:
            n += 1
        out = now_out
    return n


# --- the walk -----------------------------------------------------------------


def test_a_walk_arrives_and_taps(g):
    runner, band, pts, start = rig(g)
    run_walk(runner, pts, start=start, virtual=True)
    assert runner.nav.done, "never reached the goal"
    assert strokes(band) > 0, "walked the whole route without tapping anyone"


def test_a_position_is_not_a_tick(g):
    """the point of the loop: positions arrive at walking pace, strokes render in ms."""
    fed, band_fed, pts, start = rig(g)
    fed.start(*start, now=0)
    for i, p in enumerate(pts):
        fed.update(*p, now=i * 420)
    assert band_fed.commands == [], "servos moved without anyone ticking the engine"

    ticked, band_ticked, pts2, start2 = rig(g)
    run_walk(ticked, pts2, start=start2, virtual=True)
    assert strokes(band_ticked) > 0


def test_the_band_is_left_detached(g):
    runner, band, pts, start = rig(g)
    run_walk(runner, pts, start=start, virtual=True)
    assert not runner.busy, "a pattern was still playing when the walk ended"
    assert band.commands[-1][1:] == (None, None), "left a servo engaged at the end"


def test_arrive_plays_out_instead_of_being_cut_off(g):
    runner, band, pts, start = rig(g)
    run_walk(runner, pts, start=start, virtual=True)
    # arrive is three strokes on both shoulders; they must be the last thing that happens
    tail = [c for c in band.commands if c[1] is not None or c[2] is not None][-3:]
    assert len(tail) == 3
    for _, left, right in tail:
        assert left is not None and right is not None, "arrive should use both shoulders"


# --- events -------------------------------------------------------------------


def test_events_come_back_paired_with_positions(g):
    runner, _, pts, start = rig(g)
    seen = run_walk(runner, pts, start=start, virtual=True)
    assert seen, "no events at all"
    kinds = {e["type"] for _, e in seen}
    assert "route" in kinds and "arrive" in kinds
    for p, e in seen:
        assert len(p) == 4, "an event lost its position"
        assert "text" in e


def test_on_event_sees_the_same_events(g):
    runner, _, pts, start = rig(g)
    heard = []
    seen = run_walk(runner, pts, start=start, virtual=True,
                    on_event=lambda p, e: heard.append(e["type"]))
    assert heard == [e["type"] for _, e in seen]


# --- going wrong ---------------------------------------------------------------


def test_drifting_off_route_is_felt(g):
    """shove the walker 4 m sideways partway along, the way cli.simulate does."""
    runner, band, pts, start = rig(g)
    at = len(pts) // 3
    x, y, f, h = pts[at]
    dx, dy = 4 * math.sin(h), -4 * math.cos(h)
    pts = pts[:at] + [(x + dx, y + dy, f, h)] * 4 + pts[at:]

    seen = run_walk(runner, pts, start=start, virtual=True)
    assert any(e["type"] == "off" or e.get("cue") == "off_route" for _, e in seen), \
        "walked 4 m off the route without noticing"
    assert strokes(band) > 0


def test_an_empty_walk_does_nothing(g):
    runner, band, _, _ = rig(g)
    assert run_walk(runner, [], virtual=True) == []
    assert band.commands == []


# --- pacing --------------------------------------------------------------------


def test_slower_pace_gives_the_engine_more_room(g):
    """more ms per step means more ticks, so nothing gets cut short."""
    counts = []
    for pace in (200, 800):
        runner, band, pts, start = rig(g)
        run_walk(runner, pts, start=start, pace_ms=pace, virtual=True)
        counts.append(strokes(band))
    assert counts[1] >= counts[0]
