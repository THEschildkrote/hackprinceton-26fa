"""the nudge band on a raspberry pi: two servos that tap the traveller's shoulders.

why this is a redesign and not a port of firmware/nudge_band
-----------------------------------------------------------
the esp32 band drove two vibration motors, and its grammar (firmware/nudge_band/haptics.h)
is written in on/off milliseconds: `hazard` is 30 ms on, 30 off, five times over. a servo
cannot do that. an sg90 needs roughly 100 ms to swing 30 degrees and as long again to come
back, so the shortest stroke a shoulder can actually feel is nearer 300 ms. driven at 30 ms
it would buzz its gears without moving the horn far enough to notice.

so the cue set is redesigned rather than translated. nothing upstream changes: the wire
protocol in nudge/device.py is untouched, and this consumes the same live packet.

the grammar
-----------
a stroke is: swing the horn out to TAP_DEG, hold it there, bring it back to REST_DEG,
then detach. direction is carried by which shoulder taps; magnitude by how many strokes.

    bear left       1 light stroke, left shoulder
    turn left       2 strokes, left shoulder
    u-turn          3 strokes, both
    floor change    2 long strokes, both
    arrive          3 quick strokes, both
    hazard          3 fast strokes, both
    off route       1 long press, both
    unmapped        2 slow strokes, both, repeating until you are back on the map

steering - the continuous "you are drifting" cue - is the one thing that does not survive
the move. the motor version pulsed every 300 ms at 90 degrees off heading; a servo doing
that would wear itself out inside an hour and be unbearable to wear. here it is a single
light tap on the side you should turn toward, repeating no faster than once every 1.5 s.

servo care, which matters when it is strapped to someone
--------------------------------------------------------
- the horn detaches between strokes. an sg90 holding position hums and draws current the
  whole time; detached it is silent and free.
- patterns are rate limited, so a burst of cues cannot thrash the gears.
- two sg90s can pull well over an amp between them when they stall. do not take that from
  the pi's 5V pin. give the servos their own supply (the 4xAA pack) and tie the grounds
  together, or you will brown out the pi mid-demo.
- software pwm jitters and makes the horn twitch at rest. this uses pigpio for hardware
  timed pulses when the daemon is up (`sudo pigpiod`) and falls back quietly if not.
"""
import os
import time
from collections import namedtuple

REST_DEG = 0.0      # horn parked, off the shoulder
TAP_DEG = 30.0      # firm tap
LIGHT_DEG = 18.0    # lighter tap, for bear and steering

L, R, BOTH = 1, 2, 3

# on_ms: commanded out at `deg`. back_ms: commanded home. gap_ms: detached.
Stroke = namedtuple("Stroke", "on_ms back_ms gap_ms sides deg")


def _firm(sides, n, on=180, back=140, gap=120, deg=TAP_DEG):
    return [Stroke(on, back, gap, sides, deg) for _ in range(n)]


# mirrors the names in nudge/instruct.py CUES, with servo-shaped timings
CUES = {
    "straight":   _firm(BOTH, 1, on=110, back=110, gap=80, deg=LIGHT_DEG),
    "bear_left":  _firm(L, 1, on=130, back=120, gap=100, deg=LIGHT_DEG),
    "bear_right": _firm(R, 1, on=130, back=120, gap=100, deg=LIGHT_DEG),
    "left":       _firm(L, 2),
    "right":      _firm(R, 2),
    "uturn":      _firm(BOTH, 3, on=220, back=160, gap=140),
    "floor":      _firm(BOTH, 2, on=300, back=180, gap=160),
    "arrive":     _firm(BOTH, 3, on=130, back=120, gap=90),
    "hazard":     _firm(BOTH, 3, on=120, back=110, gap=70),
    "off_route":  _firm(BOTH, 1, on=500, back=200, gap=120),
    "unmapped":   _firm(BOTH, 2, on=400, back=250, gap=400),
    "steer_left":  _firm(L, 1, on=120, back=110, gap=80, deg=LIGHT_DEG),
    "steer_right": _firm(R, 1, on=120, back=110, gap=80, deg=LIGHT_DEG),
}


RANK = {
    # advisory: "you are drifting". lose these first, nothing depends on them
    "straight": 0, "steer_left": 0, "steer_right": 0,
    # navigation: the maneuver you are about to make. losing one is a wrong turn
    "bear_left": 1, "bear_right": 1, "left": 1, "right": 1, "uturn": 1, "floor": 1,
    # state and safety: always get through
    "arrive": 2, "off_route": 2, "unmapped": 2, "hazard": 3,
}


