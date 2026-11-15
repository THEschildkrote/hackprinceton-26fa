# For the team repository

Two things here belong with `arkankau/hackprinceton-26fa`, the team's server.

## `venue_route.py`: a route in the venue, on the belt

Run it on the laptop where the team's server runs (the venue scan lives in that server's `cache/`, not in
git). It picks two points, plans the route with the server and gets it to the belt. Python only, no
packages. See "A route in the venue" in the main README, or the top of the file.

```bash
python venue_route.py --list
python venue_route.py --traveller Sam
python venue_route.py --begin SESSION
```

## `belt-handoff.patch`: merged, kept for reference

One commit that teaches the upstream server to hand a walk to the belt runtime in this repository and to
follow the belt's position. It was opened as a pull request from a fork and **merged into
`before-hardware` on 14 Nov**: https://github.com/arkankau/hackprinceton-26fa/pull/2 . Nothing needs
applying any more; a checkout of `before-hardware` at or after `c8fad9a` has it. The file is the commit as
it was written on top of `4ecfc9a`.

Run the upstream server with the Pi's address to switch it on:

```bash
NUDGE_BELT_URL=http://172.20.10.2:8000 python -m uvicorn nudge.server:app --port 8765
```

In PowerShell:

```powershell
$env:NUDGE_BELT_URL = "http://172.20.10.2:8000"; python -m uvicorn nudge.server:app --port 8765
```

What it added: `nudge/belt.py`, four small hooks in `nudge/server.py`, `tests/test_belt.py` (13 tests),
and `docs/belt.md`, which explains the calls and the limits. Without `NUDGE_BELT_URL` it does nothing.

What was checked:

- Before the merge, the upstream test suite with the patch on top of `57b0e2f`: 308 passed, 16 skipped,
  1 failed. The one failure (`test_voice_asks_for_the_building_then_uses_the_address`) needs
  `MATTERPORT_API_KEY`, which this laptop does not have, and fails the same way without the patch.
- After the merge, on `before-hardware` at `4f2bb63`: `nudge/belt.py` is identical to the patch, the belt
  and dashboard tests pass (24), and the server handed a planned route to a belt runtime with mock taps.
- The upstream server run against this runtime in simulator mode: both ended up agreeing on the wearer's
  position and heading.

What was not: a walk on the belt with a route from upstream.
