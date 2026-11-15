> This folder is the belt's on-body runtime: what runs on the Raspberry Pi on the belt. It is a self-contained
> project, copied here from its own repository on 15 Nov. Run everything in it from inside `belt/`.
> The server side that plans routes is the rest of this repository; the two meet in `nudge/belt.py`
> (see [docs/belt.md](../docs/belt.md)).

# Isaac on-body runtime

The project is called **Isaac** outside the team (renamed from Nudge on 14
Nov). The two pages people see, the viewer and the phone page, say Isaac. The
code keeps the old name everywhere else, on purpose, so nothing that runs has
to change: the Python package and command (`nudge`), the service on the Pi,
the certificate file, the repository and its paths.

Isaac is a wearable guide for blind visitors: a belt with a Raspberry Pi and
two servo pods, one on each hip, taps the wearer's left or right side to steer
them along an indoor route. (The first design was a collar tapping the
shoulders; the code still says "shoulder" in places.) This repo is what runs on the body: sensing, position, route following
and taps. Generating the route (Matterport scan, aerial imagery, external APIs)
is a teammate's job and enters here only as a list of waypoints.

```
iPhone (Safari page)  --heading, steps-->  Pi: Localizer -> Follower -> TapDriver --> hip pods
                                               |                                  
laptop (viewer page)  <------ /state ----------+---- JSONL log (replayable)
```

The iMessage front end (text a destination, hear the route, reply GO) is a
separate program on the laptop that drives this runtime over HTTP:
[bridge/README.md](bridge/README.md).

