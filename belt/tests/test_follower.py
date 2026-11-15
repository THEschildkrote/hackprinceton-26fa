import pytest

from nudge.config import FollowerConfig
from nudge.follower import Action, Follower, FollowerError, State
from nudge.geometry import bearing_deg, wrap180, wrap360

L_ROUTE = [(0.0, 0.0), (0.0, 5.0), (5.0, 5.0)]


def navigating(route=L_ROUTE, **cfg):
    f = Follower(FollowerConfig(**cfg))
    f.set_route(route)
    f.start()
    return f


# -- angle wrap --------------------------------------------------------------


@pytest.mark.parametrize("angle, expected", [
    (0, 0), (10, 10), (-10, -10), (179, 179), (-179, -179),
    (180, 180), (-180, 180), (181, -179), (190, -170), (-190, 170),
    (270, -90), (360, 0), (540, 180), (725, 5), (-725, -5),
])
def test_wrap180(angle, expected):
    assert wrap180(angle) == pytest.approx(expected)
    assert -180 < wrap180(angle) <= 180


def test_wrap360_and_bearing():
    assert wrap360(-10) == 350
    assert wrap360(725) == 5
    assert bearing_deg(0, 1) == pytest.approx(0)      # +y is "ahead"
    assert bearing_deg(1, 0) == pytest.approx(90)     # +x is to the right
    assert bearing_deg(0, -1) == pytest.approx(180)
    assert bearing_deg(-1, 0) == pytest.approx(270)


def test_error_wraps_across_north():
    f = navigating()
    f.update(0.0, 0.0, 0.0, 350.0)  # target bears 0 deg, facing 350: 10 deg to the right
    assert f.error_deg == pytest.approx(10)
    f.update(0.1, 0.0, 0.0, 10.0)
    assert f.error_deg == pytest.approx(-10)


def test_error_sign_picks_the_side_to_turn_towards():
    f = navigating()
    assert f.update(0.0, 0.0, 0.0, 330.0) == [Action("tap", "right")]   # target is clockwise
    f = navigating()
    assert f.update(0.0, 0.0, 0.0, 30.0) == [Action("tap", "left")]


# -- deadband hysteresis ------------------------------------------------------


def test_quiet_inside_the_deadband():
    f = navigating()
    for i, heading in enumerate([0, 5, 15, 19.9, 345, 340.1]):
        assert f.update(i * 2.0, 0.0, 0.0, heading) == []
    assert not f.correcting


def test_hysteresis_enters_at_20_and_exits_at_10():
    f = navigating()
    t = 0.0

    def tick(heading):
        nonlocal t
        t += 2.0  # well past the tap period, so only the deadband decides
        return f.update(t, 0.0, 0.0, heading)

    assert tick(15) == []                        # below 20: quiet
    assert tick(25) == [Action("tap", "left")]   # above 20: correcting
    assert tick(15) == [Action("tap", "left")]   # still above 10: keeps correcting
    assert tick(11) == [Action("tap", "left")]
    assert tick(9) == []                         # below 10: quiet again
    assert tick(15) == []                        # and stays quiet until 20
    assert tick(19) == []
    assert tick(21) == [Action("tap", "left")]


def test_double_tap_above_60_degrees():
    f = navigating()
    assert f.update(0.0, 0.0, 0.0, 300.0) == [Action("tap", "right")]         # exactly 60
    assert f.update(2.0, 0.0, 0.0, 299.0) == [Action("double_tap", "right")]  # 61
    assert f.update(8.0, 0.0, 0.0, 90.0) == [Action("double_tap", "left")]   # long after: a new error, not an overturn


def test_an_overturn_is_called_back_with_one_tap_and_sooner_than_the_period():
    f = navigating()                                    # the target is straight ahead at heading 0
    assert f.update(0.0, 0.0, 0.0, 270.0) == [Action("double_tap", "right")]   # facing left of it by 90
    assert f.update(0.3, 0.0, 0.0, 80.0) == []          # turned 170: now 80 past it, but 0.3 s after the tap
    assert f.update(0.6, 0.0, 0.0, 80.0) == [Action("tap", "left", "overturn")]   # 0.5 s is enough for this
    assert f.update(0.9, 0.0, 0.0, 80.0) == []          # after that, the usual period again
    assert f.update(1.7, 0.0, 0.0, 80.0) == [Action("double_tap", "left")]


def test_an_error_on_the_other_side_long_after_a_tap_is_not_an_overturn():
    f = navigating()
    assert f.update(0.0, 0.0, 0.0, 330.0) == [Action("tap", "right")]
    assert f.update(1.0, 0.0, 0.0, 0.0) == []
    assert f.update(6.0, 0.0, 0.0, 30.0) == [Action("tap", "left")]            # past overturn_window_s