def approach_cue(act, direction):
    """which cue to tap as a maneuver comes up. mirrors haptics.h approach_cue."""
    if act == "turn":
        return "left" if direction == "L" else "right" if direction == "R" else None
    if act == "bear":
        return "bear_left" if direction == "L" else "bear_right" if direction == "R" else None
    if act == "uturn":
        return "uturn"
    if act == "floor":
        return "floor"
    return None  # straight, walk, depart; arrive fires off the done flag


class Engine:
    """pure timing logic: no gpio, no clock of its own. `now` is milliseconds.

    feed it live packets (nudge.device.live_packet) and call tick() as fast as you like.
    tick returns (left_deg, right_deg); None for a side means detach it.
    """

    STEER_DEAD_DEG = 15     # on course inside this, say nothing
    STEER_FAST_MS = 1500    # at 90 degrees off or worse
    STEER_SLOW_MS = 4000    # just outside the dead band
    NEAR_M = 2.0            # tap the turn as you come inside this
    RESET_M = 3.0           # backed off this far: allow the tap again
    UNMAPPED_REPEAT_MS = 4000
    MIN_GAP_MS = 250        # never start a pattern sooner than this after the last

    def __init__(self):
        self._tl = []
        self._t0 = 0
        self._active = False
        self._ended_at = -10 ** 9
        self._playing = None
        self._live = None
        self._steer_t0 = 0
        self._steering = False
        self._unmapped_t0 = 0
        self._tapped = False
        self._last = (None, None)
        self._was = {"off": False, "done": False, "hazard": False, "unmapped": False}

    # --- cues ---------------------------------------------------------------

    def play(self, cue, now, force=False):
        """start a pattern.

        a servo stroke is ~320 ms, so the band renders about three cues a second however
        fast positions arrive. when they come faster than that, something has to be lost,
        and RANK decides what: a maneuver cuts in over a steering tap, never the reverse.
        equal ranks do not pre-empt, so two turn cues never interrupt each other.

        `force` cuts in regardless. neither it nor a rank ever skips the rest after a
        pattern ends: that gap is what keeps a burst of cues off the gears.
        """
        pat = CUES.get(cue)
        if not pat:
            return False
        if not self._active and now - self._ended_at < self.MIN_GAP_MS:
            return False
        if self._active and not force and RANK.get(cue, 0) <= RANK.get(self._playing, 0):
            return False
        tl, t = [], 0
        for s in pat:
            if s.on_ms:
                t += s.on_ms
                tl.append((t,) + self._pair(s.sides, s.deg))
            if s.back_ms:
                t += s.back_ms
                tl.append((t,) + self._pair(s.sides, REST_DEG))
            if s.gap_ms:
                t += s.gap_ms
                tl.append((t, None, None))
        self._tl, self._t0, self._active, self._playing = tl, now, bool(tl), cue
        return self._active

    @staticmethod
    def _pair(sides, deg):
        return (deg if sides & L else None, deg if sides & R else None)

    # --- state in ----------------------------------------------------------

    def on_live(self, live, now):
        """live is a dict from nudge.device.live_packet (or decode_live)."""
        self._live = live
        # these three are state changes the traveller has to feel, so they cut in over a
        # steering tap or a turn cue that happens to be mid-stroke
        for flag, cue in (("off", "off_route"), ("done", "arrive"), ("hazard", "hazard")):
            on = bool(live.get(flag))
            if on and not self._was[flag]:
                self.play(cue, now, force=True)
            self._was[flag] = on

        unmapped = bool(live.get("unmapped"))
        if unmapped and not self._was["unmapped"]:
            self.play("unmapped", now)
            self._unmapped_t0 = now
        if not unmapped and self._was["unmapped"] and self._playing == "unmapped":
            self._active = False  # back on the map: stop mid-pattern
        self._was["unmapped"] = unmapped
        if unmapped:
            return  # no turn taps: there is no route here

        act, direction = live.get("act_next"), live.get("dir_next")
        m = live.get("m_next")
        if (act, direction) != (getattr(self, "_last_act", None), getattr(self, "_last_dir", None)):
            self._tapped = False
        if m is not None and m > self.RESET_M:
            self._tapped = False
        self._last_act, self._last_dir = act, direction

        if not self._tapped and not live.get("done") and m is not None and m < self.NEAR_M:
            cue = approach_cue(act, direction)
            if cue and self.play(cue, now):
                self._tapped = True

    # --- steering -----------------------------------------------------------

    @classmethod
    def steer_period(cls, deg):
        """ms between steering taps. 0 means stay quiet."""
        a = abs(deg)
        if a <= cls.STEER_DEAD_DEG:
            return 0
        if a >= 90:
            return cls.STEER_FAST_MS
        span = 90 - cls.STEER_DEAD_DEG
        drop = cls.STEER_SLOW_MS - cls.STEER_FAST_MS
        return int(cls.STEER_SLOW_MS - (a - cls.STEER_DEAD_DEG) * drop / span)

    # --- output -------------------------------------------------------------

    def tick(self, now):
        """(left_deg, right_deg); None on a side means detach it."""
        out = self._tick(now)
        self._last = out
        return out

    def _tick(self, now):
        if self._active:
            e = now - self._t0
            for end, left, right in self._tl:
                if e < end:
                    return (left, right)
            self._active = False
            self._ended_at = now
            self._steer_t0 = now

        live = self._live
        if live is None:
            return (None, None)

        if live.get("unmapped"):
            if now - self._unmapped_t0 >= self.UNMAPPED_REPEAT_MS:
                self._unmapped_t0 = now
                if self.play("unmapped", now):
                    return self._tick(now)
            return (None, None)

        if live.get("done") or live.get("off"):
            return (None, None)
        deg = live.get("deg_next")
        if deg is None:
            return (None, None)
        period = self.steer_period(deg)
        if not period:
            self._steering = False
            return (None, None)
        # the first tap lands as soon as you leave the dead band; waiting out a full period
        # would let someone walk four seconds in the wrong direction before being told
        if self._steering and now - self._steer_t0 < period:
            return (None, None)
        self._steering = True
        self._steer_t0 = now
        # device.py convention: + degrees is left (ccw)
        if self.play("steer_left" if deg > 0 else "steer_right", now):
            return self._tick(now)
        return (None, None)

    @property
    def busy(self):
        return self._active