**Status.** Everything here runs and is tested on a laptop with simulated
sensors and mock servos (270 tests). On the Pi (14 Nov), the PCA9685 answers
at address 0x40 and all six tap primitives ran against it: the chip was read
back putting out the commanded pulse on each channel, and `get_throttled`
stayed `0x0`. On the finished belt the wearer feels both pods at a 45 degree
swing, the iPhone streams to the Pi at 15 Hz, and a straight walk with the
phone in a back pocket gave a steady heading. **Not yet done on hardware:** a
guided walk, a route from the upstream planner, other wearers, and the systemd
unit. See [Hardware checks](#hardware-checks).

## Wiring

| From | To | Purpose |
| --- | --- | --- |
| Pi pin 1 (3V3) | PCA9685 VCC | logic supply |
| Pi pin 3 (GPIO2, SDA) | PCA9685 SDA | I2C data |
| Pi pin 5 (GPIO3, SCL) | PCA9685 SCL | I2C clock |
| Pi pin 6 (GND) | PCA9685 GND | ground |
| Pi pin 2 (5V) | PCA9685 V+ | servo power |
| PCA9685 channel 0 | left pod servo (SG92R) | tap left hip |
| PCA9685 channel 1 | right pod servo (SG92R) | tap right hip |

The PCA9685 (HW-170) is at I2C address `0x40`. The servos take their power
from the Pi's 5 V pin, which is why the driver never starts both within 50 ms
of each other and why `vcgencmd get_throttled` is on the checklist.

## Install

On the Pi (Raspberry Pi OS Lite 64-bit). `pip` needs internet once, so share
the laptop's connection over Ethernet or use the iPhone tether for this step.

```bash
sudo raspi-config nonint do_i2c 0
sudo apt install -y python3-venv i2c-tools openssl
i2cdetect -y 1                       # expect 40 (and 70) in the grid
python3 -m venv .venv
.venv/bin/pip install -e ".[pi]"
```

To copy the repo from the Windows laptop without its `.venv`:
`git archive -o nudge.tar.gz HEAD`, `scp nudge.tar.gz <user>@<pi>:`, then
`mkdir nudge && tar -xzf nudge.tar.gz -C nudge` on the Pi.

On a laptop (Python 3.11+), for the simulator and tests:

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"    # .venv/bin/pip on macOS/Linux
```

Dependencies: `aiohttp` for the web server and `pytest` for tests. Nothing
else is required.

**The taps need nothing installed.** The servo driver talks to the PCA9685
through `/dev/i2c-1` with the standard library, so on a Pi with no working
route to PyPI this already runs from a plain checkout:

```bash
python3 -m nudge.main taps          # same as `nudge taps` once installed
```

If `adafruit-circuitpython-servokit` is installed (`pip install -e ".[pi]"`)
`driver = "servo"` uses it; otherwise it uses the built-in driver.
`driver = "pca9685"` always uses the built-in one. `aiohttp` is imported only
when the web server starts.

## Build steps and how to check each

| Step | Command | What you should see |
| --- | --- | --- |
| 1. Actuators | `nudge taps` on the Pi (`nudge taps --mock` on a laptop) | tap left, tap right, double tap left, double tap right, stop (both), arrived (left-right three times), then both arms at rest |
| 2. Simulator | `nudge --sim` on a laptop | arrow keys turn 15 deg, space is a step; the tap log prints as you wander off the route. Also drivable from the viewer page |
| 3. Phone | `nudge --tls` on the Pi, open `/phone` on the iPhone | the Pi console prints heading, step count, rate and heading jitter once a second |
| 4. Walk | open `/viewer` on the laptop, Calibrate, Start | route, live pose, trail and tap events; ARRIVED and three left-right taps at the end of `demo_L` |
| 5. Boot | `source ~/nudge/bin/activate && sh deploy/install-service.sh` on the Pi (asks for the sudo password; `--print` shows the unit without installing) | `systemctl status nudge` is active after a reboot. **Installed on the Pi on 14 Nov:** `systemctl is-enabled nudge` says `enabled`, and the runtime it started is the one serving. When its process was killed to load new code, the service had it back within seconds. After a power cycle on 14 Nov evening it was up by itself within a minute of boot, with no restarts. To load new code: `git pull`, then `pkill -f 'python -m [n]udge.main'` (no sudo needed; the service restarts it) |

Useful extras:

```bash
nudge taps --jog left 60        # hold one arm at an angle while you find rest/contact angles
nudge taps --soak 300           # tap once a second for 5 minutes, then print vcgencmd get_throttled
nudge --sim --script routes/demo_L.walk --speed 4    # unattended scripted walk
nudge replay logs/walk-XXXX.jsonl                     # re-run a logged walk and check it reproduces
pytest                                                # 270 tests, about 15 seconds
```

Rest and contact angles in `config.toml` (`[actuators.left]`, `[actuators.right]`)
are placeholders: 90 deg rest, a 40 deg swing, mirrored. Tune them on the body
with `--jog` before anything else.

## Running a walk

1. `nudge --tls` on the Pi. It prints the viewer and phone addresses.
2. iPhone: open `https://<pi>:8443/phone`, tap **Start sensors**, allow motion
   access. The page keeps the screen awake and streams at 15 Hz.
3. Laptop: open `http://<pi>:8000/viewer`.
4. The wearer stands on waypoint 0 facing waypoint 1. Press **Calibrate**
   (key `C`), then **Start** (`S`). **Stop** (`X`) taps both hips.
   With no laptop, start from the phone page itself: pick the route and the
   wearer's height, press **Start walk**, pocket the phone, face the first leg
   and stand still. The page loads the route, counts down 8 seconds, waits
   until the Pi sees 3 seconds of steady stream with no steps, then calibrates
   and starts, saying each stage aloud. It gives up after 40 seconds if the
   wearer does not stand still. Tested against the mock belt on 15 Nov, not
   yet on the iPhone: the spoken cues in particular are untried on iOS.
5. If the dot on the map and the wearer drift apart: when the wearer is
   standing on a waypoint, pick it under **Wearer is at** and press **Fix
   position** (key `M`). See "Fixing the position by hand" below.

### The short demo for a crowded room

`demo_3m_right` needs no map, no server and about four metres of floor: 3 m straight, a right turn at the
3 m mark, and it ends a step or two after the turn. Stand anywhere, face the way the 3 m should go,
Load route, Calibrate, Start.

- Walk straight and it is silent.
- Turn off the line by more than 20 degrees and it taps the side to come back to, once a second, until
  you are within 10 degrees again.
- About a metre before the 3 m mark it taps right, then double-taps right, so that the turn lands on the
  mark. If you walk past without turning it keeps double-tapping right.
- One step after the turn it plays the arrival pattern and stops.

Checked in simulation on the shipped settings (`tests/test_demo_routes.py`). **Not walked.**

### Fixing the position by hand

Position comes from counting steps, so a miscounted step or a wrong step
length moves the dot away from the wearer, and nothing on the belt can see
that. Someone watching can. **Fix position** (`POST /mark {"waypoint": 2}`;
no body means the next waypoint) says "the wearer is standing on this
waypoint now":

- the position is put on that waypoint, and guidance carries on along the leg
  that leaves it. The heading is left alone, so a wearer who has not turned
  yet is tapped round;
- if the leg just walked was walked from a known point, it also measures the
  step: distance / steps. That is used from then on when it is within 30
  percent of the current step length; further off than that is more likely a
  miscount or an early press, so it is shown in the viewer and not used;
- if the count had run ahead and the belt had already played "arrived", fixing
  the position on an earlier waypoint resumes guidance;
- marking the last waypoint ends the walk with "arrived".

It works while guiding, which is when it is needed, and it is refused before
calibration. The list names waypoints as the map does (`1`, `2`, ... `end`)
and follows the waypoint being walked towards until another is picked. The
fix is an input in the log (`mark`), so a replay applies it at the same
moment. What it cannot do is help between waypoints: it needs the wearer to
be on one. Checked in tests and in the viewer against the simulator, **not
yet on a walk**.

### Phone in the back pocket

`config.toml` is currently set up for the phone riding in a back pocket rather
than fixed upright on the belt.

**Measured on 14 Nov** (one wearer, four straight passes of 4 m, 8 steps each,
phone in the back pocket, guidance off):

- Raw heading swung up to 20 degrees off the direction of travel with each
  stride. Averaged over one second it stayed within 6 to 12 degrees, under the
  20 degrees at which taps start.
- Turning round read as 178 to 185 degrees, and the heading for the same
  direction came back within 6 degrees over a minute: no drift worth the name.
- Steps counted per pass: 8, 9, 8 and 6 against 8 taken. Hence
  `step_length_m = 0.5` (4 m in 8 steps), good to about a step either way.
- The phone stopped sending for 18 seconds during a fifth pass, while still in
  the pocket. The cause is not known; see Guided Access below.

**A guided walk with the phone in the pocket has not been done yet.**

- `heading_source = "gyro"`: the heading is the direction the back of the
  phone faces, from the gyro. It does not care about the Pi and power bank
  next to the pocket, which can disturb the compass, and it does not change
  when the phone slides or turns flat within the pocket. It drifts slowly.
- `heading_smooth_s = 1.0`: the heading is averaged over about one stride, so
  the swing of the pocket with each step does not look like turning. The log
  keeps the raw reading; the console's "jitter" figure shows the raw swing.
- On the phone: turn on **Guided Access** with touch disabled and its display
  auto-lock set to Never. Safari only delivers sensor data while the page is
  on screen, and a pocket will otherwise swipe it away (the Pi then stops the
  wearer, as it should).
- Put the phone in the pocket **before** Calibrate or GO, and set the step
  threshold slider before that.
- A back pocket may feel only one leg's steps. Walk a measured 10 m and set
  `step_length_m` to the distance per *counted* step.

To go back to a phone fixed on the belt, set `heading_source = "compass"` and
`heading_smooth_s = 0`.

No laptop link? `nudge --tls --go` calibrates and starts by itself once
the phone has streamed for 3 seconds, so starting the phone page while standing
on waypoint 0 facing waypoint 1 is the whole procedure.

The same controls over HTTP, for scripts and for the teammate's pathfinder:

```bash
curl -X POST http://<pi>:8000/route -H "Content-Type: application/json" \
     -d '{"waypoints": [[0,0],[0,5],[5,5]]}'
curl -X POST http://<pi>:8000/route -H "Content-Type: application/json" -d '{"name": "demo_L"}'
curl -X POST http://<pi>:8000/calibrate
curl -X POST http://<pi>:8000/start
curl -X POST http://<pi>:8000/stop
curl http://<pi>:8000/state
```

**A different wearer.** The viewer has a Height slider. Letting go of it
sends `POST /wearer {"height_cm": 160}`, and the Pi scales the step length in
proportion: `step_length_m` is taken to be right for a wearer `height_ref_cm`
tall (0.5 m at 175 cm, measured), so 160 cm gives 0.46 m. It applies from the
next step and to later calibrations, is written to the log, and can be changed
mid-walk. This is an estimate from height alone: people of the same height do
not all step the same.

Commands that cannot be carried out answer `409` with a reason, for example
calibrating with no sensor data. A new route needs a new calibration, unless it
is sent as a new route for the walk under way (`"reroute": true`, below).
**While guidance is running, `/route` and `/calibrate` are refused (`409`):
send `/stop` first.** Either one would otherwise end guidance silently, with
no stop tap for the wearer.

## Working with the upstream stack

The team's main repository (`arkankau/hackprinceton-26fa`, branch
`before-hardware`) owns everything before the steps: CoStar and Matterport
data, the planner, the LangChain agents, speech and the Photon iMessage bot.
This runtime owns the belt: steps and heading in, taps out. The two meet at
five HTTP calls to the Pi. Nothing else needs to be shared, and neither
codebase imports the other (both happen to be Python packages called `nudge`,
so they cannot be installed side by side; the upstream server runs on the
laptop, this one on the Pi).

