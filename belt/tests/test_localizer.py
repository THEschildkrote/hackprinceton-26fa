"""Drift correction, one rule at a time, on scripted sensor traces.

Each script is the walk that really happened: the wearer follows the true
route with their true step length, and the compass reads whatever the
script says. The Localizer only ever sees {t, heading, step count}.
"""

import math

import pytest

from nudge.config import Config, DeadReckonConfig
from nudge.deadreckon import Localizer
from nudge.eventlog import EventLog
from nudge.follower import State
from nudge.navigator import Navigator
from nudge.sensors.sim import drive, script_events

STRAIGHT = [(0.0, 0.0), (0.0, 40.0)]
START = "heading 40\nwait 0.2\n"   # stand on waypoint 0 facing waypoint 1; the compass happens to read 40


def L(leg):
    """5 m... or any length: straight, 90 degrees right, straight."""
    return [(0.0, 0.0), (0.0, leg), (leg, leg)]


def run(script, route, **settings):
    """Calibrate on the first reading, then feed the rest of the trace."""
    loc = Localizer(DeadReckonConfig(**settings), route)
    events = []
    for kind, *rest in script_events(START + script):
        if kind != "sample":
            continue
        s = rest[0]
        if not loc.calibrated:
            loc.reset(s.heading_deg, s.step_count, s.t)
            continue
        loc.update(s.t, s.heading_deg, s.step_count)
        events += loc.drain()
    return loc, events


def named(events, name):
    return [e for e in events if e["event"] == name]


def xy(loc):
    return (loc.pose.x, loc.pose.y)


def error_m(loc, truth):
    return math.dist(xy(loc), truth)


# -- route snapping ------------------------------------------------------------


def test_route_snap_projects_the_pose_onto_the_leg():
    script = "offset 10\nstep 5"     # compass reads 10 deg right of the corridor
    only_snap = dict(bias_tau_s=0.0, turn_window_m=0.0)
    loc, events = run(script, STRAIGHT, **only_snap)
    assert xy(loc) == pytest.approx((0.0, 5 * 0.7 * math.cos(math.radians(10))))
    snaps = named(events, "route_snap")
    assert len(snaps) == 5
    assert all(s["moved_m"] == pytest.approx(0.7 * math.sin(math.radians(10))) for s in snaps)
    assert snaps[0]["leg"] == 0 and snaps[0]["to"][0] == pytest.approx(0.0)

    plain, _ = run(script, STRAIGHT, drift_correction=False)
    assert plain.pose.x == pytest.approx(5 * 0.7 * math.sin(math.radians(10)))   # 0.61 m of sideways drift


def test_route_snap_only_within_snap_dist():
    loc = Localizer(DeadReckonConfig(), STRAIGHT)
    loc.reset(0.0, 0, 0.0)
    loc.update(1.0, 90.0, 3)         # three steps sideways in a second: 2.1 m off the leg
    assert loc.pose.x == pytest.approx(2.1)
    assert named(loc.drain(), "route_snap") == []
    loc.update(1.1, 270.0, 4)        # one step back towards it: 1.4 m, inside snap_dist_m
    assert loc.pose.x == pytest.approx(0.0)
    assert len(named(loc.drain(), "route_snap")) == 1


def test_route_snap_can_be_switched_off():
    loc, events = run("offset 10\nstep 5", STRAIGHT, snap_dist_m=0.0, bias_tau_s=0.0)
    assert loc.pose.x == pytest.approx(5 * 0.7 * math.sin(math.radians(10)))
    assert named(events, "route_snap") == []


def test_route_snap_holds_the_pose_at_the_end_of_the_last_leg():
    loc, _ = run("step 6", [(0.0, 0.0), (0.0, 3.0)])     # 4.2 m walked on a 3 m route
    assert xy(loc) == pytest.approx((0.0, 3.0))


# -- turn snapping -------------------------------------------------------------


def test_turn_snap_puts_the_pose_on_the_waypoint_and_advances():
    # Real steps are 0.77 m, the config says 0.70: after 10 steps the pose is
    # 0.7 m short of the corner when the wearer turns it.
    loc, events = run("step 10\nturn 90 1.5\nwait 0.5", L(7.7))
    (snap,) = named(events, "turn_snap")
    assert snap["leg"] == 0 and snap["waypoint"] == 1
    assert snap["from"] == pytest.approx([0.0, 7.0]) and snap["to"] == [0.0, 7.7]
    assert snap["moved_m"] == pytest.approx(0.7)
    assert snap["expected_deg"] == pytest.approx(90.0)
    # Seen at 66 deg (within 25 deg of the turn), confirmed 0.15 s later at 75 deg.
    assert snap["turn_deg"] == pytest.approx(75.0)
    assert snap["held_s"] == pytest.approx(0.15)
    assert snap["over_s"] <= 3.0
    assert snap["steps_on_leg"] == 10 and snap["steps_since"] == 0
    assert xy(loc) == pytest.approx((0.0, 7.7))
    assert loc.leg == 1
    assert loc.pose.heading_deg == pytest.approx(90.0)


