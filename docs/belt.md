# The belt

The belt is the wearable that is actually built: two servos on a PCA9685 board, a Raspberry Pi, and the
traveller's iPhone as the step and heading sensor. The Pi runs its own on-body runtime (a separate
codebase, not this repository). That runtime owns everything from the steps down: it reads the phone,
keeps the wearer's position, and fires the taps.

This repository owns everything before the steps: the building, the route, the words. The two meet at a
handful of HTTP calls, in `nudge/belt.py`.

It is separate from the collar in [band-pi.md](band-pi.md). `nudge/band.py` drives servos on GPIO 12 and
13; the belt's servos are on the PCA9685 over I2C, which that driver does not speak. Use one or the other.

## Running it

On the Pi, the on-body runtime is already listening on port 8000. On the laptop:

```bash
NUDGE_BELT_URL=http://172.20.10.2:8000 python -m uvicorn nudge.server:app --port 8765
```

Leave `NUDGE_BAND` unset. Without `NUDGE_BELT_URL` nothing here does anything.

## What happens

| Our call | What the belt is told | Notes |
|---|---|---|
| `POST /api/nav/start` | `POST /route` with the route's points, exactly as `/api/route` returns them | if the belt is mid-walk it is stopped first; it refuses a new route while guiding |
| `POST /api/nav/calibrate` | `POST /calibrate`, then `POST /start` | the wearer is on the first point, facing the second |
| `POST /api/belt/begin {session}` | the same, without a phone compass | for starting from the debug view |
| (every half second while walking) | `GET /state` | the wearer's position is fed to the navigator like a `/api/nav/update`, so speech, the hub and the caretaker follow the belt |
| `POST /api/belt/stop` | `POST /stop` | taps both sides |
| `POST /api/belt/wearer {height_cm}` | `POST /wearer` | scales the belt's step length to the wearer |
| `GET /api/belt` | `GET /state` | `enabled`, `session`, `walking`, the belt's reply to the route, the last `error`, and what the belt reports |

The belt's position comes back in our own model frame, because that is the frame the route was sent in.
Its heading is compass style in that frame (degrees clockwise from +y); `belt.model_heading` turns it into
ours (radians counter-clockwise from +x).

Like the collar, all of this is best effort. A belt that is off, out of range or refusing never turns a
nav request into a 500: the route still plans and speaks, and `GET /api/belt` says what went wrong.

## What the belt cannot do yet

- **Change floor.** It follows the route up to the first change of floor and reports how many points it
  dropped (`route.dropped_other_floors`). Send it the next floor's route once the wearer is there.
- **Take a new route mid-walk.** A reroute has to stop the belt and calibrate again at the start of the
  new route, because it lines itself up by having the wearer face the first leg.
- **Play our cue grammar.** It taps left or right to steer, and has a stop and an arrival pattern. There is
  no hazard, off-route or floor cue on it.
- **Two position sources.** While the belt is walking, do not also post `/api/nav/step` from the phone
  app for the same session; the two would disagree about where the wearer is.

## Tested

`tests/test_belt.py` runs the real server against a stand-in belt: the route is sent in our format,
calibrate lines it up and starts it, its position moves our navigator, arrival ends the feed, a new walk
takes the belt over, and a belt that is off never breaks a walk.

It has also been run against the real on-body runtime in simulator mode on a laptop: the synthetic
building's route was handed over (five points on the first floor, two on the second dropped), the walk
was started, and after four steps both sides had the wearer at (4.0, 1.5) facing along the first leg.
It has not yet been walked on the belt itself with a route from here.