| Upstream wants to | Call on the Pi | Notes |
| --- | --- | --- |
| send a route | `POST /route` with the body of its own `/api/route` answer | `{"route": {"points": [[x, y, floor], ...]}}` is accepted as it is. Same frame: metres, x right, y up. A route that changes floor is walked one floor at a time (below). |
| change the route mid-walk | the same `POST /route` with `"reroute": true` added | position, calibration and step length are kept and guidance goes on; see below |
| say the wearer is on the next floor | `POST /floor` | the walk waits at a change of floor (`state` is `FLOOR`) until this is called |
| set the wearer | `POST /wearer {"height_cm": 160}` | optional; scales step length |
| begin | `POST /calibrate`, then `POST /start` | the wearer stands on the first point facing the second |
| stop | `POST /stop` | taps both sides |
| follow progress | `GET /state` | `pose` is in the route's own frame, so it can be drawn on the upstream map; `state` becomes `ARRIVED` |

The answer to `POST /route` says what was kept: `given`, `used` (on the first
floor, after simplifying), `floors` and `dropped_other_floors`. `/route` and `/calibrate` are refused
with `409` while guidance is running. `bridge/wearable.py` is a ready-made
client for these calls using only the standard library.

**A new route mid-walk.** `POST /route` with `"reroute": true` replaces the
rest of the walk without stopping it: the position, the calibration and the
step length are kept, and the taps carry on towards the new route. It is
picked up from where the wearer is. Within `route.rejoin_m` (2 m) of the new
route, the points already behind them are dropped and the first leg runs from
where they stand to the next point; further away, the first leg leads them to
the route's start. If guidance was not running (stopped, or arrived), the
route is loaded the same way and waits for Start, with no calibration: this
is how a second destination follows the first. It is refused before
calibration (there is no position to carry on from), and a route that ends
where the wearer already stands is refused too.

**A route that changes floor.** Points carry their floor as a third element.
The route is split where the floor changes and each floor's stretch is walked
as a route of its own. At the end of a stretch that is not the last, the belt
plays the stop pattern (both pods) instead of "arrived", and `state` becomes
`FLOOR`: the wearer is at the stairs or the lift. The belt cannot tell when
they are on the next floor (steps on stairs are not steps along a corridor,
and a lift has none), so someone has to say so: **Next floor** in the viewer
(key `F`), or `POST /floor`. The position is then put on the first point of
the next floor's stretch and guidance carries on, tapping the wearer round to
face it. The compass reference is kept across the stairs. `route.floors =
false` goes back to ending the walk at the first change of floor.

Assumptions: the planner's coordinates are in one frame across floors (the
upstream ones are), and the wearer is helped on the stairs. A blindfolded
wearer on stairs with a prototype belt is not something to demonstrate; the
stop at the change of floor is there so a person takes over.

Both are checked in tests, in replay, and in the viewer against the
simulator. **Neither has been walked**, and the upstream server does not
send `"reroute"` or call `/floor` yet.

A planner's route threads through scan positions and wiggles by a few
decimetres. `route.simplify_m = 0.4` removes points within 0.4 m of the line
through their neighbours, so the wearer is not tapped for every wiggle; real
corners are kept, and so is the first leg, because calibration means facing
along it. Set it to 0 to follow every point.

**Tried together on a laptop, not yet on the belt.** With this runtime in
simulator mode and the upstream server on its synthetic building, upstream
planned a route, handed it over, started the walk, and its navigator followed
this runtime's position: after four steps both had the wearer at (4.0, 1.5)
facing along the first leg. The upstream side of that is `nudge/belt.py` and
four hooks in their server, merged into `before-hardware` on 14 Nov
(https://github.com/arkankau/hackprinceton-26fa/pull/2; the same change is
kept here as `upstream/belt-handoff.patch`). The merged branch (`4f2bb63`) was
checked again the same way: its belt tests pass and it handed a route to a
belt runtime with mock taps. A real planner route walked on the belt is still
to do.

