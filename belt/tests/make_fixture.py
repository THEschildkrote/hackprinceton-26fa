"""Regenerate tests/data/demo_L_walk.jsonl.

The fixture is a logged walk of routes/demo_L.json with real steps of
0.75 m, a compass that reads 12 degrees high after calibration, and a
two-second sensor dropout on the second leg. test_replay.py replays it and
expects the same taps, poses and corrections to come out again, so only
regenerate it when a change in behaviour is intended:

    python tests/make_fixture.py
"""

import json
from pathlib import Path

from nudge.config import Config
from nudge.eventlog import EventLog
from nudge.navigator import Navigator
from nudge.sensors.sim import drive, script_events

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "data" / "demo_L_walk.jsonl"

SCRIPT = """
heading 217            # whatever the compass reads facing along the first leg
wait 1
cmd calibrate
cmd start
wait 0.5
offset 12              # the compass error appears once the wearer leaves the start
step 4
turn 28 0.6            # drifts right: taps left until it is corrected
step 1
turn -28 0.6
step 2                 # 7 steps of 0.75 m = 5.25 m: just past the corner
turn 90 1.4            # the right turn
wait 0.4
step 3
gap 2                  # phone drops out for two seconds
step 4                 # 7 more steps on the second leg
wait 2.5
"""


def build() -> list[dict]:
    log = EventLog(keep=True)
    # Step-length calibration is off by default; the fixture turns it on so
    # that replay covers every correction.
    nav = Navigator(Config.from_dict({"deadreckon": {"calibrate_step_length": True}}), log)
    nav.set_route(json.loads((ROOT / "routes" / "demo_L.json").read_text(encoding="utf-8")), name="demo_L")
    drive(nav, script_events(SCRIPT))
    return log.records


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    records = build()
    with FIXTURE.open("w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    kinds = {}
    for record in records:
        kinds[record["type"]] = kinds.get(record["type"], 0) + 1
    print(f"wrote {FIXTURE} ({len(records)} records)")
    print(", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    print("taps:", [(r["t"], r["kind"], r["side"]) for r in records if r["type"] == "tap"])