class NullBand:
    """records what it was told to do. for tests, the simulator and running with no servos."""

    def __init__(self):
        self.commands = []

    def apply(self, left, right):
        if not self.commands or self.commands[-1][1:] != (left, right):
            self.commands.append((len(self.commands), left, right))

    def close(self):
        pass


class ServoBand:
    """two servos on the pi's gpio. detaches between strokes so they stay quiet."""

    def __init__(self, left_pin=12, right_pin=13, rest_deg=REST_DEG, max_deg=90.0):
        from gpiozero import AngularServo  # imported here so this module loads off-pi

        factory = self._factory()
        kw = {"min_angle": 0.0, "max_angle": max_deg,
              "min_pulse_width": 0.5 / 1000, "max_pulse_width": 2.4 / 1000}
        if factory is not None:
            kw["pin_factory"] = factory
        self.rest_deg = rest_deg
        self.left = AngularServo(left_pin, initial_angle=None, **kw)
        self.right = AngularServo(right_pin, initial_angle=None, **kw)
        self._state = (None, None)

    @staticmethod
    def _factory():
        """hardware timed pulses if pigpiod is up, else gpiozero's default."""
        try:
            from gpiozero.pins.pigpio import PiGPIOFactory
            return PiGPIOFactory()
        except Exception:
            return None

    def apply(self, left, right):
        if (left, right) == self._state:
            return
        for servo, want in ((self.left, left), (self.right, right)):
            if want is None:
                servo.detach() if hasattr(servo, "detach") else setattr(servo, "value", None)
            else:
                servo.angle = want
        self._state = (left, right)

    def close(self):
        for servo in (self.left, self.right):
            try:
                servo.detach()
            except Exception:
                pass
            servo.close()