The upstream traveller app for the phone is not used with the belt: steps and
heading come from this runtime's own phone page. That leaves four things only
that app does. It registers the traveller (which gives the six-digit code a
caretaker follows them with, by iMessage or the caretaker app), it takes the
traveller's spoken requests, it starts a trip a caretaker sends, and it speaks
the turn instructions. `venue_route.py --traveller NAME` (below) covers the
first; the other three have no stand-in, so with the belt a walk is started
by an operator and the wearer is guided by taps alone.

### The team's iOS app as the sensor

The phone page is one way to get steps and a heading into this runtime. The
other is the team's traveller app (Isaac), which is what the demo is moving
to. The app is not changed and does not talk to this runtime: it reports to
the upstream server as it always has (`POST /api/nav/step` with new steps
and compass, `POST /api/nav/calibrate` with the compass while facing the
route), and the server passes each reading on to `POST /sensor` here as
`{"heading_deg": ..., "step_count": ..., "sid": "isaac-app"}`.

Nothing in this runtime had to change for that. `/sensor` already takes a
`heading_deg` and a running `step_count` from whoever sends them, and only
the change in heading since calibration matters, so the app's compass and
the page's gyro yaw are interchangeable.

What is different with the app:

- **Readings come about once a second**, not fifteen times. The server
  repeats the last one in between (for up to 2.5 s) so the one-second
  "sensor silent" stop does not fire between reports. Taps are decided on
  each reading, so they can come up to half a second later than with the page.
- **The step length is still this runtime's** (the height slider, or the
  first long leg). The app's own step-length setting goes to the server and
  is not used for the belt.
- **Only one sensor at a time.** If the phone page is open on another phone
  while the app is reporting, the second one is refused as busy.
- **The app speaks.** The turn instructions come back to it in the replies
  to its step reports.

The server side of this is in pull request 3 on the team repository. Checked
on a laptop with this runtime in its normal phone mode (mock taps), the
upstream server, and a script sending what the app sends: lined up on the
compass reading, tracked four legs, tapped at the corners, arrived, with no
stop from a silent sensor. **The real app has not been run against the belt,
and none of it has been walked.**

### A route in the venue

The venue is Matterport scan `R1FX6EmdcDt` (Rabinowitz Hall). Its data is not
in either repository: the upstream server downloads it once with
`MATTERPORT_API_KEY` and keeps it in its git-ignored `cache/` folder. So a
venue route can only be planned on the laptop where that server runs.
`upstream/venue_route.py` does it there, with nothing but Python:

```bash
python venue_route.py --list          # the points on each floor, with their ids
python venue_route.py                 # two points 6 to 12 m apart on the biggest floor, with a turn if it finds one
python venue_route.py --from A --to B # two points of your choosing
python venue_route.py --begin SESSION # the wearer is on the start, facing the second point
python venue_route.py --traveller Sam   # also register the wearer; prints the code a caretaker follows
```

`--server` (default `http://localhost:8765`) and `--belt` (default
`http://172.20.10.2:8000`) say where the two sides are; `--seed N` gives a
different pair; `--dry` plans without sending. It prints the start, the goal,
the route length, the upstream turn instructions and which way to face.

If the server has the hand-off patch and was started with `NUDGE_BELT_URL`,
the walk is started through the server (`/api/nav/start`), so the upstream
dashboard, speech and caretaker follow the same trip. Otherwise the route from
`/api/route` is posted straight to the belt, which guides just the same, and
the script says that the upstream dashboard will not show the walk.

Assumption: "two arbitrary points" means two scan positions on one floor, far
enough apart to be a walk and close enough to finish in under a minute. The
6 to 12 m range is that guess; `--min` and `--max` change it.

Checked on a laptop against the upstream synthetic building, both ways:
through the server (session started, the belt took 4 of 4 points, a wrong
session and a belt with no phone data were refused with the reason, and a
registered wearer showed up for a caretaker as walking with the route) and
straight to a belt runtime with mock taps. **Not yet run against the venue
scan**, which this laptop does not have, and not yet walked. A caretaker
following by iMessage with the printed code was not tried.

### Keeping a route on the belt

A walk depends on nothing but what is on the wearer: the Pi holds the route,
the phone in the pocket is the sensor and the hotspot. The planner, the
server and any laptop are needed once, to make the route, and not after. To
make that last through a restart, keep the route on the Pi:

- **Keep route** in the viewer asks for a name and writes the loaded route to
  `routes/<name>.json` on the Pi (`POST /route/save {"name": "floor3_loop"}`).
- `venue_route.py ... --name floor3_loop --keep` does the same in one go.

From then on it is in the viewer's route list and loads with **Load route**,
with no server running anywhere. It is saved as it was given, every floor, so
loading it is the same as being sent it again. Names are letters, digits, `-`
and `_`; names starting with `demo` are the built-in routes and are refused.

What still has to be within the phone hotspot's range is the viewer, for
Calibrate, Start, Stop and Fix position. It runs on a phone, so the person
walking beside the wearer carries it. If every viewer drops off, guidance
carries on; only the buttons are gone until one is back in range.

Checked in tests and by keeping and reloading a route on a laptop runtime.

### A loop of corridors

A planner only knows the shortest way between two points, and a loop is not
one. `venue_route.py` works a loop out itself, on the planner's own graph (its
points and the walkable edges between them, on one floor):

```bash
python venue_route.py --loop --floor "Floor 3"            # the longest loop on that floor, 20 to 150 m
python venue_route.py --loop --from ID                    # the longest loop through that point
python venue_route.py --from A --via B C D --loop         # through those points in order, back to A
python venue_route.py --from A --via B --to C             # through B on the way to C
```