def test_no_second_tap_while_the_wearer_is_already_turning_the_right_way():
    f = navigating()
    assert f.update(0.0, 0.0, 0.0, 270.0) == [Action("double_tap", "right")]
    issued = []
    for i in range(1, 31):                              # turning right at 40 degrees a second for 1.5 s
        t = i * 0.05
        issued += f.update(t, 0.0, 0.0, 270.0 + 40.0 * t)
    assert issued == []                                 # 30 degrees still to go at 1.0 s, but they are on their way
    assert f.turn_rate_deg_s == pytest.approx(40.0, abs=2.0)
    for i in range(31, 50):                             # then they stop, 30 degrees short
        issued += f.update(i * 0.05, 0.0, 0.0, 330.0)
    assert issued == [Action("tap", "right")]           # now the next tap comes


def test_turning_the_wrong_way_is_tapped_at_the_usual_period():
    f = navigating()
    issued = []
    for i in range(41):                                 # turning left, away from the target, for 2 s
        t = i * 0.05
        issued += [round(t, 2) for _ in f.update(t, 0.0, 0.0, 300.0 - 20.0 * t)]
    assert issued == [0.0, 1.0, 2.0]


def test_the_new_rules_can_be_switched_off():
    f = navigating(turning_deg_s=0, overturn_window_s=0)
    assert f.update(0.0, 0.0, 0.0, 270.0) == [Action("double_tap", "right")]
    assert f.update(0.6, 0.0, 0.0, 80.0) == []
    assert f.update(1.0, 0.0, 0.0, 80.0) == [Action("double_tap", "left")]


def test_taps_are_rate_limited_to_one_pattern_a_second():
    f = navigating()
    issued = []
    for i in range(41):  # 2 s at 20 Hz, 45 deg off the whole time
        t = i * 0.05
        issued += [(round(t, 2), a) for a in f.update(t, 0.0, 0.0, 45.0)]
    assert [t for t, _ in issued] == [0.0, 1.0, 2.0]
    assert all(a == Action("tap", "left") for _, a in issued)


def test_side_does_not_flip_when_facing_backwards():
    f = navigating()
    assert f.update(0.0, 0.0, 0.0, 179.0) == [Action("double_tap", "left")]
    assert f.update(2.0, 0.0, 0.0, 181.0) == [Action("double_tap", "left")]


# -- waypoint advance ---------------------------------------------------------


def test_advances_within_one_metre_of_a_waypoint():
    f = navigating()
    f.update(0.0, 0.0, 3.9, 0.0)   # 1.1 m short
    assert f.target == 1
    f.update(0.1, 0.0, 4.0, 0.0)   # exactly 1.0 m: within reach
    assert f.target == 2
    assert f.state is State.NAVIGATING


def test_after_advancing_it_steers_towards_the_next_waypoint():
    f = navigating()
    actions = f.update(0.0, 0.0, 4.5, 0.0)
    assert f.target == 2
    assert f.error_deg == pytest.approx(bearing_deg(5.0, 0.5))
    assert actions == [Action("double_tap", "right")]


def test_arrived_on_the_last_waypoint():
    f = navigating()
    f.update(0.0, 0.0, 4.5, 0.0)
    assert f.update(1.0, 4.2, 5.0, 90.0) == [Action("arrived")]
    assert f.state is State.ARRIVED
    assert f.update(5.0, 4.9, 5.0, 0.0) == []  # nothing more after arriving


def test_skips_several_close_waypoints_in_one_update():
    route = [(0.0, 0.0), (0.0, 1.0), (0.0, 1.5), (0.0, 2.0), (0.0, 10.0)]
    f = navigating(route)
    f.update(0.0, 0.0, 1.2, 0.0)
    assert f.target == 4


def test_arrives_immediately_if_everything_is_within_reach():
    f = navigating([(0.0, 0.0), (0.0, 0.5)])
    assert f.update(0.0, 0.0, 0.0, 0.0) == [Action("arrived")]


# -- states --------------------------------------------------------------------


def test_idle_until_started():
    f = Follower()
    f.set_route(L_ROUTE)
    assert f.state is State.IDLE
    assert f.update(0.0, 0.0, 0.0, 90.0) == []


def test_stop_signals_once_and_start_resumes():
    f = navigating()
    f.update(0.0, 0.0, 4.5, 0.0)
    assert f.stop() == [Action("stop")]
    assert f.state is State.STOPPED
    assert f.stop() == []
    assert f.update(1.0, 0.0, 4.5, 0.0) == []
    f.start()
    assert f.state is State.NAVIGATING
    assert f.target == 2  # progress along the route is kept


def test_start_needs_a_route_and_a_fresh_run():
    with pytest.raises(FollowerError):
        Follower().start()
    f = navigating([(0.0, 0.0), (0.0, 0.5)])
    f.update(0.0, 0.0, 0.0, 0.0)
    with pytest.raises(FollowerError):
        f.start()
    f.reset()
    f.start()
    assert f.state is State.NAVIGATING and f.target == 1


def test_skip_to_only_moves_forward():
    f = navigating()
    f.skip_to(2)
    assert f.target == 2
    f.skip_to(1)
    assert f.target == 2
    f.skip_to(99)
    assert f.target == 2