@pytest.mark.parametrize("script, why", [
    ("step 10\nturn 50 1.5", "50 deg is more than 25 deg short of a 90 deg turn"),
    ("step 10\nturn -90 1.5", "wrong direction"),
    ("step 10\nturn 130 1.5\nturn -130 0.05", "passes through, but see below"),
    ("step 10\nturn 90 6", "too slow: never 65 deg within 3 s"),
])
def test_turn_snap_needs_the_expected_turn_within_three_seconds(script, why):
    loc, events = run(script, L(7.7))
    snaps = named(events, "turn_snap")
    if "passes through" in why:
        assert len(snaps) == 1      # a turn that sweeps through 65-115 deg does count
    else:
        assert snaps == [], why
        assert loc.leg == 0


def test_turn_snap_only_within_turn_window_of_the_waypoint():
    near, events = run("step 10\nturn 90 1.5", L(9.5))     # believed 2.5 m from the corner
    assert len(named(events, "turn_snap")) == 1 and xy(near) == pytest.approx((0.0, 9.5))
    far, events = run("step 10\nturn 90 1.5", L(11.0))     # believed 4.0 m from the corner
    assert named(events, "turn_snap") == [] and far.leg == 0


def test_turn_snap_ignores_looking_around_before_walking():
    loc, events = run("turn 90 1.5\nturn -90 1.5", L(2.0))  # corner is only 2 m away, no step taken
    assert named(events, "turn_snap") == [] and loc.leg == 0 and xy(loc) == (0.0, 0.0)


def test_turn_snap_skips_gentle_bends():
    bend = [(0.0, 0.0), (0.0, 5.0), (2.0, 8.46)]            # a 30 deg bend
    loc, events = run("step 5\nturn 30 1", bend)
    assert named(events, "turn_snap") == []


@pytest.mark.parametrize("glitch_s", [0.05, 0.1])
def test_turn_snap_ignores_a_compass_glitch(glitch_s):
    # The compass jumps 90 deg for one or two readings near the corner and comes back.
    script = f"step 10\nturn 90 0\nwait {glitch_s}\nturn -90 0\nwait 1"
    loc, events = run(script, L(7.7))
    assert named(events, "turn_snap") == [] and loc.leg == 0
    assert xy(loc) == pytest.approx((0.0, 7.0))
    # With no hold the same glitch would have moved the pose 0.7 m and changed leg.
    hasty, events = run(script, L(7.7), turn_hold_s=0.0)
    assert len(named(events, "turn_snap")) == 1 and hasty.leg == 1


def test_turn_snap_ignores_a_swing_across_the_leg():
    # Veering 35 deg left, then over-correcting to 35 deg right, is a 70 deg
    # change in a second: the size of the turn, but the wearer is still
    # facing down this leg, not along the next one.
    loc, events = run("step 8\nturn -35 0.5\nstep 1\nturn 70 1\nwait 1", L(7.0))
    assert named(events, "turn_snap") == [] and named(events, "leg_advance") == []
    assert loc.leg == 0 and loc.pose.x == pytest.approx(0.0)


def test_steps_taken_while_a_turn_is_confirmed_count_on_the_next_leg():
    # Walking round the corner: a step lands between the turn being seen
    # (66 deg) and confirmed (75 deg). It was taken after the corner.
    loc, events = run("step 10\narc 90 5 0.3", L(7.7))
    (snap,) = named(events, "turn_snap")
    assert snap["steps_since"] == 1
    assert snap["to"] == pytest.approx([0.7, 7.7])
    assert xy(loc) == pytest.approx((1.4, 7.7))      # and the last step of the arc after that


def test_one_turn_is_not_counted_twice():
    zigzag = [(0.0, 0.0), (0.0, 7.0), (2.0, 7.0), (2.0, 0.0)]   # right, then right again 2 m later
    loc, events = run("step 10\nturn 90 1.5\nwait 1", zigzag)
    assert len(named(events, "turn_snap")) == 1
    assert loc.leg == 1


# -- step-length calibration ----------------------------------------------------