Each stretch is the shortest way that does not reuse a corridor already
walked or pass a point already visited. A loop it finds by itself is started
on its longest straight, because the belt lines itself up by having the
wearer face along the first stretch; a start that bends within 2 m gets a
warning. `--dry` prints the loop without sending it, `--name` and `--save`
write it as a file in this runtime's own format, to keep in `routes/` on the
Pi. The longest loop on a floor may not be the one you mean (it can cut
through a room), so check the points it prints against the map, and pin it
with `--via` at the corners if it is wrong.

A loop or a via-route always goes straight to the belt, because the upstream
server's walk has one destination. The belt's own viewer is then the live
map; the upstream dashboard does not show this walk.

**Long legs need two settings, and both are set in `config.toml`:**

| Setting | What it does on a long corridor |
| --- | --- |
| `turn_window_frac = 0.2` | a turn is taken for the corner if it happens within a fifth of the leg's length of it (4.8 m on a 24 m corridor) instead of the fixed 3 m. The step count is further out by the end of a long leg, and in a corridor the walls decide where the turn is |
| `calibrate_step_length = true`, `calibrate_min_leg_m = 12` | at the first corner, the step length becomes first leg length / steps counted on it, if that leg is at least 12 m. Short legs measure nothing, as before |

Legs under 15 m behave exactly as they did.

Checked in simulation only (`tests/test_corridor_loop.py`): a scripted walker
who turns where the walls are, once round an 84 m loop (24, 18, 24, 18 m) on
the shipped settings, with a real step of 0.6, 0.5 and 0.4 m against the 0.5 m
that is set. All three arrive, every corner puts the position right, and the
first corridor measures the step to within 3 percent. With a step longer than
set, the count is 4 m short at the first corner and is corrected there. With a
step shorter than set, the belt taps "turn" early on the first corridor (7
taps before the corner in the simulation) and is right from then on. Without
the two settings, the first corner is missed and the walk is lost.

So for a real loop: set the height slider before starting, expect the first
corridor to be the rough one, and use **Fix position** at the first corner if
the dot is not there when the wearer is. **Not walked.** The loop finder ran
against the upstream synthetic building, not the venue scan.

### A loop as two walks

The other way to make a loop is the upstream server's ordinary point-to-point
walk, twice: out from A to B, then back from B to A. Each is a normal walk on
the server, so its dashboard follows both. Two things make that work:

- **The walk back must not retrace the walk out.** The planner takes the
  shortest way, which from B to A is the corridor just walked. `--other-way`
  asks for the next way instead, avoiding every corridor of the shortest.
  (Three walks, each under half the loop, need no such thing.)
- **Nobody lines up again at B.** `--continue` hands the new route over as a
  continuation: the belt keeps its position and calibration from the walk
  out, picks the route up from where the wearer stands and starts guiding.
  `--from here` starts from the planner's point nearest to where the belt has
  the wearer.

```bash
python venue_route.py --from A --to B                              # walk out: stand on A facing the route, then --begin
python venue_route.py --from here --to A --other-way --continue    # at B, once the belt has arrived
```

Through the server this needs its hand-off to know continuations and `avoid`
(pull request 3 on the team repository). Without that, or with `--direct`,
the helper works the other way out itself and posts it straight to the belt,
which carries on just the same; the server's dashboard then does not show the
second walk.

Checked against the upstream synthetic building and this runtime in simulator
mode, both ways: the belt arrived at B, took the route back round the other
side without a calibration, and went on guiding from where it stood. **Not
walked**, and not run on the venue scan. The laptop running the server has to
be within reach of the belt's hotspot at B, when the second command is run.

## HTTPS for the phone page

iOS only gives motion and compass data to a page loaded over HTTPS, and only
after a tap. Two ways to get HTTPS; both are supported.

**A. Self-signed certificate: `nudge --tls`.** The server keeps plain HTTP on
port 8000 (for the viewer) and adds HTTPS on 8443 with a certificate it creates
in `certs/` on first use (`openssl` does the work; `certs/` is git-ignored).
Safari will say the connection is not private: Show Details, visit this
website. No internet needed. If Safari will not open a WebSocket to a
self-signed host, the page falls back to plain HTTPS POSTs by itself; the
"Link to Pi" row shows `websocket` or `http post`. To remove the warning
entirely, open `https://<pi>:8443/cert` on the phone, install the profile, and
enable it under Settings > General > About > Certificate Trust Settings. The
certificate names the Pi's hostname (`<hostname>.local`) and the addresses it
had when created; delete `certs/` to make a new one. Your own certificate:
`--tls-cert FILE --tls-key FILE`.

**B. HTTPS tunnel: `nudge` with no TLS flag.** Run any tunnel that forwards to
`http://localhost:8000` on the Pi, for example `cloudflared tunnel --url
http://localhost:8000`, and open `https://<tunnel-host>/phone`. The tunnel
provides a trusted certificate and carries the WebSocket. It needs internet on
the Pi, and every reading goes out to the internet and back, so expect more
latency and a stop-and-pause whenever the link stalls for a second.

### iPhone USB tethering (untested)

The plan is for the phone to reach the Pi over its own USB cable:

```bash
sudo apt install -y usbmuxd          # pairs with the iPhone; the kernel's ipheth driver does the rest
# plug the iPhone in, tap "Trust", turn on Personal Hotspot > Allow Others to Join
ip -4 addr                           # a new interface (eth1 or enx...) with 172.20.10.x
```

Then open `https://172.20.10.x:8443/phone` (or `https://<hostname>.local:8443/phone`)
on the phone. Whether the phone can open a page on its own tether client is
exactly what needs checking. If it cannot, the sensor transport is one small
interface (`PhoneSource.feed`), already fed from both `/ws` and `POST /sensor`,
so another link (hotspot Wi-Fi, a tunnel) needs no change elsewhere.

The laptop viewer needs its own path to the Pi during the demo: the Ethernet
cable, or the laptop joined to the same iPhone hotspot.

