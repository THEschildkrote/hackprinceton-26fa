"""the loop between the navigator and the servo band.

runs on a virtual clock, so a full two-floor walk takes no real time.
"""
import math

import pytest

from nudge import walkgraph
from nudge.band import RANK, NullBand, REST_DEG
from nudge.bandrun import BandRunner, run_walk
from nudge.navigator import Navigator
from nudge.router import plan
from nudge.sim import walk
from tests.synthetic import building

GOAL = "Conference Room"


@pytest.fixture(scope="module")
def g():
    return walkgraph.build(building())


def rig(g, goal=GOAL, profile="blind", wobble=0.0, seed=0):
    """a runner over the synthetic building, plus the positions of a walk to the goal."""
    start = "p_K3"
    route = plan(g, start, goal, profile)
    s = g.nodes[start]
    band = NullBand()
    runner = BandRunner(Navigator(g, goal, profile), band=band)
    pts = list(walk(g, route, step=0.5, wobble=wobble, seed=seed))
    return runner, band, pts, (s.x, s.y, s.floor, 0.0)


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


MANEUVERS = {c for c, rank in RANK.items() if rank == 1}


def cues_played(g, pace_ms, wobble=0.0, seed=0):
    """the cue names that actually started, in order."""
    runner, _, pts, start = rig(g, wobble=wobble, seed=seed)
    played, real = [], runner.engine.play

    def spy(cue, now, force=False):
        ok = real(cue, now, force)
        if ok:
            played.append(cue)
        return ok

    runner.engine.play = spy
    run_walk(runner, pts, start=start, pace_ms=pace_ms, virtual=True)
    assert runner.nav.done
    return played


def test_a_fast_walk_loses_steering_before_maneuvers(g):
    """the whole point of RANK. a servo renders ~3 cues a second; when positions outrun
    it something must go, and it must be the advisory nudges, not the turns."""
    played = cues_played(g, pace_ms=100)
    man = sum(1 for c in played if c in MANEUVERS)
    steer = sum(1 for c in played if c.startswith("steer"))
    assert "arrive" in played, "never announced arrival"
    assert man > steer, f"gave up maneuvers before steering: {played}"
    assert man >= 3, f"only {man} maneuvers survived: {played}"


def test_a_drifting_walker_keeps_their_turns(g):
    """the case RANK exists for, and the one that actually bites.

    a straight-line walk barely generates steering taps, so nothing collides and ranking
    changes nothing. a walker who drifts - which is everyone - generates plenty, and at
    speed they collide with turn cues. measured over these eight seeds at 100 ms a step:
    without ranking 20 turn cues land against 22 steering taps; with it, 32 against 19.
    so `turns > steering` separates the two, and fails if RANK is ever flattened.
    """
    turns = steer = 0
    for seed in range(8):
        played = cues_played(g, pace_ms=100, wobble=0.6, seed=seed)
        turns += sum(1 for c in played if c in MANEUVERS)
        steer += sum(1 for c in played if c.startswith("steer"))
    assert turns > steer, f"gave up turn cues to steering taps: {turns} turns, {steer} steering"


def test_ranking_is_inert_when_nothing_collides(g):
    """at walking pace there is room for every cue, so ranking must not change anything."""
    played = cues_played(g, pace_ms=420, wobble=0.6, seed=0)
    assert any(c.startswith("steer") for c in played), "steering should survive at walking pace"
    assert any(c in MANEUVERS for c in played)


def test_even_a_brutal_pace_still_turns_and_arrives(g):
    """positions ten times faster than anyone walks. the servo is the limit here, not
    the logic, so this only claims that navigation cues are what is left."""
    played = cues_played(g, pace_ms=40)
    assert "arrive" in played
    assert any(c in MANEUVERS for c in played), f"no maneuver survived at all: {played}"


def test_walking_pace_gets_everything(g):
    played = cues_played(g, pace_ms=420)
    assert sum(1 for c in played if c in MANEUVERS) >= 5
    assert any(c.startswith("steer") for c in played), "steering should survive at walking pace"


def test_slower_pace_gives_the_engine_more_room(g):
    """more ms per step means more ticks, so nothing gets cut short."""
    counts = []
    for pace in (200, 800):
        runner, band, pts, start = rig(g)
        run_walk(runner, pts, start=start, pace_ms=pace, virtual=True)
        counts.append(strokes(band))
    assert counts[1] >= counts[0]
