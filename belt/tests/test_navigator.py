from pathlib import Path

import pytest

from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.follower import Action, State
from nudge.navigator import Navigator, NavError
from nudge.sensors.base import SensorSample
from nudge.sensors.sim import drive, script_events

ROOT = Path(__file__).resolve().parent.parent
L_ROUTE = {"waypoints": [[0, 0], [0, 5], [5, 5]]}


def make(waypoints=L_ROUTE, **overrides):
    cfg = Config.from_dict(overrides)
    nav = Navigator(cfg, EventLog(keep=True))
    if waypoints:
        nav.set_route(waypoints)
    return nav


def sample(t, heading=0.0, steps=0):
    return SensorSample(t, heading, steps)


def test_commands_are_refused_until_they_make_sense():
    nav = make(waypoints=None)
    with pytest.raises(NavError):
        nav.calibrate(0.0)                 # no route
    nav.set_route(L_ROUTE)
    with pytest.raises(NavError):
        nav.calibrate(0.0)                 # no sensor data
    with pytest.raises(NavError):
        nav.start(0.0)                     # not calibrated
    nav.on_sample(sample(0.0, 123.0))
    nav.calibrate(0.0)
    nav.start(0.0)
    assert nav.follower.state is State.NAVIGATING


def test_bad_routes_are_refused():
    nav = make(waypoints=None)
    for bad in [{}, {"waypoints": [[0, 0]]}, {"waypoints": [[0, 0], [0, 0]]},
                {"waypoints": [[0, 0], [1, "x"]]}, {"waypoints": [[0, 0], [1, float("nan")]]}, []]:
        with pytest.raises(NavError):
            nav.set_route(bad)


def test_calibration_places_the_wearer_on_waypoint_0_facing_waypoint_1():
    nav = make({"waypoints": [[2, 3], [7, 3]]})   # first leg points along +x
    nav.on_sample(sample(0.0, 200.0, 40))
    nav.calibrate(0.0)
    pose = nav.dr.pose
    assert (pose.x, pose.y, pose.heading_deg) == pytest.approx((2.0, 3.0, 90.0))
    nav.on_sample(sample(0.1, 200.0, 41))
    assert (nav.dr.pose.x, nav.dr.pose.y) == pytest.approx((2.7, 3.0))


def test_calibration_averages_the_compass_across_north():
    nav = make()
    for i, h in enumerate([358.0, 2.0, 359.0, 1.0]):
        nav.on_sample(sample(i * 0.05, h))
    info = nav.calibrate(0.2)
    assert min(info["compass_deg"], 360 - info["compass_deg"]) < 0.01


def test_a_new_route_needs_a_new_calibration():
    nav = make()
    nav.on_sample(sample(0.0))
    nav.calibrate(0.0)
    nav.set_route({"waypoints": [[0, 0], [3, 0]]})
    with pytest.raises(NavError):
        nav.start(0.1)


def test_route_and_calibrate_are_refused_while_guiding():
    # Either would put the follower back to IDLE: guidance would end with
    # no signal to the wearer. They have to follow a stop, which taps both pods.
    nav = make()
    nav.on_sample(sample(0.0))
    nav.calibrate(0.0)
    nav.start(0.0)
    with pytest.raises(NavError, match="stop first"):
        nav.calibrate(0.5)
    with pytest.raises(NavError, match="stop first"):
        nav.set_route({"waypoints": [[0, 0], [3, 0]]})
    nav.on_stale(1.5)                      # paused for want of sensor data is still guiding
    with pytest.raises(NavError, match="stop first"):
        nav.set_route({"waypoints": [[0, 0], [3, 0]]})
    assert nav.follower.state is State.NAVIGATING
    assert nav.waypoints == [(0.0, 0.0), (0.0, 5.0), (5.0, 5.0)] and nav.loc.calibrated

    assert nav.stop(2.0) == [Action("stop")]
    nav.set_route({"waypoints": [[0, 0], [3, 0]]})
    assert nav.follower.state is State.IDLE and len(nav.waypoints) == 2


def test_flip_y_mirrors_the_route():
    nav = make({"waypoints": [[0, 0], [0, 5], [5, 5]]}, route={"flip_y": True})
    assert nav.waypoints == [(0.0, 0.0), (0.0, -5.0), (5.0, -5.0)]