CALIBRATE = dict(calibrate_step_length=True)    # the rule is off unless asked for


def test_step_length_calibration_is_off_by_default():
    loc, events = run("step 10\nturn 90 1.5", L(7.7))
    assert len(named(events, "turn_snap")) == 1
    assert named(events, "step_length") == [] and loc.step_length_m == 0.7


def test_step_length_is_calibrated_from_the_first_leg():
    loc, events = run("step 10\nturn 90 1.5", L(7.7), **CALIBRATE)
    (cal,) = named(events, "step_length")
    assert cal["old_m"] == 0.7 and cal["new_m"] == pytest.approx(0.77)
    assert cal["leg_m"] == pytest.approx(7.7) and cal["steps"] == 10
    assert loc.step_length_m == pytest.approx(0.77)


def test_ten_percent_step_length_error_long_and_short():
    for true_step in (0.77, 0.63):
        leg = true_step * 10
        script = "step 10\nturn 90 1.5\nstep 10"
        loc, _ = run(script, L(leg), **CALIBRATE)
        assert loc.step_length_m == pytest.approx(true_step)
        assert error_m(loc, (leg, leg)) < 0.05
        plain, _ = run(script, L(leg), drift_correction=False)
        assert error_m(plain, (leg, leg)) > 0.9          # 0.7 m wrong on each leg


@pytest.mark.parametrize("leg, steps, expected", [(5.0, 4, 0.9), (3.0, 10, 0.5)])
def test_step_length_is_clamped(leg, steps, expected):
    loc, events = run(f"step {steps}\nturn 90 1.5", L(leg), **CALIBRATE)
    (cal,) = named(events, "step_length")
    assert cal["measured_m"] == pytest.approx(leg / steps)
    assert cal["new_m"] == expected == loc.step_length_m


def test_step_length_is_calibrated_once_and_only_from_a_real_leg():
    u_turn = [(0.0, 0.0), (0.0, 7.7), (7.7, 7.7), (7.7, 0.0)]
    loc, events = run("step 10\nturn 90 1.5\nstep 9\nturn 90 1.5", u_turn, **CALIBRATE)
    assert len(named(events, "turn_snap")) == 2
    assert len(named(events, "step_length")) == 1
    short, events = run("step 2\nturn 90 1.5", L(1.4), **CALIBRATE)   # two steps are not a measurement
    assert len(named(events, "turn_snap")) == 1 and named(events, "step_length") == []


# -- compass bias ----------------------------------------------------------------


def test_compass_bias_converges_on_a_constant_15_degree_offset():
    loc, _ = run("offset 15\nstep 50", STRAIGHT)             # 27.5 s of walking
    assert loc.bias_deg == pytest.approx(15.0, abs=0.5)
    assert loc.pose.heading_deg == pytest.approx(0.0, abs=0.5) or loc.pose.heading_deg > 359.5
    assert loc.pose.x == pytest.approx(0.0, abs=1e-9)


def test_compass_bias_time_constant_is_five_seconds():
    # Learning starts with the first step on the leg; then exactly 5 s of offset.
    loc, _ = run("step 1\noffset 15\nstep 10 0.5", STRAIGHT)
    assert loc.bias_deg == pytest.approx(15.0 * (1 - math.exp(-1)), abs=0.3)


def test_compass_bias_is_not_learned_while_standing_at_the_start():
    loc, _ = run("offset 15\nwait 1.2", STRAIGHT)            # calibrated, not yet walking
    assert loc.bias_deg == 0.0


def test_compass_bias_is_reset_at_each_turn():
    loc, events = run("offset 15\nstep 10\nturn 90 1.5\nwait 0.3", L(7.0))
    (snap,) = named(events, "turn_snap")
    # What was thrown away: about 15 * (1 - e^-1.1) = 10 deg after 5.5 s of
    # walking, plus a little from the first 15 deg of the turn itself.
    assert 9.5 < snap["bias_deg"] < 11.5
    assert loc.bias_deg == 0.0               # nothing is learned until a step on the new leg


def test_compass_bias_ignores_headings_far_off_the_leg():
    loc, _ = run("offset 45\nstep 20", STRAIGHT)             # 45 deg off is not "walking the leg"
    assert loc.bias_deg == 0.0


def test_constant_15_degree_compass_offset_on_the_l_route():
    script = "offset 15\nstep 10\nturn 90 1.5\nstep 10"
    loc, _ = run(script, L(7.0))
    assert error_m(loc, (7.0, 7.0)) < 0.4
    plain, _ = run(script, L(7.0), drift_correction=False)
    assert error_m(plain, (7.0, 7.0)) > 2.0


