# The band on a Raspberry Pi

Two servos tap the traveller's shoulders. Direction is which shoulder; magnitude is how
many strokes. The Pi runs `nudge` itself, so there is no BLE pairing and no phone in the
critical path — one less thing to fail on stage.

## Wiring

| Servo | Signal | Power |
|---|---|---|
| Left | BCM **12** (hardware PWM0) | external 5V |
| Right | BCM **13** (hardware PWM1) | external 5V |

GPIO 12 and 13 are the two hardware-PWM pins. Software PWM on other pins jitters, and the
horn twitches at rest, which you can feel through a collar.

**Do not power the servos from the Pi's 5V pin.** Two SG90s can pull well over an amp
between them when they stall, which browns out the Pi mid-demo. Give them their own supply
— the 4×AA pack — and tie the grounds together so the PWM signal has a reference.

Override the pins if you wire it differently:

```python
ServoBand(left_pin=12, right_pin=13)
```

## Smoke test

No Pi, no GPIO, instant — run this anywhere to check the grammar:

```bash
python -m nudge.band --dry
```

On the Pi with the collar on someone:

```bash
python -m nudge.band
```

It walks every cue with a pause between, printing each name as it goes. That's the one to
run with a person wearing it, to check the cues are actually distinguishable. Pass cue
names to repeat just those:

```bash
python -m nudge.band left right hazard
```

For steadier pulses, start the pigpio daemon first — the code picks it up automatically and
falls back quietly if it isn't running:

```bash
sudo pigpiod
```

## Driving it from a walk

`nudge/band.py` only knows how to tap a shoulder. `nudge/bandrun.py` is the loop that
connects it to a navigator:

```bash
python -m nudge.bandrun synthetic --to "Conference Room" --dry   # anywhere, instant
python -m nudge.bandrun synthetic --to "Conference Room"         # on the pi, real servos
```

That plans a route, simulates someone walking it, and drives the band the whole way while
printing what would be spoken. `--wobble 0.5` makes the walk untidy; `--pace` sets the
milliseconds per half-metre step (420 is about walking speed).

To drive it from something other than the simulator — the debug view, iMessage, or real
localization when it exists — use the runner directly:

```python
runner = BandRunner(Navigator(g, goal, "blind"), band=ServoBand())
runner.start(x, y, floor, heading, now=t)
...
events = runner.update(x, y, floor, heading, now=t)   # whenever a position arrives
runner.tick(t)                                        # as often as you can, forever
```

**A position is not a tick.** Positions arrive at walking pace — every 400 ms or so — but a
stroke only renders if `tick()` is called every few milliseconds. Call `update()` when a
position arrives and `tick()` continuously in between, or the horn never moves. There is a
test for exactly this (`test_a_position_is_not_a_tick`), because it is the easy thing to get
wrong when wiring the band into something new.

The runner owns no clock and starts no thread, so whatever is already running the show —
the server, the CLI, a `while True` on the Pi — keeps control of the loop.

## The grammar

| Cue | What you feel |
|---|---|
| `bear_left` / `bear_right` | 1 light stroke, that shoulder |
| `left` / `right` | 2 strokes, that shoulder |
| `uturn` | 3 strokes, both |
| `floor` | 2 long strokes, both |
| `arrive` | 3 quick strokes, both |
| `hazard` | 3 fast strokes, both |
| `off_route` | 1 long press, both |
| `unmapped` | 2 slow strokes, both, repeating until you are back on the map |
| steering | 1 light tap on the side to turn toward |

## What changed from the ESP32 band

`firmware/nudge_band` drove two vibration motors, and its grammar is written in on/off
milliseconds — `hazard` is 30 ms on, 30 off, five times. A servo cannot do that. An SG90
needs roughly 100 ms to swing 30° and as long to come back, so the shortest stroke a
shoulder can feel is nearer 300 ms.

So `nudge/band.py` is a redesign, not a port. Three things are genuinely different:

- **Steering.** The motor version pulsed faster the further you drifted, down to every
  300 ms at 90°. A servo doing that would wear out and be unbearable. Here it is one light
  tap on the side you should turn toward, never more than once every 1.5 s.
- **The horn detaches between strokes.** A held SG90 hums and draws current the whole time.
- **Patterns are rate limited.** A burst of cues can't thrash the gears. Important cues
  (hazard, off route, arrive) can cut in over one already playing, but never skip the rest
  after a pattern ends.

Nothing upstream changed. The wire protocol in `nudge/device.py` is untouched, and the
engine consumes the same live packet, so the debug view and the simulated walk still work.

The timing logic takes `now` as a millisecond argument instead of reading a clock, same as
`haptics.h` did, so `tests/test_band.py` covers all of it with no hardware and no waiting.