def test_stale_sensor_stops_once_and_resumes():
    nav = make()
    nav.on_sample(sample(0.0, 90.0))       # compass reads 90 when facing along the first leg
    nav.calibrate(0.0)
    nav.start(0.0)
    assert nav.on_stale(1.0) == [Action("stop")]
    assert nav.paused and not nav.sensor_ok
    assert nav.on_stale(2.0) == []         # only once
    assert nav.on_stale(3.0) == []
    # Data comes back with the wearer 45 deg off course: guidance resumes.
    assert nav.on_sample(sample(3.5, 135.0)) == [Action("tap", "left")]
    assert not nav.paused and nav.sensor_ok
    assert nav.on_stale(4.5) == [Action("stop")]   # and a new dropout stops again


def test_stale_sensor_does_nothing_unless_navigating():
    nav = make()
    nav.on_sample(sample(0.0))
    nav.calibrate(0.0)
    assert nav.on_stale(1.0) == []
    with pytest.raises(NavError):
        nav.calibrate(1.0)                 # refuses to calibrate on stale data


def test_operator_stop_then_start_resumes():
    nav = make()
    nav.on_sample(sample(0.0))
    nav.calibrate(0.0)
    nav.start(0.0)
    assert nav.stop(0.5) == [Action("stop")]
    assert nav.on_sample(sample(1.0, 90.0)) == []
    nav.start(1.5)
    assert nav.on_sample(sample(2.0, 90.0)) == [Action("double_tap", "left")]


def test_scripted_walk_of_demo_l_arrives():
    nav = make(waypoints=None)
    import json
    nav.set_route(json.loads((ROOT / "routes" / "demo_L.json").read_text()))
    script = (ROOT / "routes" / "demo_L.walk").read_text()
    actions = drive(nav, script_events(script))
    assert nav.follower.state is State.ARRIVED
    assert actions[-1] == Action("arrived")
    assert Action("double_tap", "right") in actions
    assert not any(a.side == "left" for a in actions)
    assert (nav.dr.pose.x, nav.dr.pose.y) == pytest.approx((4.9, 4.9), abs=0.75)


def test_scripted_dropout_triggers_the_stop():
    nav = make()
    script = "wait 0.2\ncmd calibrate\ncmd start\nstep 2\ngap 3\nstep 1"
    actions = drive(nav, script_events(script))
    assert actions.count(Action("stop")) == 1


def test_everything_is_logged():
    nav = make()
    drive(nav, script_events("wait 0.2\ncmd calibrate\ncmd start\nturn 45 0.5\ncmd stop"))
    types = [r["type"] for r in nav.log.records]
    assert types[0] == "meta" and types[1] == "route"
    for expected in ("sensor", "calibrate", "start", "pose", "tap", "stop"):
        assert expected in types
    taps = [r for r in nav.log.records if r["type"] == "tap"]
    assert taps[0]["kind"] == "tap" and taps[0]["side"] == "left"
    assert taps[-1]["kind"] == "stop"


# -- heading smoothing (a phone that swings with each stride) ----------------------------

import math  # noqa: E402

from nudge.config import ConfigError  # noqa: E402
from nudge.replay import replay  # noqa: E402

STRAIGHT = {"waypoints": [[0, 0], [0, 40]]}


def wobbling_walk(nav, seconds=12.0, wobble_deg=25.0, period_s=1.1, dt=0.05):
    """Walk straight down the first leg while the phone swings once per stride,
    as it does in a back pocket. Returns the taps and the largest heading error."""
    nav.on_sample(sample(0.0, 0.0, 0))
    nav.calibrate(0.0)
    nav.start(0.0)
    actions, worst, steps = [], 0.0, 0
    for k in range(1, int(seconds / dt) + 1):
        t = round(k * dt, 6)
        if k % 11 == 0:
            steps += 1                                   # a step every 0.55 s
        heading = wobble_deg * math.sin(2 * math.pi * t / period_s)
        actions += nav.on_sample(sample(t, heading % 360.0, steps))
        if t > 2.0:
            worst = max(worst, abs(nav.follower.error_deg))
    return actions, worst