# -- freeze ------------------------------------------------------------------------


def test_pose_freezes_after_one_and_a_half_seconds_without_a_step():
    loc, events = run("step 3\nwait 1.4", STRAIGHT)
    assert not loc.frozen
    loc, events = run("step 3\nwait 1.6", STRAIGHT)
    assert loc.frozen
    (freeze,) = named(events, "freeze")
    assert freeze["y"] == pytest.approx(2.1)


def test_frozen_pose_ignores_compass_wander_and_stops_learning_bias():
    loc, _ = run("step 3\nwait 2\noffset 20\nwait 10", STRAIGHT)
    assert loc.frozen
    assert xy(loc) == pytest.approx((0.0, 2.1))
    assert abs(loc.bias_deg) < 0.01          # ten seconds of a 20 deg offset taught it nothing


def test_frozen_heading_stays_live_by_default_so_a_turn_on_the_spot_is_seen():
    loc, _ = run("step 3\nwait 2\nturn 40 1", STRAIGHT)
    assert loc.frozen and loc.pose.heading_deg == pytest.approx(40.0)
    held, _ = run("step 3\nwait 2\nturn 40 1", STRAIGHT, freeze_heading=True)
    assert held.frozen and held.pose.heading_deg == pytest.approx(0.0)


def test_a_step_unfreezes():
    loc, events = run("step 3\nwait 2\nstep 1", STRAIGHT)
    assert not loc.frozen
    assert [e["event"] for e in events if e["event"] in ("freeze", "unfreeze")] == ["freeze", "unfreeze"]
    assert loc.pose.y == pytest.approx(2.8)


def test_turn_on_the_spot_still_snaps_while_frozen():
    loc, events = run("step 10\nwait 2\nturn 90 1.5", L(7.7))
    assert [e["event"] for e in events if e["event"] in ("freeze", "turn_snap")] == ["freeze", "turn_snap"]
    assert xy(loc) == pytest.approx((0.0, 7.7))


# -- never getting stuck -------------------------------------------------------------


def test_slow_turn_is_picked_up_after_two_steps_along_the_next_leg():
    loc, events = run("step 10\nturn 90 6\nstep 3", L(7.0))    # too slow for the turn snap
    assert named(events, "turn_snap") == []
    (advance,) = named(events, "leg_advance")
    assert advance["reason"] == "heading" and advance["leg"] == 0
    assert loc.leg == 1
    assert xy(loc) == pytest.approx((2.1, 7.0))                # three steps into the second leg


BEND_45 = [(0.0, 0.0), (0.0, 6.0), (4.0, 10.0)]         # the shallowest corner treated as a turn


def test_veering_before_a_shallow_corner_is_not_taken_for_the_turn():
    # 25 deg off the leg is nearer the next leg's bearing (45) than this
    # one's (0), but it is veering, not turning: the pose stays on this leg.
    loc, events = run("step 5\nturn 25 0.5\nstep 3", BEND_45)
    assert named(events, "leg_advance") == [] and named(events, "turn_snap") == []
    assert loc.leg == 0
    assert loc.pose.x == pytest.approx(0.0) and 5.3 < loc.pose.y < 5.5    # still short of the corner at y = 6


def test_a_slow_turn_at_a_shallow_corner_is_still_picked_up():
    loc, events = run("step 8\nturn 45 8\nstep 3", BEND_45)     # too slow for the turn snap
    assert named(events, "turn_snap") == []
    (advance,) = named(events, "leg_advance")
    assert advance["reason"] == "heading" and loc.leg == 1


# -- step counts that jump ---------------------------------------------------------------


@pytest.mark.parametrize("settings", [{}, {"drift_correction": False}])
def test_a_jump_in_the_step_count_is_dropped_not_walked(settings):
    loc = Localizer(DeadReckonConfig(**settings), STRAIGHT)
    loc.reset(0.0, 0, 0.0)
    loc.update(0.1, 0.0, 1)
    assert loc.pose.y == pytest.approx(0.7)
    loc.update(0.2, 0.0, 41)                 # forty steps in a tenth of a second
    assert loc.pose.y == pytest.approx(0.7)
    (jump,) = named(loc.drain(), "step_jump")
    assert jump["steps"] == 40 and jump["allowed"] == 2
    loc.update(0.3, 0.0, 42)                 # counting carries on from the new value
    assert loc.pose.y == pytest.approx(1.4)


