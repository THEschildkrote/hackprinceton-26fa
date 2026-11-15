"""The routes that ship with the runtime, and the short one made for a crowded room."""
from pathlib import Path

import pytest

from nudge.config import load_config
from nudge.eventlog import EventLog
from nudge.follower import State
from nudge.navigator import Navigator
from nudge.replay import replay
from nudge.route import list_routes, parse_route, read_route_file
from nudge.sensors.sim import drive, script_events

ROOT = Path(__file__).resolve().parent.parent
START = "heading 70\nwait 1\ncmd calibrate\ncmd start\n"


def walk(script, route="demo_3m_right"):
    cfg, _ = load_config(str(ROOT / "config.toml"))                 # the settings the Pi runs
    nav = Navigator(cfg, EventLog(keep=True))
    nav.set_route(read_route_file(ROOT / "routes" / f"{route}.json"), name=route)
    drive(nav, script_events(START + script))
    taps = [(r["kind"], r.get("side")) for r in nav.log.records if r["type"] == "tap"]
    return nav, taps


@pytest.mark.parametrize("name", list_routes(ROOT / "routes"))
def test_every_shipped_route_loads(name):
    assert len(parse_route(read_route_file(ROOT / "routes" / f"{name}.json"))) >= 2


def test_the_short_demo_is_three_metres_a_right_turn_and_a_step_and_a_half():
    assert parse_route(read_route_file(ROOT / "routes" / "demo_3m_right.json")) == [(0.0, 0.0), (0.0, 3.0), (1.5, 3.0)]


def test_walked_straight_it_is_quiet_then_says_turn_right_then_ends():
    nav, taps = walk("step 6\nturn 90 1.2\nstep 2\nwait 1")
    assert nav.follower.state is State.ARRIVED
    assert taps[-1] == ("arrived", None)
    cue = taps[:-1]
    assert cue and all(side == "right" for _, side in cue) and ("double_tap", "right") in cue   # the turn, and only the turn
    first = next(r for r in nav.log.records if r["type"] == "tap")
    at = min((r for r in nav.log.records if r["type"] == "pose"), key=lambda r: abs(r["t"] - first["t"]))
    assert 1.9 <= at["y"] <= 3.0                                     # the cue comes within a metre of the 3 m mark
    assert replay(nav.log.records).ok


@pytest.mark.parametrize("veer, back", [(35, "left"), (-35, "right")])
def test_veering_off_the_line_is_tapped_back_before_the_turn(veer, back):
    nav, taps = walk(f"step 2\nturn {veer} 0.8\nstep 1\nwait 1.2\nturn {-veer} 0.8\nstep 3\nturn 90 1.2\nstep 2\nwait 1")
    assert nav.follower.state is State.ARRIVED
    assert taps[0] == ("tap", back)                                  # one tap, on the side to come back to
    assert ("double_tap", "right") in taps and taps[-1] == ("arrived", None)


def test_walking_past_the_mark_keeps_saying_turn_right():
    nav, taps = walk("step 9\nwait 2")
    assert nav.follower.state is State.NAVIGATING
    assert taps.count(("double_tap", "right")) >= 3
