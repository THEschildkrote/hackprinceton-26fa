"""Closed loop: a simulated wearer who obeys the taps.

The scripted traces elsewhere are walks that happen regardless of the
taps. Here the taps are the only thing steering: the wearer turns towards
the tapped shoulder, keeps walking, and stops when told. That checks the
follower and the drift corrections work together rather than against
each other.
"""

import math
import random

import pytest

from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.follower import State
from nudge.navigator import Navigator
from nudge.sensors.base import SensorSample

DT = 0.05
DEMO_L = {"waypoints": [[0, 0], [0, 5], [5, 5]]}


class Wearer:
    def __init__(self, step_m=0.7, cadence_s=0.55, turn_rate=90.0, react_s=0.4,
                 per_tap=25.0, per_double=50.0, compass0=40.0, noise_deg=0.0, seed=1):
        self.step_m, self.cadence_s, self.turn_rate, self.react_s = step_m, cadence_s, turn_rate, react_s
        self.per = {"tap": per_tap, "double_tap": per_double}
        self.compass0, self.noise_deg = compass0, noise_deg
        self.rng = random.Random(seed)
        self.x = self.y = self.heading = 0.0   # true pose; heading 0 is along +y
        self.steps = 0
        self.pending = 0.0                      # degrees still to turn
        self.walking = False
        self.next_step = 0.0
        self.inbox = []                         # (time it is felt, action)
        self.felt = []

    def feel(self, t, action):
        self.inbox.append((t + self.react_s, action))

    def tick(self, t):
        for due, action in [m for m in self.inbox if m[0] <= t]:
            self.inbox.remove((due, action))
            self.felt.append(action)
            if action.kind in self.per:
                self.pending += self.per[action.kind] * (1 if action.side == "right" else -1)
            else:                               # stop or arrived
                self.walking = False
        if self.pending:
            turn = max(-self.turn_rate * DT, min(self.turn_rate * DT, self.pending))
            self.heading += turn
            self.pending -= turn
        if self.walking and abs(self.pending) < 30 and t >= self.next_step:
            self.x += self.step_m * math.sin(math.radians(self.heading))
            self.y += self.step_m * math.cos(math.radians(self.heading))
            self.steps += 1
            self.next_step = t + self.cadence_s

    def reading(self, t):
        noise = self.rng.uniform(-self.noise_deg, self.noise_deg) if self.noise_deg else 0.0
        return SensorSample(round(t, 3), (self.compass0 + self.heading + noise) % 360.0, self.steps)


def walk(wearer, route=DEMO_L, limit_s=120.0, config=None, **deadreckon):
    nav = Navigator(Config.from_dict({**(config or {}), "deadreckon": deadreckon}), EventLog(keep=True))
    nav.set_route(route)
    t = 0.0
    while t < limit_s:
        t += DT
        wearer.tick(t)
        for action in nav.on_sample(wearer.reading(t)):
            wearer.feel(t, action)
        if abs(t - 0.5) < DT / 2:
            nav.calibrate(t)
            nav.start(t)
            wearer.walking = True
            wearer.next_step = t + 0.5
        if nav.follower.state is State.ARRIVED and not wearer.inbox and not wearer.walking:
            break
    return nav


def off_target(wearer, route=DEMO_L):
    return math.dist((wearer.x, wearer.y), route["waypoints"][-1])


def side_flips(wearer):
    sides = [a.side for a in wearer.felt if a.side]
    return sum(1 for a, b in zip(sides, sides[1:]) if a != b)


def test_obedient_wearer_reaches_the_end_of_demo_l():
    wearer = Wearer()
    nav = walk(wearer)
    assert nav.follower.state is State.ARRIVED
    assert wearer.felt[-1].kind == "arrived"
    assert off_target(wearer) < 1.5              # arrival is declared within 1 m, plus reaction
    assert len(wearer.felt) <= 8                 # a few taps at the corner, not a stream
    assert side_flips(wearer) == 0               # never told left then right
    assert not any(a.side == "left" for a in wearer.felt)


