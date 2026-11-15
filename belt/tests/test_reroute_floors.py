"""A new route for a walk that is under way, and a route that changes floor."""
import pytest

from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.follower import Action, State
from nudge.navigator import Navigator, NavError
from nudge.replay import replay
from nudge.route import RouteError, extract_floors, join_route
from nudge.sensors.base import SensorSample
from nudge.sensors.sim import SimSource

from test_mark import Walk
from test_server import serve, wait_for

L = [(0.0, 0.0), (0.0, 5.0), (5.0, 5.0)]
L_ROUTE = {"waypoints": [[0, 0], [0, 5], [5, 5]]}
TWO_FLOORS = {"points": [[0, 0, "f1"], [0, 4, "f1"], [0, 4, "f2"], [5, 4, "f2"]]}


# -- picking a route up from where the wearer is -------------------------------------------


def test_a_route_is_picked_up_at_the_nearest_place():
    assert join_route((0.0, 2.0), L, 2.0) == [(0.0, 2.0), (0.0, 5.0), (5.0, 5.0)]      # part way along the first leg
    assert join_route((1.0, 2.0), L, 2.0) == [(1.0, 2.0), (0.0, 5.0), (5.0, 5.0)]      # a metre to its side
    assert join_route((2.0, 5.0), L, 2.0) == [(2.0, 5.0), (5.0, 5.0)]                  # already on the second leg
    assert join_route((0.0, 5.0), L, 2.0) == [(0.0, 5.0), (5.0, 5.0)]                  # standing on the corner
    assert join_route((0.0, 4.9), L, 2.0) == [(0.0, 4.9), (5.0, 5.0)]                  # a first leg of 10 cm is skipped


def test_too_far_from_the_route_means_being_led_to_its_start():
    assert join_route((10.0, -10.0), L, 2.0) == [(10.0, -10.0)] + L
    assert join_route((0.0, 2.0), L, 0.0) == [(0.0, 2.0), (0.0, 5.0), (5.0, 5.0)]      # on the line counts at any reach


def test_already_at_the_end_is_not_a_route():
    with pytest.raises(RouteError, match="already at the end"):
        join_route((5.0, 5.0), L, 2.0)


def test_a_route_is_split_where_the_floor_changes():
    assert extract_floors(TWO_FLOORS) == [("f1", [[0.0, 0.0], [0.0, 4.0]]), ("f2", [[0.0, 4.0], [5.0, 4.0]])]
    assert extract_floors({"waypoints": [[0, 0], [1, 0]]}) == [(None, [[0.0, 0.0], [1.0, 0.0]])]
    back = extract_floors({"points": [[0, 0, 1], [1, 0, 1], [1, 0, 2], [2, 0, 2], [2, 0, 1], [3, 0, 1]]})
    assert [floor for floor, _ in back] == [1, 2, 1]                                   # up, along, and down again


# -- a new route mid-walk ------------------------------------------------------------------


def test_a_new_route_mid_walk_keeps_the_position_and_goes_on_guiding():
    w = Walk()
    w.steps(4)                                                  # two metres up the first leg
    out = w.nav.reroute({"route": {"points": [[0, 0, "f1"], [0, 3, "f1"], [-4, 3, "f1"]]}}, t=w.t)
    assert out == {"guiding": True, "given": 3, "used": 3, "dropped_other_floors": 0, "floors": 1}
    assert w.nav.waypoints == [(0.0, 2.0), (0.0, 3.0), (-4.0, 3.0)]            # picked up from where they are
    assert (w.nav.pose.x, w.nav.pose.y) == pytest.approx((0.0, 2.0)) and w.nav.loc.calibrated
    assert w.nav.follower.state is State.NAVIGATING and w.nav.follower.target == 1
    w.steps(2)                                                  # on to the new corner
    assert any(a.side == "left" for a in w.actions)             # and tapped round to the left, the new way
    w.feed(270.0)
    w.feed(270.0)
    w.steps(7, heading=270.0)
    assert w.nav.follower.state is State.ARRIVED
    assert (w.nav.pose.x, w.nav.pose.y) == pytest.approx((-3.5, 3.0), abs=0.6)
    assert replay(w.nav.log.records).ok


def test_a_new_route_from_far_away_leads_to_its_start_first():
    w = Walk()
    w.steps(4)
    w.nav.reroute({"waypoints": [[8, 2], [8, 6]]}, t=w.t)
    assert w.nav.waypoints == [(0.0, 2.0), (8.0, 2.0), (8.0, 6.0)]
    assert [(a.kind, a.side) for a in w.feed(0.0)] == [("double_tap", "right")]


def test_a_new_route_while_not_guiding_waits_for_start_and_needs_no_calibration():
    w = Walk()
    w.steps(4)
    w.nav.stop(w.t)
    out = w.nav.reroute({"waypoints": [[0, 0], [0, 6]]}, t=w.t)
    assert out["guiding"] is False and w.nav.follower.state is State.IDLE
    w.nav.start(w.t)                                            # no calibrate: the position was kept
    w.steps(6)
    assert w.nav.follower.state is State.ARRIVED and w.nav.pose.y == pytest.approx(5.0)
    assert replay(w.nav.log.records).ok


def test_a_new_route_is_refused_without_a_position_or_with_nowhere_to_go():
    nav = Navigator(Config.from_dict({}), EventLog(keep=True))
    nav.set_route(L_ROUTE)
    with pytest.raises(NavError, match="not calibrated"):
        nav.reroute(L_ROUTE)
    w = Walk()
    w.steps(4)
    for bad in ({"waypoints": [[0, 0]]}, {}, {"waypoints": [[0, 1], [0, 2]]}):       # the last one ends where they stand
        with pytest.raises(NavError):
            w.nav.reroute(bad, t=w.t)
    assert w.nav.waypoints == L and w.nav.follower.state is State.NAVIGATING          # the walk is untouched


