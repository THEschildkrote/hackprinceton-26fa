import math

import pytest

from nudge.deadreckon import DeadReckoner


def calibrated(step=0.7, heading=0.0, compass=0.0, steps=0, x=0.0, y=0.0):
    dr = DeadReckoner(step)
    dr.reset(x, y, heading, compass, steps)
    return dr


def test_needs_calibration():
    dr = DeadReckoner()
    assert not dr.calibrated
    with pytest.raises(RuntimeError):
        dr.update(0.0, 0)


def test_each_step_advances_step_length_along_heading():
    dr = calibrated()
    pose = dr.update(0.0, 1)
    assert (pose.x, pose.y) == pytest.approx((0.0, 0.7))
    pose = dr.update(0.0, 3)
    assert (pose.x, pose.y) == pytest.approx((0.0, 2.1))


def test_heading_90_is_plus_x():
    dr = calibrated()
    pose = dr.update(90.0, 2)
    assert (pose.x, pose.y) == pytest.approx((1.4, 0.0))
    assert pose.heading_deg == pytest.approx(90.0)


def test_diagonal_step():
    dr = calibrated(step=1.0)
    pose = dr.update(45.0, 1)
    assert (pose.x, pose.y) == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)))


def test_calibration_cancels_any_compass_offset():
    # Facing along a leg that bears 90 deg while the compass happens to read 217.
    dr = calibrated(heading=90.0, compass=217.0)
    pose = dr.update(217.0, 1)
    assert pose.heading_deg == pytest.approx(90.0)
    assert (pose.x, pose.y) == pytest.approx((0.7, 0.0))
    pose = dr.update(307.0, 2)  # turned 90 deg to the right
    assert pose.heading_deg == pytest.approx(180.0)
    assert (pose.x, pose.y) == pytest.approx((0.7, -0.7))


def test_heading_wraps_through_north():
    dr = calibrated(heading=350.0, compass=350.0)
    assert dr.update(20.0, 0).heading_deg == pytest.approx(20.0)
    dr = calibrated(heading=10.0, compass=350.0)
    assert dr.update(340.0, 0).heading_deg == pytest.approx(0.0)


def test_steps_already_counted_at_calibration_do_not_move_the_pose():
    dr = calibrated(steps=120)
    pose = dr.update(0.0, 120)
    assert (pose.x, pose.y) == (0.0, 0.0)
    pose = dr.update(0.0, 121)
    assert pose.y == pytest.approx(0.7)


def test_several_steps_in_one_reading():
    dr = calibrated()
    assert dr.update(0.0, 5).y == pytest.approx(3.5)


def test_step_count_reset_rebases_without_moving():
    dr = calibrated()
    dr.update(0.0, 10)
    pose = dr.update(0.0, 0)  # the phone page reloaded
    assert pose.y == pytest.approx(7.0)
    pose = dr.update(0.0, 2)
    assert pose.y == pytest.approx(8.4)


def test_turning_without_steps_changes_only_heading():
    dr = calibrated()
    dr.update(0.0, 2)
    pose = dr.update(135.0, 2)
    assert (pose.x, pose.y) == pytest.approx((0.0, 1.4))
    assert pose.heading_deg == pytest.approx(135.0)


def test_l_shaped_walk():
    dr = calibrated(compass=40.0)
    for n in range(1, 8):
        dr.update(40.0, n)
    for n in range(8, 15):
        dr.update(130.0, n)
    assert (dr.pose.x, dr.pose.y) == pytest.approx((4.9, 4.9))


def test_blanked_steps_are_dropped_not_deferred():
    dr = calibrated()
    dr.update(0.0, 1, count_steps=False)
    assert dr.pose.y == 0.0
    assert dr.update(0.0, 2).y == pytest.approx(0.7)
