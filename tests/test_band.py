"""the pi servo band: timing logic only, no gpio.

the engine takes `now` in milliseconds rather than reading a clock, so every one of these
runs in no time and is exact. same trick as firmware/test/test_protocol.cpp.
"""
import pytest

from nudge.band import CUES, Engine, NullBand, REST_DEG, approach_cue

LEFT, RIGHT = 0, 1


def drive(engine, ms, step=5, live=None, start=0):
    """tick the engine over a span, feeding `live` each time as the real loop would."""
    states = []
    for now in range(start, start + ms, step):
        if live is not None:
            engine.on_live(live, now)
        states.append(engine.tick(now))
    return states


def strokes(states, side):
    """how many times that shoulder was tapped: rising edges off rest or detached."""
    n, out = 0, False
    for s in states:
        v = s[side]
        now_out = v is not None and v > REST_DEG
        if now_out and not out:
            n += 1
        out = now_out
    return n


def live(**kw):
    base = {"m_next": 10.0, "deg_next": 0, "act_next": "straight", "dir_next": "S",
            "off": False, "done": False, "unmapped": False, "hazard": False}
    base.update(kw)
    return base


# --- the grammar --------------------------------------------------------------


def test_every_cue_plays_and_finishes():
    for cue in CUES:
        e = Engine()
        assert e.play(cue, 0, force=True), cue
        states = drive(e, 6000)
        assert not e.busy, f"{cue} never finished"
        assert states[-1] == (None, None), f"{cue} left a servo engaged"


@pytest.mark.parametrize("cue,side,other,n", [
    ("bear_left", LEFT, RIGHT, 1),
    ("left", LEFT, RIGHT, 2),
    ("bear_right", RIGHT, LEFT, 1),
    ("right", RIGHT, LEFT, 2),
])
def test_direction_is_the_shoulder_and_size_is_the_count(cue, side, other, n):
    e = Engine()
    e.play(cue, 0, force=True)
    states = drive(e, 4000)
    assert strokes(states, side) == n
    assert strokes(states, other) == 0
    assert all(s[other] is None for s in states)


@pytest.mark.parametrize("cue,n", [("uturn", 3), ("floor", 2), ("arrive", 3), ("hazard", 3)])
def test_both_shoulder_cues(cue, n):
    e = Engine()
    e.play(cue, 0, force=True)
    states = drive(e, 5000)
    assert strokes(states, LEFT) == n
    assert strokes(states, RIGHT) == n


def test_horn_detaches_between_strokes():
    e = Engine()
    e.play("left", 0, force=True)
    states = drive(e, 3000)
    assert (None, None) in states[:-1], "never detached mid-pattern; the servo will hum"


def test_a_turn_is_slower_than_a_vibration_pulse():
    """the whole reason this is a redesign: a stroke a shoulder can feel takes ~300 ms."""
    for cue in ("left", "right", "uturn"):
        on_ms = CUES[cue][0].on_ms + CUES[cue][0].back_ms
        assert on_ms >= 250, f"{cue} stroke is {on_ms} ms, too fast for an sg90 to travel"


# --- live packets -------------------------------------------------------------


def test_hazard_fires_once_per_rising_edge():
    e = Engine()
    states = drive(e, 2000, live=live(hazard=True))
    assert strokes(states, LEFT) == 3
    more = drive(e, 2000, live=live(hazard=True), start=2000)
    assert strokes(more, LEFT) == 0, "hazard repeated without a new edge"


def test_hazard_cuts_in_over_a_steering_tap():
    e = Engine()
    e.on_live(live(deg_next=80), 0)
    e.tick(0)
    assert e.busy
    e.on_live(live(deg_next=80, hazard=True), 20)
    states = drive(e, 2000, start=20)
    assert strokes(states, LEFT) == strokes(states, RIGHT) >= 3


def test_arrive_fires_on_done():
    e = Engine()
    states = drive(e, 2500, live=live(done=True))
    assert strokes(states, LEFT) == 3


def test_approach_tap_fires_inside_two_metres_and_not_again():
    e = Engine()
    far = drive(e, 500, live=live(m_next=5.0, act_next="turn", dir_next="L", deg_next=0))
    assert strokes(far, LEFT) == 0
    near = drive(e, 2500, live=live(m_next=1.5, act_next="turn", dir_next="L", deg_next=0), start=500)
    assert strokes(near, LEFT) == 2, "a left turn is two strokes on the left shoulder"
    again = drive(e, 2500, live=live(m_next=1.2, act_next="turn", dir_next="L", deg_next=0), start=3000)
    assert strokes(again, LEFT) == 0, "tapped the same turn twice"


