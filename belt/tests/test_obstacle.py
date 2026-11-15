"""Something in the way that is not on the map, and the switch that decides whether it matters."""
import pytest

from nudge.config import Config, ConfigError, ObstacleConfig
from nudge.follower import Action, State
from nudge.navigator import NavError
from nudge.obstacle import ObstacleGuard
from nudge.replay import replay
from nudge.sensors.sim import SimSource

from test_mark import Walk
from test_server import serve, wait_for

STOP = Action("stop", None, "obstacle")
L_ROUTE = {"waypoints": [[0, 0], [0, 5], [5, 5]]}


class Seeing(Walk):
    """A walk with a sensor looking ahead: every reading is fresh when the next sample comes."""

    def see(self, left, ahead, right, heading=0.0, step=False):
        self.nav.on_obstacle(left, ahead, right, self.t)
        return self.feed(heading, step=step)


def on():
    w = Seeing()
    w.nav.set_obstacle_mode(True, w.t)
    return w


# -- the switch ----------------------------------------------------------------------------


def test_the_switch_is_off_until_someone_turns_it_on():
    assert ObstacleConfig().enabled is False and Config.from_dict({}).obstacle.enabled is False
    w = Seeing()
    for _ in range(4):
        assert w.see(3.0, 0.4, 3.0, step=True) == []            # a wall half a step away, and nothing is said
    seen = w.nav.snapshot(w.t)["obstacle"]
    assert seen["enabled"] is False and seen["blocked"] is False and seen["ahead_m"] == 0.4      # shown all the same
    assert w.nav.pose.y == pytest.approx(2.0)


def test_switched_on_it_stops_the_wearer_and_switched_off_it_lets_them_go():
    w = on()
    assert w.see(3.0, 1.0, 3.0) == [STOP]
    assert w.nav.snapshot(w.t)["obstacle"]["blocked"] is True
    assert w.nav.set_obstacle_mode(False, w.t) is False
    assert w.nav.snapshot(w.t)["obstacle"]["blocked"] is False
    assert w.see(3.0, 1.0, 3.0) == []
    with pytest.raises(NavError, match="enabled"):
        w.nav.set_obstacle_mode("yes", w.t)
    assert replay(w.nav.log.records).ok


# -- what it does when it is on -------------------------------------------------------------


def test_stop_once_then_one_tap_a_second_towards_the_side_with_more_room():
    w = on()
    got = [w.see(3.0, 1.2, 0.6) for _ in range(5)]              # five readings, half a second apart
    assert got == [[STOP], [], [Action("tap", "left", "obstacle")], [], [Action("tap", "left", "obstacle")]]
    assert w.see(0.7, 1.2, 2.5) == []                           # the room is on the right now, but a tap just went
    assert w.see(0.7, 1.2, 2.5) == [Action("tap", "right", "obstacle")]


def test_boxed_in_on_both_sides_it_only_stops():
    w = on()
    got = [w.see(0.5, 1.0, 0.8) for _ in range(6)]
    assert got == [[STOP], [], [], [], [], []]


def test_nothing_in_range_is_room():
    w = on()
    assert w.see(None, None, None) == []                        # nothing ahead at all
    assert w.see(0.5, 1.0, None) == [STOP]
    w.see(0.5, 1.0, None)
    assert w.see(0.5, 1.0, None) == [Action("tap", "right", "obstacle")]    # nothing on the right: that way


def test_it_waits_for_a_clearer_path_than_the_one_that_stopped_it():
    w = on()
    assert w.see(3.0, 1.6, 3.0) == []                           # 1.6 m: not yet a stop
    assert w.see(3.0, 1.4, 3.0) == [STOP]
    w.see(3.0, 1.8, 3.0)
    assert w.nav.guard.blocked                                  # 1.8 m is past stop_m but not past clear_m
    w.see(3.0, 2.2, 3.0)
    assert not w.nav.guard.blocked


def test_the_routes_steering_is_held_back_while_blocked_and_comes_back_after():
    w = on()
    w.steps(8)                                                  # at the corner of the L: a right turn is due
    w.actions.clear()
    assert w.see(3.0, 1.0, 3.0) == [STOP]                       # but someone is standing in the corner
    assert not any(a.reason is None for a in w.actions)         # no "turn right" on top of the stop
    for _ in range(3):
        w.see(3.0, 1.0, 3.0)
    assert all(a.reason == "obstacle" for a in w.actions)
    got = w.see(3.0, None, 3.0) + w.see(3.0, None, 3.0) + w.see(3.0, None, 3.0)      # they moved away
    assert [(a.kind, a.side, a.reason) for a in got][:1] in ([("double_tap", "right", None)], [("tap", "right", None)])


