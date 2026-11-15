"""Route input: this runtime's own format, and the upstream planner's answer as it is."""

import math

import pytest

from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.follower import Action, State
from nudge.navigator import Navigator, NavError
from nudge.replay import replay
from nudge.route import RouteError, extract_points, parse_route, simplify
from nudge.sensors.sim import drive, script_events

# What POST /api/route returns upstream: points are [x, y, floor] in the Matterport model frame.
PLANNER_ANSWER = {
    "route": {"nodes": ["p1", "p2", "p3"], "length": 6.0, "cost": 6.0, "profile": "blind",
              "points": [[0.0, 0.0, "f1"], [0.0, 3.0, "f1"], [3.0, 3.0, "f1"]]},
    "maneuvers": [{"say": "Walk 3 meters, then turn right"}],
}


def test_the_planners_answer_is_accepted_as_it_is():
    assert parse_route(PLANNER_ANSWER) == [(0.0, 0.0), (0.0, 3.0), (3.0, 3.0)]
    assert parse_route(PLANNER_ANSWER["route"]) == [(0.0, 0.0), (0.0, 3.0), (3.0, 3.0)]
    assert parse_route({"waypoints": [[0, 0], [0, 3], [3, 3]]}) == [(0.0, 0.0), (0.0, 3.0), (3.0, 3.0)]


def test_a_route_is_used_up_to_its_first_change_of_floor():
    data = {"points": [[0, 0, "f1"], [0, 4, "f1"], [2, 4, "f1"], [2, 4, "f2"], [2, 9, "f2"]]}
    points, dropped = extract_points(data)
    assert points == [[0.0, 0.0], [0.0, 4.0], [2.0, 4.0]] and dropped == 2
    assert extract_points({"points": [[0, 0, 1], [0, 4, 1]]}) == ([[0.0, 0.0], [0.0, 4.0]], 0)
    with pytest.raises(RouteError, match="two distinct"):
        parse_route({"points": [[0, 0, "f1"], [0, 4, "f2"]]})       # nothing to walk on the first floor


@pytest.mark.parametrize("bad", [
    {}, [], "route", {"route": {}}, {"points": "x"}, {"points": [[0, 0], [1]]},
    {"points": [[0, 0, "f1", 9], [1, 1, "f1", 9]]}, {"waypoints": [[0, 0], [float("inf"), 1]]},
])
def test_unusable_routes_are_refused(bad):
    with pytest.raises(RouteError):
        parse_route(bad)


def test_simplify_straightens_wiggles_and_keeps_corners():
    # A corridor walked through scan positions a few decimetres either side
    # of its centre line, then a real corner.
    wiggly = [(0.0, 0.0), (0.25, 2.0), (-0.2, 4.0), (0.15, 6.0), (0.0, 8.0), (2.0, 8.2), (4.0, 7.9), (6.0, 8.0)]
    assert simplify(wiggly, 0.4) == [(0.0, 0.0), (0.0, 8.0), (6.0, 8.0)]
    assert simplify(wiggly, 0.0) == wiggly                           # 0 follows every point
    assert simplify([(0.0, 0.0), (0.0, 5.0), (5.0, 5.0)], 0.4) == [(0.0, 0.0), (0.0, 5.0), (5.0, 5.0)]
    for kept in simplify(wiggly, 0.1):
        assert kept in wiggly                                         # never invents a point


def navigator(**route):
    return Navigator(Config.from_dict({"route": route}), EventLog(keep=True))


def test_a_planner_route_is_walked_to_arrival_and_replays():
    nav = navigator(simplify_m=0.4)
    wiggly = {"route": {"points": [[0, 0, "f1"], [0.2, 1.5, "f1"], [-0.15, 3.0, "f1"], [0, 5, "f1"],
                                   [2.5, 5.2, "f1"], [5, 5, "f1"], [5, 5, "f2"], [9, 5, "f2"]]}}
    nav.set_route(wiggly, name="to the kitchen")
    # The first leg is kept as given (it is what the wearer faces at calibration); the rest is straightened.
    assert nav.waypoints == [(0.0, 0.0), (0.2, 1.5), (0.0, 5.0), (5.0, 5.0)]
    assert nav.route_info == {"given": 8, "used": 4, "dropped_other_floors": 0, "floors": 2}
    script = "heading 40\nwait 0.5\ncmd calibrate\ncmd start\nstep 7\nturn 90 1.5\nstep 7\nwait 1"
    issued = drive(nav, script_events(script))
    assert nav.follower.state is State.FLOOR                          # the end of this floor, not of the route
    assert issued[-1] == Action("stop", None, "floor")
    assert math.dist((nav.pose.x, nav.pose.y), (5.0, 5.0)) < 1.0
    logged = next(r for r in nav.log.records if r["type"] == "route")
    assert logged["waypoints"][1] == [0.2, 1.5] and len(logged["waypoints"]) == 6    # as given, the first floor
    assert logged["points"][-1] == [9.0, 5.0, "f2"] and len(logged["points"]) == 8   # and every floor, for replay
    assert replay(nav.log.records).ok


def test_route_floors_false_ends_the_walk_at_the_first_change_of_floor():
    nav = navigator(floors=False)
    nav.set_route({"points": [[0, 0, "f1"], [0, 5, "f1"], [0, 5, "f2"], [4, 5, "f2"]]})
    assert nav.route_info == {"given": 4, "used": 2, "dropped_other_floors": 2, "floors": 1}
    drive(nav, script_events("heading 0\nwait 0.5\ncmd calibrate\ncmd start\nstep 7\nwait 1"))
    assert nav.follower.state is State.ARRIVED
    assert "points" not in next(r for r in nav.log.records if r["type"] == "route")


def test_flip_and_simplify_are_applied_once_on_replay():
    nav = navigator(flip_y=True, simplify_m=0.4)
    nav.set_route({"points": [[0, 0], [0, 1], [0.1, 2], [0, 4], [3, 4]]})
    assert nav.waypoints == [(0.0, 0.0), (0.0, -1.0), (0.0, -4.0), (3.0, -4.0)]
    again = Navigator(nav.cfg, EventLog(keep=True))
    again.set_route({"waypoints": next(r for r in nav.log.records if r["type"] == "route")["waypoints"]})
    assert again.waypoints == nav.waypoints


def test_bad_planner_routes_are_refused_by_the_navigator():
    nav = navigator()
    with pytest.raises(NavError, match="waypoint 1"):
        nav.set_route({"route": {"points": [[0, 0, "f1"], ["x", 1, "f1"]]}})


def test_the_first_leg_is_never_simplified_away():
    # The second point is only 0.3 m off the line to the third, but it is the
    # direction the wearer is told to face when calibrating.
    nav = navigator(simplify_m=0.4)
    nav.set_route({"points": [[2, 1.5], [5, 1.5], [9.5, 2.5], [12, 4.5]]})
    assert nav.waypoints[:2] == [(2.0, 1.5), (5.0, 1.5)]
    nav.on_sample(__import__("nudge.sensors.base", fromlist=["SensorSample"]).SensorSample(0.0, 200.0, 0))
    nav.calibrate(0.0)
    assert nav.pose.heading_deg == pytest.approx(90.0)        # facing along +x, as given