## Route input

```json
{"waypoints": [[x_m, y_m], [x_m, y_m], ...]}
```

Loaded from `routes/*.json` (the default is `routes/demo_L.json`: 5 m
straight, 90 degrees right, 5 m straight) or `POST /route`. The frame is a
flat plan view in metres with **x to the right and y up**, so walking along +y
and then turning towards +x is a right turn. If the pathfinder's frame has y
pointing down (image coordinates), set `route.flip_y = true`, otherwise every
left and right will be swapped. Repeated points are dropped; a route needs at
least two distinct waypoints.

## How it decides

**Calibration.** The compass reading (averaged over the last 0.5 s) at the
moment of `/calibrate` is stored as the reference for "facing along the first
leg". Only changes from it matter afterwards, so north offset, declination and
the phone's mounting angle all cancel.

**Follower** (`follower.py`, a pure state machine: IDLE, NAVIGATING, ARRIVED,
STOPPED). Heading error to the next waypoint is wrapped to +/-180. It starts
tapping when the error exceeds 20 deg and keeps going until it is back under
10 deg. It taps the side to turn towards: one tap a second, or one double tap
a second (two taps a second) above 60 deg. Within 1.0 m of a waypoint it moves
on to the next; at the last one it plays `arrived()`.

**Turning too far.** A tap says which way, not how much, and with the phone in
a pocket the heading is a one-second average, so the follower sees a turn
late. Two rules keep a turn from becoming an overturn, and call the wearer
back when it does:

| Rule | What it does | Switch |
| --- | --- | --- |
| Hold while turning | no new tap while the wearer is already turning the way the tap would ask, faster than `turning_deg_s` (25 deg/s). A second tap then would be answered on top of the first | `turning_deg_s = 0` |
| Overturn | within `overturn_window_s` (4 s) of a tap, for the same waypoint, the error is now past 20 deg on the other side: the wearer turned through it. They get one single tap on the other side (never a double tap, whatever the size), as soon as `overturn_gap_s` (0.5 s) after the last tap instead of a full second. The tap is logged with `reason: "overturn"` and the viewer says "turned too far: back a little" | `overturn_window_s = 0` |

An overturn smaller than 20 deg is inside the deadband and is left alone.

Checked in simulation only (`tests/test_closed_loop.py`), with wearers who
turn 45 to 130 degrees per tap and react after 0.4 to 1.2 s, on the belt's own
settings. Without the two rules, two of the five kept swinging from side to
side and never arrived, and the others took up to 57 taps; with them, all five
arrive within 2 m in 16 taps or fewer. An obedient wearer gets the same taps
or fewer. **Not yet felt on the belt**: whether a real wearer reads a single
tap on the other side as "back a little" is for the first guided walk to say.

**Localizer** (`deadreckon.py`). Each step advances `step_length_m` along the
current heading, with five drift corrections that use the route:

| Rule | What it does | Switch |
| --- | --- | --- |
| Route snap | within `snap_dist_m` (1.5) of the current leg, the pose is projected onto it | `snap_dist_m = 0` |
| Turn snap | a heading change within 25 deg of the expected turn, in 3 s or less, within `turn_window_m` (3.0) of the next waypoint, that is still there 0.15 s later and leaves the wearer facing along the next leg: the pose is set to that waypoint and the next leg begins | `turn_window_m = 0` |
| Step length | after the first turn snap: first leg length / steps counted on it, clamped to 0.35-0.9 m, on a first leg of at least `calibrate_min_leg_m` (12 m) | `calibrate_step_length = false` |
| Compass bias | while walking a leg, the heading-minus-leg-bearing difference is low-passed (5 s) and subtracted; reset at each turn | `bias_tau_s = 0` |
| Freeze | no step for 1.5 s: the pose is frozen | `freeze_after_s = 0` |

Step-length calibration only happens on a first leg of 12 m or more (on a 5 m
leg one step either way is 14 percent), so on short routes the step still
comes from `step_length_m` and the height slider.
`drift_correction = false` turns all five off and leaves plain dead reckoning.
Every snap and calibration is written to the JSONL log (`route_snap`,
`turn_snap`, `step_length`, `leg_advance`, `step_jump`, `freeze`, `unfreeze`)
and shown in the viewer. Route snaps smaller than 1 cm are applied but not
logged.

**Safety.** If no sensor reading arrives for 1 s while navigating, `stop()` is
played once and guidance pauses until readings resume. The phone page is part
of this: it sends a reading only while the phone is still delivering motion
and orientation events (nothing older than 0.5 s), so a phone whose sensors
have stopped goes quiet and the Pi stops the wearer, instead of the page
repeating its last reading for ever. The Pi also refuses any reading the phone
marks as more than 1 s old. Steering taps are rate-limited twice: the follower
issues at most one pattern a second, and the driver drops any steering tap
that arrives within 0.4 s of the last or while a pattern is playing. Both arms
return to rest when a pattern fails, when the program exits and when the
service stops.

**One phone at a time.** The first phone to send a usable reading is the
sensor. Readings from any other phone or tab are refused (the page says so)
for as long as the first keeps sending; when it has been silent for 1 s the
next one takes over. Only the change in a phone's step count is used, so a
page reload or a change of phone never arrives as a jump in steps. A different
phone has a different compass zero: stop and calibrate again.

### Something in the way (off by default)

The route comes from a scan made when the building was empty. People, chairs
and bags are not on it, and nothing in this runtime can see them. If a sensor
looking ahead of the wearer is added, it reports here:

```
POST /obstacle {"left_m": 2.4, "ahead_m": 0.9, "right_m": null}
```

Three distances in metres: the nearest thing to the left, dead ahead and to
the right (`null`: nothing in range). Several times a second. The runtime
does not care what is looking: a phone's LiDAR through a native app, an
ultrasonic or time-of-flight sensor on the belt, a camera.