# -- a route that changes floor ------------------------------------------------------------


def test_the_walk_stops_at_the_change_of_floor_and_goes_on_when_told():
    w = Walk(TWO_FLOORS)
    assert w.nav.route_info["floors"] == 2 and w.nav.waypoints == [(0.0, 0.0), (0.0, 4.0)]
    w.steps(8)
    assert w.nav.follower.state is State.FLOOR
    assert w.actions == [Action("stop", None, "floor")]         # one stop, at the stairs, and nothing after it
    with pytest.raises(NavError, match="change of floor"):
        w.nav.start(w.t)
    w.steps(3)                                                  # steps on the stairs: counted by the phone, not by us
    out = w.nav.next_floor(w.t)
    assert out == {"floor": "f2", "floor_index": 1, "floors": 2, "waypoints": 2}
    assert (w.nav.pose.x, w.nav.pose.y) == pytest.approx((0.0, 4.0))            # where the second floor's stretch begins
    assert w.nav.follower.state is State.NAVIGATING
    route = w.nav.snapshot()["route"]
    assert (route["floor"], route["floor_index"], route["floors"]) == ("f2", 1, 2)
    assert [(a.kind, a.side) for a in w.feed(0.0)] == [("double_tap", "right")]
    w.feed(90.0)
    w.feed(90.0)
    w.steps(9, heading=90.0)
    assert w.nav.follower.state is State.ARRIVED and w.actions[-1] == Action("arrived")
    assert replay(w.nav.log.records).ok


def test_next_floor_is_refused_when_there_is_none_or_the_walk_is_not_on():
    w = Walk()
    with pytest.raises(NavError, match="no further floor"):
        w.nav.next_floor(w.t)
    nav = Navigator(Config.from_dict({}), EventLog(keep=True))
    nav.set_route(TWO_FLOORS)
    with pytest.raises(NavError, match="not calibrated"):
        nav.next_floor(0.0)
    nav.on_sample(SensorSample(0.5, 0.0, 0))
    nav.calibrate(0.5)
    with pytest.raises(NavError, match="not under way"):
        nav.next_floor(0.5)


def test_fixing_the_position_at_the_end_of_a_floor_is_the_change_of_floor():
    w = Walk(TWO_FLOORS)
    w.steps(3)
    w.nav.mark(1, w.t)
    assert w.feed(0.0) == [Action("stop", None, "floor")] and w.nav.follower.state is State.FLOOR


def test_stop_at_a_change_of_floor_is_silent_and_calibrating_goes_back_to_the_first_floor():
    w = Walk(TWO_FLOORS)
    w.steps(8)
    assert w.nav.stop(w.t) == [] and w.nav.follower.state is State.STOPPED          # already standing still
    w.nav.next_floor(w.t)                                                           # an operator stop does not lose the route
    assert w.nav.follower.state is State.NAVIGATING and w.nav.floor_index == 1
    w.nav.stop(w.t)
    w.nav.calibrate(w.t)
    assert w.nav.floor_index == 0 and w.nav.waypoints == [(0.0, 0.0), (0.0, 4.0)]
    assert (w.nav.pose.x, w.nav.pose.y) == (0.0, 0.0)


def test_a_new_route_mid_walk_may_change_floor_too():
    w = Walk()
    w.steps(4)
    out = w.nav.reroute({"points": [[0, 0, 1], [0, 4, 1], [0, 4, 2], [3, 4, 2]]}, t=w.t)
    assert out["floors"] == 2 and w.nav.waypoints == [(0.0, 2.0), (0.0, 4.0)]
    w.steps(4)
    assert w.nav.follower.state is State.FLOOR
    assert replay(w.nav.log.records).ok


# -- over HTTP -----------------------------------------------------------------------------


def test_reroute_and_next_floor_over_http():
    async def test(client, runtime):
        await client.post("/route", json=L_ROUTE)
        early = await client.post("/route", json={**L_ROUTE, "reroute": True})
        assert early.status == 409 and "not calibrated" in (await early.json())["error"]
        await wait_for(lambda: runtime.nav.samples >= 1)
        await client.post("/calibrate")
        await client.post("/start")
        plain = await client.post("/route", json=TWO_FLOORS)                 # without "reroute" it is still refused mid-walk
        assert plain.status == 409 and "stop first" in (await plain.json())["error"]
        assert (await client.post("/floor")).status == 409                   # this route has one floor
        answer = await client.post("/route", json={"route": {"points": TWO_FLOORS["points"]}, "reroute": True})
        body = await answer.json()
        assert answer.status == 200 and body["reroute"] is True and body["guiding"] is True and body["floors"] == 2
        state = await (await client.get("/state")).json()
        assert state["state"] == "NAVIGATING" and state["calibrated"] and state["route"]["floors"] == 2
        assert state["route"]["waypoints"] == [[0.0, 0.0], [0.0, 4.0]]
        floor = await (await client.post("/floor")).json()
        assert floor["ok"] and floor["floor"] == "f2" and floor["floor_index"] == 1
        state = await (await client.get("/state")).json()
        assert [state["pose"]["x"], state["pose"]["y"]] == [0.0, 4.0] and state["route"]["floor"] == "f2"
        page = await (await client.get("/viewer")).text()
        assert 'id="floor"' in page and "/floor" in page

    serve(SimSource(), test)
