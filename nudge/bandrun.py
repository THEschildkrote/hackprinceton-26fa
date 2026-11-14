"""the loop between a navigator and the servo band.

band.py knows how to tap a shoulder but nothing about where anyone is. navigator.py knows
where they are but nothing about servos. this is what sits between them.

the two run at very different rates, and that is the whole problem it solves. positions
arrive at walking pace — sim.walk emits one every 0.5 m, so roughly every 400 ms, and real
localization will be no faster. a stroke, meanwhile, only renders if the servo is commanded
every few milliseconds. so a position update is not a tick: `update()` takes a new position
whenever one shows up, and `tick()` has to keep being called in between or the horn never
moves.

the band is driven off the live packet (nudge/device.py), not off navigator events, which
is what the esp32 firmware did too: the engine works out the approach tap from act, dir and
metres-to-next on its own. events come back from update() so the caller can speak them —
the pi has audio out, which the microcontroller did not.

    python -m nudge.bandrun synthetic --to "Conference Room" --dry
    python -m nudge.bandrun synthetic --to "Conference Room"      # on the pi, real servos
"""
from .band import Engine, NullBand, ServoBand, clock
from .device import live_packet

PACE_MS = 420  # a 0.5 m sim step at about 1.2 m/s, roughly a walking pace


class BandRunner:
    """holds a navigator and a band together. owns no clock and starts no thread.

    call update() when a position arrives and tick() as often as you can manage. both take
    `now` in milliseconds from the same source, so tests can run on a virtual clock.
    """

    def __init__(self, nav, band=None, engine=None):
        self.nav = nav
        self.band = NullBand() if band is None else band
        self.engine = engine or Engine()
        self.heading = None
        self._applied = (None, None)

    # --- positions in -------------------------------------------------------

    def start(self, x, y, floor, heading=None, sid=None, now=0):
        events = self.nav.start(x, y, floor, heading, sid)
        self.sync(heading, now)
        return events

    def update(self, x, y, floor, heading=None, sid=None, now=0):
        events = self.nav.update(x, y, floor, heading, sid)
        self.sync(heading, now)
        return events

    def sync(self, heading=None, now=0):
        """push the navigator's current state into the engine without driving it.

        for when something else owns the navigator: the server has already called
        nav.update() on an http request, and the band only needs to hear what changed.
        """
        self.heading = heading
        self.engine.on_live(live_packet(self.nav, heading), now)

    # --- servos out ---------------------------------------------------------

    def tick(self, now):
        """render whatever the engine wants right now. cheap enough to call in a tight loop."""
        state = self.engine.tick(now)
        if state != self._applied:
            self.band.apply(*state)
            self._applied = state
        return state

    @property
    def busy(self):
        return self.engine.busy

    def close(self):
        self.band.apply(None, None)
        self._applied = (None, None)
        self.band.close()


def run_walk(runner, positions, start=None, pace_ms=PACE_MS, step_ms=5, virtual=False,
             on_event=None):
    """walk a list of positions end to end, ticking the band between each one.

    blocking, and paced in real time unless `virtual`. returns the events it saw, in the
    shape sim.run uses: (position, event).
    """
    now, wait = clock(virtual)
    positions = list(positions)
    if not positions:
        return []
    seen = []

    def record(p, events):
        for e in events or ():
            seen.append((p, e))
            if on_event:
                on_event(p, e)

    head = start if start is not None else positions[0]
    record(head, runner.start(*head, now=now()))

    for p in positions:
        record(p, runner.update(*p, now=now()))
        end = now() + pace_ms
        while now() < end:
            runner.tick(now())
            wait(step_ms)
        if runner.nav.done:
            break

    # let the last pattern play out, or the arrive cue gets cut off mid-stroke
    while runner.busy:
        runner.tick(now())
        wait(step_ms)
    runner.close()
    return seen


# --- cli ---------------------------------------------------------------------


def main(argv=None):
    import argparse

    from .cli import farthest, pick_start
    from .fetch import load
    from .navigator import Navigator
    from .router import PROFILES, plan
    from .sim import walk
    from .walkgraph import build

    ap = argparse.ArgumentParser(prog="nudge.bandrun",
                                 description="walk a route and drive the servo band")
    ap.add_argument("sid")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--profile", default="blind", choices=sorted(PROFILES))
    ap.add_argument("--wobble", type=float, default=0.0)
    ap.add_argument("--pace", type=int, default=PACE_MS, help="ms per 0.5 m step")
    ap.add_argument("--dry", action="store_true",
                    help="no gpio and no waiting: prints what the servos would do")
    a = ap.parse_args(argv)

    g = build(load(a.sid))
    start = pick_start(g, a.frm)
    goal = a.to or farthest(g, start)
    route = plan(g, start, goal, a.profile)
    s = g.nodes[start]

    nav = Navigator(g, goal, a.profile)
    band = NullBand() if a.dry else ServoBand()
    runner = BandRunner(nav, band=band)

    print(f"walking {a.profile} from {s.label} to {goal!r}")

    def say(p, e):
        cue = f"[{e['cue']}]" if e.get("cue") else ""
        print(f"  ({p[0]:6.2f},{p[1]:6.2f}) {e['type']:8} {cue:14} {e['text']}")

    run_walk(runner, walk(g, route, step=0.5, wobble=a.wobble),
             start=(s.x, s.y, s.floor, 0.0), pace_ms=a.pace, virtual=a.dry, on_event=say)

    if a.dry:
        taps = sum(1 for _, left, right in band.commands if left or right)
        print(f"\n{taps} strokes, {len(band.commands)} servo state changes, no gpio touched")
    print("arrived" if nav.done else "ran out of positions before arriving")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