**There is a switch, and it is off.** `obstacle.enabled = false` in
`config.toml`; **Obstacle stop** in the viewer or
`POST /obstacle/mode {"enabled": true}` flips it while running. Off, readings
are shown in the viewer ("Ahead", marked "shown only") and never change a tap.

On:

| Reading | What the belt does |
| --- | --- |
| ahead closer than `stop_m` (1.5 m) | the stop pattern (both pods), once. The route's own steering taps are held back |
| still blocked | one tap a second towards the side with more room, if that side has at least `side_room_m` (1.2 m). Boxed in on both sides: nothing more, the wearer waits |
| ahead beyond `clear_m` (2.0 m) | guidance carries on and steers back to the route |
| no reading for `stale_s` (1 s) | the last one is dropped: a silent sensor must not leave the wearer stopped. The viewer says "sensor silent" |

Arrival and a stop at a change of floor are never held back. Readings and the
switch are inputs in the log, so a replay makes the same stops.

**No sensor exists yet.** This is the belt's half only, checked in tests and in
the viewer by posting readings by hand. What would feed it:

- **iPhone LiDAR.** Only a native app can read it (ARKit), and only on a Pro
  iPhone (12 Pro or later). The Safari page this runtime uses cannot, and one
  phone cannot run that page and a camera app in front at once. So either a
  second, front-mounted Pro phone runs a small depth app, or the team's app
  takes over steps and heading as well.
- **A distance sensor on the belt** (ultrasonic or time-of-flight) wired to
  the Pi gives `ahead_m` with no phone change. It sees one cone, not three
  directions, so it can stop the wearer but not say which way is open.

The numbers are guesses to be tuned on a real walk: at walking pace a person
covers about a metre between a reading and their own reaction to a tap, which
is why the stop is at 1.5 m and not closer.

## Assumptions and decisions

These were judgement calls where the brief left room. Each is one setting or a
few lines to change.

- **"Twice a second above 60 deg"** is played as one `double_tap` each second
  rather than single taps every half second: same tap count, and a rhythm that
  is easier to tell apart from the gentle correction.
- **"Enter at 20, exit at 10"** is read as: start correcting above 20, stop
  below 10.
- **Freeze holds position and the bias estimate, not heading.** With heading
  frozen too, a wearer who stops and turns on the spot keeps being told to
  turn, because the follower never sees that they did. In simulation a wearer
  pivoting at 45 deg/s or slower was still being told to turn when the 90 s
  run ended, and never arrived.
  `freeze_heading = true` gives the literal full-pose freeze.
- **Turn snaps may fire while frozen**, since turning on the spot at a corner
  is the main case they exist for. They need at least one step on the leg
  first, so looking around at the start cannot teleport the pose.
- **A turn snap needs more than the heading change in the brief.** The change
  must still be there 0.15 s later (`turn_hold_s`), so one bad compass reading
  near a corner cannot move the pose up to 3 m; and it must leave the wearer
  facing along the next leg, so swinging from one side of the leg to the other
  (an over-correction of about the right size) is not a turn. Steps taken
  during those 0.15 s are counted along the next leg. The 25 deg tolerance is
  also capped at a third of the turn: at a 45 deg corner a 25 deg tolerance
  would accept a 20 deg veer as the turn.
- **Bends gentler than 45 deg are not turns** (`min_turn_deg`). Without this a
  10 deg bend would "match" no heading change at all and snap the pose forward
  by up to 3 m.
- **The pose cannot get pinned to a finished leg.** Route snapping alone would
  hold the pose at a corner forever if a turn was missed, so: waypoints on a
  straight are walked through, and a turn too slow for the turn snap is
  accepted after two steps taken facing along the next leg, to the same
  tolerance as the turn snap (`leg_advance` in the log).
- **A step count that jumps is ignored, not walked.** More steps in one
  reading than anyone can take (2, plus 3 a second since the last reading, 8 at
  most) are dropped and logged as `step_jump`. Applied, they would move the
  pose metres along whatever the heading happened to be.
- **An unreliable compass is not sent.** When iOS reports a negative compass
  accuracy the phone page withholds the compass heading and says so; with the
  default `heading_source = "compass"` the Pi then sees silence and stops the
  wearer. `heading_source = "gyro"` is unaffected. Opening `/phone?trust=1`
  sends the compass regardless.
- **Compass bias is learned only within 20 deg of the leg** (`bias_max_deg`),
  with a step taken on the leg and not while standing. 20 deg is the follower's
  own threshold, so a heading the taps are correcting is never learned away.
- **Step-length calibration is off by default.** When switched on it needs at
  least 3 steps on the first leg and happens once, on a turn snap at waypoint 1.
- **Gyro heading option.** The phone also sends a magnet-immune yaw computed
  from the gyro-based orientation (`sensors.heading_source = "gyro"`). It
  drifts slowly but ignores the magnetic ring stand; the default remains
  `webkitCompassHeading`. Both are in every log record, so one logged walk
  shows which is steadier.
- **Step counts that go backwards** (the phone page reloaded) re-base without
  moving the pose.
- **`/route` and `/calibrate` are refused while guiding.** Both reset the
  follower, which mid-walk would end guidance with no signal. Stop first.
- **No authentication** on the HTTP endpoints: anyone on the same link can
  post a route or stop. Fine for a tethered demo, not for a shared network.
- **Commits** are authored with the local part of the account email as the
  name, because git had no identity configured. Change with `git config user.name`.

### Where the drift rules help and where they do not

The five rules assume the building keeps the wearer on the route line: they
turn where the corridor turns. Then a turn really does mark a waypoint and the
number of steps on a leg really does measure step length.

On open floor, where the taps are the only thing steering, a turn happens
where the system *thinks* the corner is, so it carries no new information.
Simulated tap-following wearers on `demo_L` finished this far from the true
end point (metres; simulation, not measurement):

