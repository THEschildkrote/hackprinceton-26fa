"""A loop of corridors, on the settings the belt ships with.

In a corridor the walls decide where the wearer turns: at the real corner,
whatever the step count says. So the walker here is scripted: so many real
steps down each corridor, then the turn. What is being checked is that the
belt's idea of where they are survives a step length that is wrong by a
fifth, over legs far longer than anything in the other tests.
"""
from pathlib import Path

import pytest

from nudge.config import Config, load_config
from nudge.eventlog import EventLog
from nudge.follower import State
from nudge.navigator import Navigator
from nudge.replay import replay
from nudge.sensors.sim import drive, script_events

ROOT = Path(__file__).resolve().parent.parent
LOOP = {"name": "corridors", "waypoints": [[0, 0], [0, 24], [18, 24], [18, 0], [0, 0]]}     # 84 m, four right turns


def walk(step_m, cfg):
    """Once round the loop with steps of step_m: 24 m, 18 m, 24 m, 18 m, turning right where the walls do."""
    long_leg, short_leg = round(24 / step_m), round(18 / step_m)
    script = (f"heading 130\nwait 0.5\ncmd calibrate\ncmd start\n"
              f"step {long_leg}\nturn 90 1.2\nstep {short_leg}\nturn 90 1.2\n"
              f"step {long_leg}\nturn 90 1.2\nstep {short_leg}\nwait 1")
    nav = Navigator(cfg, EventLog(keep=True))
    nav.set_route(LOOP)
    issued = drive(nav, script_events(script))
    return nav, issued


def shipped():
    cfg, _ = load_config(str(ROOT / "config.toml"))
    return cfg


def events(nav, name):
    return [r for r in nav.log.records if r["type"] == name]


def test_the_shipped_settings_are_the_corridor_ones():
    d = shipped().deadreckon
    assert d.turn_window_frac == 0.2 and d.calibrate_step_length and d.calibrate_min_leg_m == 12
    assert d.step_length_min_m <= 0.4 <= d.step_length_m


@pytest.mark.parametrize("step_m", [0.6, 0.5, 0.4])
def test_once_round_the_loop_with_the_step_length_out_by_a_fifth_either_way(step_m):
    nav, issued = walk(step_m, shipped())
    assert nav.follower.state is State.ARRIVED and issued[-1].kind == "arrived"
    assert len(events(nav, "turn_snap")) == 3                          # every corner put the position right
    assert nav.loc.step_length_m == pytest.approx(step_m, rel=0.03)    # and the first corridor measured the step
    assert (nav.pose.x, nav.pose.y) == pytest.approx((0.0, 0.0), abs=1.0)
    assert replay(nav.log.records).ok


def test_a_step_that_is_longer_than_set_is_caught_at_the_first_corner():
    nav, _ = walk(0.6, shipped())
    first = events(nav, "turn_snap")[0]
    assert first["moved_m"] == pytest.approx(4.0, abs=0.3)             # the count had them 4 m short of the corner
    assert events(nav, "step_length")[0]["new_m"] == pytest.approx(0.6)
    assert all(e["moved_m"] < 1.0 for e in events(nav, "turn_snap")[1:])       # after that it stays close


def test_a_step_that_is_shorter_than_set_means_early_turn_taps_on_the_first_corridor_only():
    nav, _ = walk(0.4, shipped())
    corners = [e["t"] for e in events(nav, "turn_snap")]
    taps = [r for r in nav.log.records if r["type"] == "tap" and r["kind"] in ("tap", "double_tap")]
    early = [r for r in taps if r["t"] < corners[0]]
    assert early and all(r["side"] == "right" for r in early)         # told to turn before the corner was there
    on_time = [r for r in taps if corners[1] - 3 <= r["t"] < corners[1]]
    assert on_time and all(r["side"] == "right" for r in on_time)     # the next corner's taps come at the corner
    assert not [r for r in taps if corners[0] + 3 < r["t"] < corners[1] - 3]   # and the corridor before it is quiet


def test_without_the_corridor_settings_the_first_corner_is_missed_and_the_walk_is_lost():
    old = Config.from_dict({"deadreckon": {"step_length_m": 0.5}})    # the 3 m window, no step measuring
    nav, _ = walk(0.6, old)
    assert nav.follower.state is State.NAVIGATING                      # never arrives
    assert not events(nav, "turn_snap")
    assert nav.pose.y < 21 and nav.pose.x < 1                          # still pinned on the first corridor


def test_short_legs_are_untouched_by_the_corridor_settings():
    cfg = shipped()
    nav = Navigator(cfg, EventLog(keep=True))
    nav.set_route({"waypoints": [[0, 0], [0, 5], [5, 5]]})
    assert nav.loc._turn_window(0) == cfg.deadreckon.turn_window_m     # a 5 m leg: the plain 3 m window
    drive(nav, script_events("heading 0\nwait 0.5\ncmd calibrate\ncmd start\nstep 10\nturn 90 1.2\nstep 9\nwait 1"))
    assert nav.follower.state is State.ARRIVED
    assert not events(nav, "step_length")                              # too short a leg to measure a step on