def test_backing_away_rearms_the_turn_tap():
    e = Engine()
    drive(e, 2500, live=live(m_next=1.5, act_next="turn", dir_next="L", deg_next=0))
    drive(e, 500, live=live(m_next=4.0, act_next="turn", dir_next="L", deg_next=0), start=2500)
    back = drive(e, 2500, live=live(m_next=1.5, act_next="turn", dir_next="L", deg_next=0), start=3000)
    assert strokes(back, LEFT) == 2


def test_unmapped_says_stop_and_gives_no_turn_taps():
    e = Engine()
    states = drive(e, 3000, live=live(unmapped=True, m_next=1.0, act_next="turn", dir_next="L"))
    assert strokes(states, LEFT) == strokes(states, RIGHT) == 2  # the unmapped pattern only


def test_unmapped_repeats_while_you_are_off_the_map():
    e = Engine()
    first = drive(e, 3000, live=live(unmapped=True))
    later = drive(e, 6000, live=live(unmapped=True), start=3000)
    assert strokes(first, LEFT) == 2
    assert strokes(later, LEFT) >= 2, "stopped reminding while still off the map"


# --- steering -----------------------------------------------------------------


def test_steer_period_dead_band_and_clamp():
    assert Engine.steer_period(0) == 0
    assert Engine.steer_period(Engine.STEER_DEAD_DEG) == 0
    assert Engine.steer_period(-10) == 0
    assert Engine.steer_period(90) == Engine.STEER_FAST_MS
    assert Engine.steer_period(170) == Engine.STEER_FAST_MS


def test_steer_period_tightens_as_you_drift():
    periods = [Engine.steer_period(d) for d in (20, 40, 60, 90)]
    assert periods == sorted(periods, reverse=True)
    assert all(p >= Engine.STEER_FAST_MS for p in periods)


def test_steering_never_outruns_a_servo():
    """the motor band pulsed every 300 ms at 90 degrees. that would wreck an sg90."""
    assert Engine.STEER_FAST_MS >= 1500


@pytest.mark.parametrize("deg,side,other", [(40, LEFT, RIGHT), (-40, RIGHT, LEFT)])
def test_steering_taps_the_side_you_turn_toward(deg, side, other):
    """device.py convention: + degrees is left."""
    e = Engine()
    states = drive(e, 1200, live=live(deg_next=deg))
    assert strokes(states, side) >= 1
    assert strokes(states, other) == 0


def test_no_steering_inside_the_dead_band():
    e = Engine()
    states = drive(e, 6000, live=live(deg_next=8))
    assert states.count((None, None)) == len(states)


def test_no_steering_when_off_route_or_arrived():
    for flag in ("off", "done"):
        e = Engine()
        e.on_live(live(**{flag: True}), 0)
        drive(e, 3000, start=0)  # let the state cue finish
        quiet = drive(e, 6000, live=live(deg_next=80, **{flag: True}), start=3000)
        assert quiet.count((None, None)) == len(quiet), f"kept steering while {flag}"


def test_unknown_heading_is_silent():
    e = Engine()
    states = drive(e, 6000, live=live(deg_next=None))
    assert states.count((None, None)) == len(states)


# --- gear protection ----------------------------------------------------------


def play_out(engine, start=0, step=5):
    """tick until the pattern finishes; returns the time it ended."""
    now = start
    while engine.busy:
        now += step
        engine.tick(now)
    return now


def test_min_gap_blocks_back_to_back_patterns():
    e = Engine()
    e.play("left", 0, force=True)
    ended = play_out(e)
    assert not e.play("right", ended + 10), "started a new pattern with no rest"
    assert e.play("right", ended + Engine.MIN_GAP_MS + 10)


def test_force_cuts_in_but_still_respects_the_rest():
    e = Engine()
    e.play("left", 0, force=True)
    assert e.play("hazard", 50, force=True), "force should pre-empt a running pattern"
    ended = play_out(e, start=50)
    assert not e.play("hazard", ended + 10, force=True), "force skipped the rest after a pattern"
    assert e.play("hazard", ended + Engine.MIN_GAP_MS + 10, force=True)


# --- wiring -------------------------------------------------------------------


def test_approach_cue_matches_the_firmware_table():
    assert approach_cue("turn", "L") == "left"
    assert approach_cue("turn", "R") == "right"
    assert approach_cue("bear", "L") == "bear_left"
    assert approach_cue("uturn", "S") == "uturn"
    assert approach_cue("floor", "S") == "floor"
    assert approach_cue("straight", "S") is None
    assert approach_cue("arrive", "S") is None


def test_null_band_records_only_changes():
    band = NullBand()
    band.apply(None, None)
    band.apply(30.0, None)
    band.apply(30.0, None)
    band.apply(None, None)
    assert [(c[1], c[2]) for c in band.commands] == [(None, None), (30.0, None), (None, None)]


def test_cue_names_cover_the_instruct_table():
    from nudge.instruct import CUES as SPOKEN
    assert set(SPOKEN) <= set(CUES), f"no servo cue for {set(SPOKEN) - set(CUES)}"