def test_steps_that_pile_up_over_a_short_silence_are_kept():
    loc = Localizer(DeadReckonConfig(), STRAIGHT)
    loc.reset(0.0, 0, 0.0)
    loc.update(2.0, 0.0, 4)                  # two seconds without a reading, four steps walked
    assert loc.pose.y == pytest.approx(2.8)
    assert named(loc.drain(), "step_jump") == []


def test_waypoints_on_a_straight_are_walked_through():
    loc, events = run("step 6", [(0.0, 0.0), (0.0, 3.0), (0.0, 6.0)])
    (advance,) = named(events, "leg_advance")
    assert advance["reason"] == "passed"
    assert loc.leg == 1 and xy(loc) == pytest.approx((0.0, 4.2))


def test_dense_route_round_a_gentle_corner_is_followed():
    s30, c30 = math.sin(math.radians(30)), math.cos(math.radians(30))
    route = [(0.0, 0.0), (0.0, 4.0), (2 * s30, 4 + 2 * c30), (2 * s30 + 2 * c30, 4 + 2 * c30 + 2 * s30)]
    route.append((route[-1][0] + 4.0, route[-1][1]))
    script = "step 8\nturn 30 0.5\nstep 4\nturn 30 0.5\nstep 4\nturn 30 0.5\nstep 8"
    loc, events = run(script, route, step_length_m=0.5)
    assert loc.leg == 3
    assert error_m(loc, route[-1]) < 0.3
    assert {e["reason"] for e in named(events, "leg_advance")} == {"passed"}


# -- through the whole pipeline ---------------------------------------------------------


def navigator(route, **deadreckon):
    nav = Navigator(Config.from_dict({"deadreckon": deadreckon}), EventLog(keep=True))
    nav.set_route({"waypoints": [list(p) for p in route]})
    return nav


WALK = "heading 40\nwait 0.5\ncmd calibrate\ncmd start\noffset 15\nstep 10\nturn 90 1.5\nstep 10\nwait 1"


def test_step_error_and_compass_offset_together_still_arrive():
    nav = navigator(L(7.7), **CALIBRATE)    # real steps 0.77 m, and a 15 deg compass offset
    drive(nav, script_events(WALK))
    assert nav.follower.state is State.ARRIVED
    assert math.dist((nav.pose.x, nav.pose.y), (7.7, 7.7)) < 0.4
    assert nav.loc.step_length_m == pytest.approx(0.77)

    plain = navigator(L(7.7), drift_correction=False)
    drive(plain, script_events(WALK))
    assert math.dist((plain.pose.x, plain.pose.y), (7.7, 7.7)) > 2.0


def test_every_snap_and_calibration_is_in_the_jsonl_log():
    nav = navigator(L(7.7), **CALIBRATE)
    drive(nav, script_events(WALK))
    by_type = {}
    for record in nav.log.records:
        by_type.setdefault(record["type"], []).append(record)
    assert len(by_type["turn_snap"]) == 1 and by_type["turn_snap"][0]["to"] == [0.0, 7.7]
    assert len(by_type["step_length"]) == 1 and by_type["step_length"][0]["new_m"] == pytest.approx(0.77)
    assert len(by_type["route_snap"]) >= 10            # one per step taken at an angle to the leg
    assert [r["type"] for r in nav.log.records if r["type"] in ("freeze", "unfreeze")][:2] == ["freeze", "unfreeze"]
    for record in by_type["route_snap"] + by_type["turn_snap"]:
        assert {"t", "leg", "from", "to", "moved_m"} <= set(record)
    pose = by_type["pose"][-1]
    assert {"leg", "bias_deg", "step_length_m", "frozen"} <= set(pose)
    assert [e["event"] for e in nav.events if e["event"] == "turn_snap"] == ["turn_snap"]   # and on /state


def test_follower_never_steers_back_to_a_corner_already_turned():
    # Believed 2.5 m short of the corner when the wearer turns it: the turn
    # snap moves the pose there and the follower must aim at the next waypoint.
    nav = navigator(L(9.5))
    drive(nav, script_events("heading 40\nwait 0.5\ncmd calibrate\ncmd start\nstep 10\nturn 90 1.5\nwait 3\nstep 3"))
    assert nav.loc.leg == 1 and nav.follower.target == 2
    (snap,) = [e for e in nav.events if e["event"] == "turn_snap"]
    # Until the turn is recognised the follower can only read it as drifting
    # off course (at most one tap the wrong way). After the snap: silence.
    before = [t for t in nav.taps if t["t"] < snap["t"]]
    after = [t for t in nav.taps if t["t"] >= snap["t"]]
    assert len(before) <= 1
    assert after == []