class PcaBand:
    """the belt's two servos on a pca9685 board over i2c (NUDGE_BAND=pca).

    the same angles and pulse widths as ServoBand, so a stroke feels the same on the belt as on the
    collar. a detached channel is switched fully off, so the servo is silent between strokes.
    channels and address come from NUDGE_BELT_CHANNELS (default "0,1", left then right) and
    NUDGE_BELT_I2C (default 0x40).
    """

    MODE1, MODE2, PRESCALE, LED0 = 0x00, 0x01, 0xFE, 0x06

    def __init__(self, channels=(0, 1), address=0x40, bus=None, busno=1, max_deg=90.0,
                 min_pulse_ms=0.5, max_pulse_ms=2.4, hz=50):
        if bus is None:
            try:
                from smbus2 import SMBus
            except ImportError:
                from smbus import SMBus  # raspberry pi os ships python3-smbus
            bus = SMBus(busno)
        self.bus, self.addr, self.ch = bus, address, tuple(channels)
        self.max_deg, self.lo, self.hi, self.hz = max_deg, min_pulse_ms, max_pulse_ms, hz
        w = bus.write_byte_data
        w(address, self.MODE1, 0x10)                               # asleep, to set the prescaler
        w(address, self.PRESCALE, round(25e6 / (4096 * hz)) - 1)    # 25 MHz oscillator -> 50 Hz
        w(address, self.MODE1, 0x00)
        time.sleep(0.005)                                           # the oscillator settles
        w(address, self.MODE1, 0xA0)                                # restart, auto increment
        w(address, self.MODE2, 0x04)                                # totem pole outputs
        self._state = (None, None)
        for c in self.ch:
            self._off(c)

    @classmethod
    def from_env(cls):
        chans = os.environ.get("NUDGE_BELT_CHANNELS", "0,1").split(",")
        return cls(channels=(int(chans[0]), int(chans[1])), address=int(os.environ.get("NUDGE_BELT_I2C", "0x40"), 0))

    def _count(self, deg):
        deg = max(0.0, min(self.max_deg, deg))
        ms = self.lo + (self.hi - self.lo) * deg / self.max_deg
        return round(ms * self.hz / 1000.0 * 4096)

    def _off(self, c):
        self.bus.write_i2c_block_data(self.addr, self.LED0 + 4 * c, [0, 0, 0, 0x10])

    def apply(self, left, right):
        if (left, right) == self._state:
            return
        for c, want in zip(self.ch, (left, right)):
            if want is None:
                self._off(c)
            else:
                n = self._count(want)
                self.bus.write_i2c_block_data(self.addr, self.LED0 + 4 * c, [0, 0, n & 0xFF, n >> 8])
        self._state = (left, right)

    def close(self):
        for c in self.ch:
            try:
                self._off(c)
            except Exception:
                pass
        try:
            self.bus.close()
        except Exception:
            pass


TOUR = ["bear_left", "left", "bear_right", "right", "uturn",
        "floor", "arrive", "hazard", "off_route", "unmapped"]


def _wall_clock():
    import time
    t0 = time.monotonic()
    return (lambda: int((time.monotonic() - t0) * 1000),
            lambda ms: time.sleep(ms / 1000.0))


def _virtual_clock():
    """no sleeping: a dry run should be instant and identical every time.

    only wait() moves time, exactly as the wall clock behaves — reading the time must not
    change it, or a loop that reads it twice drifts against one that reads it once.
    """
    t = [0]

    def now():
        return t[0]

    def wait(ms):
        t[0] += ms

    return now, wait


def clock(virtual=False):
    """(now_ms, wait_ms). the band and the runner both pace themselves through this."""
    return _virtual_clock() if virtual else _wall_clock()


def selftest(band=None, cues=None, gap_ms=1200, virtual=False, step_ms=5):
    """walk every cue so whoever is wearing the collar can tell them apart.

    python -m nudge.band            # on the pi, real servos
    python -m nudge.band --dry      # anywhere: no gpio, no waiting
    """
    now, wait = clock(virtual)
    owned = band is None
    if owned:
        band = ServoBand()
    engine = Engine()
    try:
        for cue in (cues or TOUR):
            if cue not in CUES:
                print(f"  {cue}: no such cue")
                continue
            print(f"  {cue}")
            # play refuses while the gears are resting, so wait it out rather than skip
            while not engine.play(cue, now(), force=True):
                wait(20)
            while engine.busy:
                band.apply(*engine.tick(now()))
                wait(step_ms)
            band.apply(None, None)
            wait(gap_ms)
    finally:
        if owned:
            band.close()


def main(argv=None):
    import sys

    argv = sys.argv[1:] if argv is None else argv
    cues = [a for a in argv if not a.startswith("-")] or None
    if "--dry" in argv:
        band = NullBand()
        selftest(band=band, cues=cues, virtual=True)
        taps = sum(1 for _, left, right in band.commands if left or right)
        print(f"{len(band.commands)} servo state changes, {taps} strokes, no gpio touched")
        return 0
    selftest(cues=cues)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
