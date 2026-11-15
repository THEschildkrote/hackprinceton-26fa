"""The operator's position fix: "the wearer is standing on this waypoint now"."""
import pytest

from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.follower import State
from nudge.navigator import Navigator, NavError
from nudge.replay import replay
from nudge.sensors.base import SensorSample
from nudge.sensors.sim import SimSource

from test_server import serve, wait_for

L_ROUTE = {"waypoints": [[0, 0], [0, 5], [5, 5]]}
SHORT_L = {"waypoints": [[0, 0], [0, 3], [3, 3]]}


class Walk:
    """A navigator and a wearer who reports one step per reading, half a second apart."""

    def __init__(self, route=L_ROUTE, step_length_m=0.5):
        self.nav = Navigator(Config.from_dict({"deadreckon": {"step_length_m": step_length_m}}), EventLog(keep=True))
        self.nav.set_route(route)
        self.t, self.count = 0.0, 0
        self.actions = []
        self.feed(0.0)
        self.nav.calibrate(self.t)
        self.nav.start(self.t)

    def feed(self, heading, step=False):
        self.t += 0.5
        self.count += 1 if step else 0
        got = self.nav.on_sample(SensorSample(self.t, heading, self.count))
        self.actions += got
        return got

    def steps(self, n, heading=0.0):
        for _ in range(n):
            self.feed(heading, step=True)

    def events(self, name):
        return [e for e in self.nav.events if e["event"] == name]


def test_mark_puts_the_wearer_on_the_corner_and_guidance_goes_on_from_there():
    w = Walk()
    w.steps(7)                                  # seven steps counted on a five metre leg: the estimate is 1.5 m short
    assert w.nav.pose.y == pytest.approx(3.5) and w.nav.loc.leg == 0 and not w.actions
    out = w.nav.mark(1, w.t)
    assert (w.nav.pose.x, w.nav.pose.y) == pytest.approx((0.0, 5.0))
    assert out["waypoint"] == 1 and out["moved_m"] == pytest.approx(1.5)
    assert w.nav.loc.leg == 1 and w.nav.follower.target == 2
    snap = w.events("mark_snap")[0]
    assert snap["waypoint"] == 1 and snap["from"] == pytest.approx([0.0, 3.5]) and snap["to"] == [0.0, 5.0]
    got = w.feed(0.0)                           # still facing along the first leg: tapped round to the right
    assert [(a.kind, a.side) for a in got] == [("double_tap", "right")]
    assert replay(w.nav.log.records).ok


def test_the_marked_leg_measures_the_step_when_the_answer_is_believable():
    w = Walk()
    w.steps(9)
    out = w.nav.mark(1, w.t)                    # 5 m in 9 steps: 0.556 m, within 30 percent of 0.5
    assert out["step_length_was_m"] == pytest.approx(0.5) and out["step_length_m"] == pytest.approx(5 / 9)
    change = w.events("step_length")[0]
    assert change["reason"] == "mark" and change["used"] is True and change["steps"] == 9
    w.feed(90.0)
    w.steps(1, heading=90.0)                    # the next leg is walked with the new step
    assert w.nav.pose.x == pytest.approx(5 / 9)


def test_a_step_length_far_from_the_current_one_is_reported_but_not_used():
    w = Walk()
    w.steps(5)                                  # 5 m in 5 steps would be a metre a step
    out = w.nav.mark(1, w.t)
    assert out["step_length_m"] == pytest.approx(0.5)
    change = w.events("step_length")[0]
    assert change["used"] is False and change["measured_m"] == pytest.approx(1.0) and change["new_m"] == pytest.approx(0.5)
    assert (w.nav.pose.x, w.nav.pose.y) == pytest.approx((0.0, 5.0))      # the position is fixed all the same


def test_too_few_steps_measure_nothing():
    w = Walk()
    w.steps(2)
    w.nav.mark(1, w.t)
    assert not w.events("step_length") and w.nav.loc.step_length_m == pytest.approx(0.5)


def test_mark_without_a_waypoint_means_the_next_one():
    w = Walk()
    w.steps(8)
    assert w.nav.mark(None, w.t)["waypoint"] == 1
    assert w.nav.mark(None, w.t)["waypoint"] == 2        # now on the second leg: its end


def test_marking_the_end_arrives():
    w = Walk()
    w.steps(8)
    w.nav.mark(2, w.t)
    assert (w.nav.pose.x, w.nav.pose.y) == pytest.approx((5.0, 5.0))
    got = w.feed(0.0)
    assert [a.kind for a in got] == ["arrived"] and w.nav.follower.state is State.ARRIVED


def test_mark_brings_back_a_walk_that_arrived_too_early():
    w = Walk(SHORT_L)
    w.steps(6)                                  # to the corner
    w.feed(90.0)
    w.feed(90.0)                                # turned right, and it held
    w.steps(4, heading=90.0)                    # within reach of the end, by the count
    assert w.nav.follower.state is State.ARRIVED
    w.nav.mark(1, w.t)                          # but the wearer is really still on the corner
    assert w.nav.follower.state is State.NAVIGATING and w.nav.follower.target == 2
    assert (w.nav.pose.x, w.nav.pose.y) == pytest.approx((0.0, 3.0)) and w.nav.loc.leg == 1
    w.steps(2, heading=90.0)
    assert w.nav.pose.x == pytest.approx(1.0) and w.nav.follower.state is State.NAVIGATING
    assert replay(w.nav.log.records).ok


def test_mark_is_refused_when_it_cannot_mean_anything():
    nav = Navigator(Config.from_dict({}), EventLog(keep=True))
    nav.set_route(L_ROUTE)
    with pytest.raises(NavError, match="not calibrated"):
        nav.mark(1, 0.0)
    w = Walk()
    for bad in (0, 3, -1, 1.5, "1", True):
        with pytest.raises(NavError, match="waypoint"):
            w.nav.mark(bad, w.t)
    assert not w.events("mark_snap")


def test_mark_over_http():
    async def test(client, runtime):
        await client.post("/route", json=L_ROUTE)
        refused = await client.post("/mark", json={"waypoint": 1})
        assert refused.status == 409 and "not calibrated" in (await refused.json())["error"]
        await wait_for(lambda: runtime.nav.samples >= 1)
        await client.post("/calibrate")
        await client.post("/start")
        answer = await client.post("/mark", json={"waypoint": 1})
        body = await answer.json()
        assert answer.status == 200 and body["waypoint"] == 1 and body["moved_m"] > 0
        state = await (await client.get("/state")).json()
        assert [state["pose"]["x"], state["pose"]["y"]] == state["route"]["waypoints"][1]
        assert any(e["event"] == "mark_snap" for e in state["events"])
        assert (await (await client.post("/mark")).json())["waypoint"] == 2          # no body: the next one
        assert (await client.post("/mark", json={"waypoint": 9})).status == 409
        page = await (await client.get("/viewer")).text()
        assert 'id="mark"' in page and "/mark" in page

    serve(SimSource(), test)