def test_stride_wobble_causes_taps_unless_the_heading_is_smoothed():
    raw_taps, raw_worst = wobbling_walk(make(STRAIGHT))
    assert raw_worst > 20.0 and len(raw_taps) >= 3       # steering at the swing of a pocket

    nav = make(STRAIGHT, sensors={"heading_smooth_s": 1.1})
    taps, worst = wobbling_walk(nav)
    assert taps == [] and worst < 3.0
    assert nav.dr.pose.y == pytest.approx(21 * 0.7, abs=0.2)   # and the walk is still counted
    assert nav.heading_spread_deg() > 10.0               # the jitter readout still shows the raw swing


def test_smoothing_a_little_off_one_stride_is_still_enough():
    taps, worst = wobbling_walk(make(STRAIGHT, sensors={"heading_smooth_s": 1.0}), wobble_deg=15.0, period_s=1.2)
    assert taps == [] and worst < 8.0


def test_a_real_turn_still_gets_through_the_smoothing():
    nav = make(sensors={"heading_smooth_s": 1.0})
    script = "heading 40\nwait 1.2\ncmd calibrate\ncmd start\nstep 7\nturn 90 1.5\nwait 1.5\nstep 7\nwait 1"
    drive(nav, script_events(script))
    assert nav.follower.state is State.ARRIVED
    assert [e["event"] for e in nav.events if e["event"] == "turn_snap"] == ["turn_snap"]
    assert nav.pose.heading_deg == pytest.approx(90.0, abs=1.0)


def test_calibration_uses_the_smoothed_heading_so_nothing_jumps():
    nav = make(sensors={"heading_smooth_s": 1.0})
    for k in range(21):                                   # standing still; the reading flickers +/-10 deg
        nav.on_sample(sample(k * 0.05, 350.0 if k % 2 else 10.0))
    nav.calibrate(1.0)
    nav.start(1.0)
    assert nav.on_sample(sample(1.05, 350.0)) == []
    assert min(nav.pose.heading_deg, 360.0 - nav.pose.heading_deg) < 1.0


def test_a_smoothed_walk_replays_exactly():
    nav = make(STRAIGHT, sensors={"heading_smooth_s": 1.1})
    wobbling_walk(nav, seconds=6.0)
    result = replay(nav.log.records)
    assert result.ok, result.summary()


def test_heading_smoothing_is_bounded():
    for bad in (-0.1, 2.5):
        with pytest.raises(ConfigError):
            make(sensors={"heading_smooth_s": bad})


# -- wearer height (someone else puts the belt on) ---------------------------------------


def test_wearer_height_scales_the_step_length_from_the_reference_wearer():
    nav = make(deadreckon={"step_length_m": 0.5, "height_ref_cm": 175.0})
    assert nav.snapshot()["wearer"]["height_cm"] == 175.0
    assert nav.set_wearer_height(160, 0.0) == pytest.approx(0.5 * 160 / 175)
    assert nav.set_wearer_height(190, 0.1) == pytest.approx(0.5 * 190 / 175)     # from the measured step, not the last one
    assert nav.snapshot()["wearer"]["height_cm"] == 190.0
    assert nav.snapshot()["localizer"]["step_length_m"] == pytest.approx(0.5 * 190 / 175)


def test_wearer_height_applies_to_the_next_step_and_survives_calibration():
    nav = make(STRAIGHT, deadreckon={"step_length_m": 0.5, "height_ref_cm": 175.0})
    nav.on_sample(sample(0.0, 0.0, 0))
    nav.calibrate(0.0)
    nav.start(0.0)
    nav.on_sample(sample(0.5, 0.0, 1))
    assert nav.pose.y == pytest.approx(0.5)
    nav.set_wearer_height(140, 0.6)                       # mid-walk: only the steps after it change
    nav.on_sample(sample(1.0, 0.0, 2))
    assert nav.pose.y == pytest.approx(0.5 + 0.4)
    nav.stop(1.5)
    nav.calibrate(1.5)                                    # the next wearer's calibration keeps it
    nav.start(1.5)
    nav.on_sample(sample(2.0, 0.0, 3))
    assert nav.pose.y == pytest.approx(0.4)
    assert replay(nav.log.records).ok                     # and a replay makes the same change at the same moment


@pytest.mark.parametrize("bad", [None, "tall", True, 90, 260, float("nan")])
def test_wearer_height_refuses_nonsense(bad):
    nav = make()
    with pytest.raises(NavError, match="centimetres"):
        nav.set_wearer_height(bad, 0.0)
    assert nav.snapshot()["localizer"]["step_length_m"] == 0.7