def test_arrival_is_never_swallowed():
    w = on()
    w.steps(8)
    w.feed(90.0)
    w.feed(90.0)
    w.steps(7, heading=90.0)                                    # one step short of the end's reach
    assert w.nav.follower.state is State.NAVIGATING
    assert w.see(3.0, 0.8, 3.0, heading=90.0, step=True) == [Action("arrived")]     # the wall at the end of the route
    assert w.nav.follower.state is State.ARRIVED and not w.nav.guard.blocked


def test_a_sensor_that_goes_quiet_does_not_leave_the_wearer_stopped():
    w = on()
    assert w.see(3.0, 1.0, 3.0) == [STOP]                       # the reading was half a second old by then
    w.feed(0.0)
    assert w.nav.guard.blocked                                  # a second old: still believed
    w.feed(0.0)
    assert not w.nav.guard.blocked                              # older than stale_s: let go
    assert w.nav.snapshot(w.t)["obstacle"]["age_s"] == pytest.approx(1.5)


def test_readings_before_the_walk_starts_do_nothing():
    w = on()
    w.nav.stop(w.t)
    assert w.see(3.0, 0.5, 3.0) == [] and not w.nav.guard.blocked


def test_nonsense_readings_are_refused():
    w = on()
    for bad in (-1, "near", True, float("nan"), 5000):
        with pytest.raises(NavError, match="metres"):
            w.nav.on_obstacle(None, bad, None, w.t)
    assert w.nav.guard.ahead_m is None


def test_a_walk_with_obstacles_replays_to_the_same_taps():
    w = on()
    w.steps(3)
    for ahead in (1.2, 1.2, 1.2, 1.9, 2.4, None):
        w.see(2.5, ahead, 0.5, step=ahead is None)
    w.steps(4)
    assert [a.reason for a in w.actions if a.reason] == ["obstacle", "obstacle"]
    assert replay(w.nav.log.records).ok


def test_guard_alone_passes_everything_when_off():
    guard = ObstacleGuard(ObstacleConfig(enabled=False))
    guard.reading(1.0, 0.2, 0.2, 0.2)
    tap = [Action("tap", "left")]
    assert guard.update(1.1, True, tap) is tap


def test_settings_are_checked():
    with pytest.raises(ConfigError, match="stop_m"):
        Config.from_dict({"obstacle": {"stop_m": 2.5, "clear_m": 2.0}})
    with pytest.raises(ConfigError, match="stale_s"):
        Config.from_dict({"obstacle": {"stale_s": 0}})


# -- over HTTP -----------------------------------------------------------------------------


def test_obstacle_readings_and_the_switch_over_http():
    async def test(client, runtime):
        await client.post("/route", json=L_ROUTE)
        await wait_for(lambda: runtime.nav.samples >= 1)
        await client.post("/calibrate")
        await client.post("/start")
        seen = await (await client.post("/obstacle", json={"left_m": 2.4, "ahead_m": 0.9, "right_m": None})).json()
        assert seen["ok"] and seen["enabled"] is False and seen["ahead_m"] == 0.9
        await client.post("/sim", json={"key": "step"})
        await wait_for(lambda: runtime.nav.last.step_count == 1)
        assert not runtime.nav.guard.blocked                                         # off: shown, not acted on
        flipped = await (await client.post("/obstacle/mode", json={"enabled": True})).json()
        assert flipped == {"ok": True, "enabled": True}
        await client.post("/obstacle", json={"ahead_m": 0.9})
        await wait_for(lambda: runtime.nav.guard.blocked)
        state = await (await client.get("/state")).json()
        assert state["obstacle"]["blocked"] and state["obstacle"]["enabled"]
        assert state["taps"][-1]["kind"] == "stop" and state["taps"][-1]["reason"] == "obstacle"
        assert (await client.post("/obstacle", json={"left_m": 1.0})).status == 400      # ahead_m is the one that matters
        assert (await client.post("/obstacle", json={"ahead_m": "close"})).status == 409
        assert (await client.post("/obstacle/mode", json={"enabled": "on"})).status == 409
        page = await (await client.get("/viewer")).text()
        assert 'id="obstacleOn"' in page and "/obstacle/mode" in page and 'id="ahead"' in page

    serve(SimSource(), test)