| Settings | walks through the turn | stops and pivots | pivots, steps 10% long |
| --- | --- | --- | --- |
| default | 0.14 | 0.99 | 0.98 |
| with `calibrate_step_length = true` | 0.62 | 1.62 | 1.42 |
| plain dead reckoning | 0.80 | 0.99 | 0.98 |

These numbers move by half a metre with small changes in timing (whether one
more tap lands before a turn is recognised), so read them as "about a metre",
not as a ranking of the pivoting columns.

Two things stand out. On a 5 m first leg one step more or less changes the
calibrated step length by about 14 percent, more than the error it is meant to
fix, which is why that rule is off by default and a measured `step_length_m`
is the safer demo setting. And route snapping hides real sideways
drift when nothing physical is holding the wearer on the line. One more
consequence of turn detection: when the wearer turns a real corner before the
system expects it, they can get one tap the wrong way in the second before the
turn is recognised.

## Logs and replay

Each run writes `logs/walk-YYYYMMDD-HHMMSS.jsonl`: one JSON object per line.
Inputs are `meta` (the full config), `route`, `calibrate`, `start`, `stop`,
`sensor` (every message, including the phone's raw fields) and `stale`.
Outputs are `pose`, `tap` and the localizer events.

`nudge replay LOG` feeds the inputs back through a fresh pipeline and checks
that the same taps, poses and corrections come out. `nudge replay LOG --config
other.toml` replays the same walk with different settings, which is the quick
way to try a threshold or switch a rule off against a real walk.

## Hardware checks

**Done on the Pi, 14 Nov** (Raspberry Pi 4 Model B, Debian 13, Python 3.13,
over ssh on the iPhone hotspot):

- I2C had to be enabled first (`sudo raspi-config nonint do_i2c 0`); then
  `i2cdetect -y 1` shows `40` and `70`.
- `tap`, `double_tap`, `stop` and `arrived` each ran with the built-in driver
  and no I2C errors. Holding each arm with `--jog` and reading the chip back
  gave 1025 us on channel 0 at 50 deg, 1870 us on channel 1 at 130 deg, and
  1450 us on both at rest, at 50 Hz.
- `vcgencmd get_throttled` was `0x0` before and after. This was sixteen arm
  movements over about two minutes, not the five-minute soak below.
- Those first runs were on the bench at 40 degrees. On the finished belt, worn
  at the waist, 35 degrees was too faint and 45 is felt on both sides; that is
  what `config.toml` now uses (contact at 45 and 135 around a 90 degree rest).

**Still open.** The tools for each are in place.

1. **Is `webkitCompassHeading` stable with the phone landscape and upright,
   and with the magnetic ring stand attached?** Open `/phone`, mount the phone,
   stand still: read "Heading jitter (2 s)" and "Compass accuracy" on the page
   (the Pi console prints the same jitter). Then turn a slow full circle and
   check the heading climbs steadily through 360. Repeat with the ring stand
   on. If the compass is poor and "Gyro yaw" is steady, set
   `heading_source = "gyro"`. If the page says iOS reports the compass as
   unreliable, that is the answer for that mounting: the page is withholding
   the compass heading and the Pi will not guide on it.
2. **Does iPhone USB tethering give the phone a route to the Pi?** Follow the
   tethering section above; success is the "Link to Pi" row turning green and
   readings appearing on the Pi console.
3. **`vcgencmd get_throttled` after five minutes of tapping.** `nudge taps
   --soak 300` runs the taps and prints the decoded value before and after.
   Anything other than `0x0` means the 5 V rail sagged: give the servos their
   own supply.

Also worth a minute each: tune rest/contact angles (`--jog`); walk ten steps
and adjust the step threshold slider on the phone page until the count is
right; and stand still while the pods tap to see whether taps register as
steps (if they do, set `step_blank_after_tap_s` to about 0.5).

## To do

Not built or not done. Nothing here is started.

- **A new destination by voice, in the app.** "Take me to the kitchen" is planned by the server, but the
  traveller app does not pick up the session the reply names, so it sends no steps and the belt never
  starts. The change is in the team's `ios/Nudge/Traveller.swift`, in `command`: when the voice reply
  carries a `session` that is not the current one and no walk is under way, adopt it and go on to
  `calibrate()`, as `start(_:fromCaretaker:)` does. It needs a build in Xcode and a try on a phone, neither
  of which can be done from this laptop. Until then: a trip is sent once from Isaac Care, and "start my
  trip" repeats it.
- **"Stop" by voice reaches the belt.** Today it only stops the app; the belt stops a few seconds later
  because its sensor goes quiet. The app would call `POST /api/belt/stop`.
- **A guided walk on the belt**, with any sensor. Everything in this README about walking is from
  simulation and tests.
- **A sensor for the obstacle input.** The belt's half exists behind its switch; nothing feeds it.
- **A walk with a second wearer**, to see whether the height slider is enough.
- **Five-minute servo soak**: `python -m nudge.main taps --soak 300`.

## Layout

```
nudge/actuators.py     TapDriver, ServoDriver, MockDriver
nudge/sensors/         SensorSource; PhoneSource, SimSource, ImuSource (stub)
nudge/deadreckon.py    DeadReckoner, Localizer (drift correction)
nudge/follower.py      route-following state machine
nudge/navigator.py     sample -> pose -> taps, with logging; no clock, no I/O
nudge/runtime.py       live loop, stale-sensor watchdog, commands
nudge/server.py        HTTP and WebSocket endpoints
nudge/replay.py        JSONL replay
nudge/web/             phone.html, viewer.html
routes/                demo_L.json, demo_L.walk (sim script)
deploy/                systemd unit and installer
tests/                 pytest suite and a logged-walk fixture
bridge/                iMessage front end on Photon (laptop side): see bridge/README.md
```
