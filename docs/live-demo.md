# Live demo: a real person, the Pi collar, the phone

A blind traveller wears the servo collar (see [band-pi.md](band-pi.md)) and carries an iPhone running the Nudge app.
The phone counts steps and reads its compass; the Pi runs Nudge, keeps the traveller on the building's walk
graph, and taps the left or right shoulder in real time. The phone speaks distances and turns.

## 1. Network

Turn on the iPhone's Personal Hotspot. Join the Pi to it (and a laptop, if you want to watch the debug view).
Venue wifi usually blocks devices from talking to each other; the hotspot doesn't.

Find the Pi's address on the hotspot: `hostname -I` on the Pi.

## 2. On the Pi

```bash
sudo pigpiod                                        # steadier servo pulses (optional, picked up automatically)
NUDGE_BAND=1 python -m uvicorn nudge.server:app --host 0.0.0.0 --port 8765
```

The collar attaches to the first walk that starts. `GET /api/band` shows `mode: servo` (or `dry` with the
reason in `error` if the servos couldn't be opened). Use `NUDGE_BAND=dry` on a laptop.

## 3. On the phone

In the Nudge app, set the server address to `http://<pi-ip>:8765`, pick the building and the destination.

## 4. At the entrance

1. **Start.** The app calls `POST /api/nav/start` with `start: "entrance"` (no coordinates needed). The server
   puts the traveller at the building's entrance and returns `start_heading_deg` when the building is on the map.
2. **Face the first direction and Calibrate.** The traveller faces the way the first instruction says; the app
   sends `POST /api/nav/calibrate {session, heading_deg}`. This lines the compass up with the building.
3. **Walk.** The app sends `POST /api/nav/step {session, steps, heading_deg}` after each step (or every half
   second with the new step count). Each reply has `say` (lines to speak), the collar is updated on the Pi.

## What the wearer feels and hears

- **Collar:** one light tap on the shoulder to turn toward when drifting; two strokes on a shoulder for a turn,
  one for a slight turn; three on both for arrival; a long press on both when off route. Full table in band-pi.md.
- **Phone:** the turn and doorway lines ("Walk 8 meters, through a narrow doorway, into the kitchen"), "Turn left
  now", room changes, and a short distance line at 20, 10 and 5 metres ("10 meters to the next turn").

## How position is worked out

- Steps move the traveller **along the route** (default 0.7 m a step), not in open space, so step-count error
  stays along the corridor instead of putting them inside a wall.
- The compass only matters when it disagrees with the route for 3 steps or more:
  - 50 to 135 degrees off: they took another way. Nudge follows the walk-graph edge that matches their heading,
    the navigator notices they've left the route and plans a new one from there.
  - more than 135 degrees off: they turned around; Nudge walks them back along the route.
  - a single odd reading is ignored.
- **Stairs:** 16 steps on a staircase moves them to the next floor. **Elevator:** after 10 s standing in it, the
  first step out puts them on the new floor. The app can also send `floor_confirm: true`.

## Troubleshooting

- **Compass swings near steel, elevators, or radiators.** Calibrate a metre or two away from them. Wild readings
  only matter if they last 3 steps.
- **Arriving early or late.** Step length varies; send `step_m` (e.g. 0.6 for short steps). Calibrating by walking
  a known 10 m and counting steps gives a good value.
- **Collar silent.** `GET /api/band` — check `attached`, `mode` and `error`. Positions faster than about 300 ms
  apart can outrun the servos (cues are dropped, not queued).
- **No phone handy.** Replay a recorded walk on the Pi:

  ```bash
  python -m nudge.pdr --make-walk walk.jsonl --sid MgGNVWsyXtH --to Kitchen
  python -m nudge.pdr --replay walk.jsonl --sid MgGNVWsyXtH --to Kitchen          # real servos
  python -m nudge.pdr --replay walk.jsonl --sid MgGNVWsyXtH --to Kitchen --dry    # anywhere
  ```

  Each line is `{"t": seconds, "steps": n, "heading_deg": compass}`.