def test_plain_dead_reckoning_gives_the_same_walk_when_sensors_are_perfect():
    a, b = Wearer(), Wearer()
    walk(a)
    walk(b, drift_correction=False)
    assert off_target(a) < 1.5 and off_target(b) < 1.5


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_compass_noise_does_not_cause_tap_chatter(seed):
    wearer = Wearer(noise_deg=6.0, seed=seed)
    nav = walk(wearer)
    assert nav.follower.state is State.ARRIVED
    assert off_target(wearer) < 2.0
    assert len(wearer.felt) <= 10
    assert side_flips(wearer) <= 1


@pytest.mark.parametrize("true_step", [0.63, 0.77])
def test_ten_percent_step_error_still_arrives_nearby(true_step):
    wearer = Wearer(step_m=true_step)
    nav = walk(wearer)
    assert nav.follower.state is State.ARRIVED
    assert off_target(wearer) < 2.0


def test_slow_careful_wearer_is_guided_round_the_corner():
    # Turns at 30 deg/s: far too slow for the 3 s turn snap, so the fallback has to cope.
    wearer = Wearer(turn_rate=30.0)
    nav = walk(wearer)
    assert nav.follower.state is State.ARRIVED
    assert off_target(wearer) < 2.0


def test_longer_route_with_two_turns():
    route = {"waypoints": [[0, 0], [0, 8], [6, 8], [6, 2]]}
    wearer = Wearer()
    nav = walk(wearer, route)
    assert nav.follower.state is State.ARRIVED
    assert off_target(wearer, route) < 2.0
    assert side_flips(wearer) == 0


# -- overturn ------------------------------------------------------------------
# The belt's own settings: the heading is averaged over a second (phone in a
# pocket), so the follower sees a turn late. A wearer who turns too far on
# each tap, or starts turning late, used to be tapped again before the first
# tap had played out, and swung from side to side.

POCKET = {"sensors": {"heading_smooth_s": 1.0}}
OLD_RULES = {**POCKET, "follower": {"turning_deg_s": 0, "overturn_window_s": 0}}
TOO_FAR = [
    dict(per_tap=45, per_double=90),
    dict(per_tap=60, per_double=130),
    dict(per_tap=60, per_double=130, turn_rate=180.0),
    dict(per_tap=60, per_double=130, react_s=0.8),
    dict(per_tap=45, per_double=90, react_s=1.2),
]


def overturn_taps(nav):
    return [r for r in nav.log.records if r["type"] == "tap" and r.get("reason") == "overturn"]


@pytest.mark.parametrize("habit", TOO_FAR)
def test_a_wearer_who_turns_too_far_is_called_back_and_arrives(habit):
    wearer = Wearer(step_m=0.5, **habit)
    nav = walk(wearer, limit_s=60.0, config=POCKET, step_length_m=0.5)
    assert nav.follower.state is State.ARRIVED
    assert off_target(wearer) < 2.0
    assert len(wearer.felt) <= 16
    assert all(r["kind"] == "tap" for r in overturn_taps(nav))      # called back gently, never with a double tap


def test_the_overturn_rules_are_what_stops_the_swinging():
    habit = dict(per_tap=60, per_double=130, react_s=0.8)
    before = walk(Wearer(step_m=0.5, **habit), limit_s=60.0, config=OLD_RULES, step_length_m=0.5)
    assert before.follower.state is State.NAVIGATING                # a minute on, still being tapped from side to side
    wearer = Wearer(step_m=0.5, **habit)
    after = walk(wearer, limit_s=60.0, config=POCKET, step_length_m=0.5)
    assert after.follower.state is State.ARRIVED and overturn_taps(after)
    assert side_flips(wearer) < 8


def test_an_obedient_wearer_is_not_tapped_more_or_led_further_off_by_the_overturn_rules():
    for config in (POCKET, OLD_RULES):
        wearer = Wearer(step_m=0.5)
        nav = walk(wearer, config=config, step_length_m=0.5)
        assert nav.follower.state is State.ARRIVED and off_target(wearer) < 1.5 and len(wearer.felt) <= 7
        assert not overturn_taps(nav)
