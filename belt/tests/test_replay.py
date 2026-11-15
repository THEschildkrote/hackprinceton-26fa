import json
from pathlib import Path

from nudge.config import Config
from nudge.eventlog import EventLog, read_log
from nudge.main import main
from nudge.navigator import Navigator
from nudge.replay import replay, replay_file
from nudge.sensors.sim import drive, script_events

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "data" / "demo_L_walk.jsonl"


def test_logged_walk_replays_to_the_same_taps_poses_and_corrections():
    result = replay_file(FIXTURE)
    assert result.ok, result.summary()
    assert result.samples == 294
    assert result.final_state == "ARRIVED"
    kinds = [(kind, side) for _, kind, side in result.replayed_taps]
    # One tap for the drift and one double tap for the corner: the walker is already turning the
    # right way when the next one would be due, so it is not sent (it was, before the overturn rules).
    assert kinds == [("tap", "left"), ("double_tap", "right"), ("stop", None), ("arrived", None)]
    assert result.replayed_events["turn_snap"] == 1
    assert result.replayed_events["step_length"] == 1
    assert result.replayed_events["route_snap"] == 14
    assert result.poses_compared == 274 and result.max_pose_error_m < 1e-9
    x, y, _ = result.final_pose
    assert abs(x - 5.0) < 1.0 and abs(y - 5.0) < 0.01


def test_fixture_is_what_make_fixture_produces():
    # If this fails after an intended behaviour change, run tests/make_fixture.py.
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_fixture", ROOT / "tests" / "make_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert json.loads(json.dumps(module.build())) == read_log(FIXTURE)


def test_a_walk_logged_to_a_file_replays_exactly(tmp_path):
    path = tmp_path / "walk.jsonl"
    log = EventLog(path)
    nav = Navigator(Config.from_dict({"follower": {"arrive_radius_m": 0.8}}), log)
    nav.set_route({"waypoints": [[0, 0], [0, 6], [-4, 6], [-4, 12]]})
    script = ("heading 300\nwait 0.5\ncmd calibrate\ncmd start\nstep 9\nturn -90 1\nstep 6\n"
              "turn 90 1.2\nstep 3\ncmd stop\nwait 1\ncmd start\nstep 6\nwait 2")
    live = drive(nav, script_events(script))
    log.close()

    result = replay_file(path)
    assert result.ok, result.summary()
    assert [(k, s) for _, k, s in result.replayed_taps] == [(a.kind, a.side) for a in live]
    assert result.final_state == nav.follower.state.value == "ARRIVED"
    assert result.replayed_events["turn_snap"] == 2


def test_replay_notices_a_tampered_log():
    records = read_log(FIXTURE)
    index = next(i for i, r in enumerate(records) if r["type"] == "sensor" and r["step_count"] == 3)
    records[index] = {**records[index], "heading_deg": (records[index]["heading_deg"] + 90) % 360}
    result = replay(records)
    assert not result.ok
    assert result.pose_mismatches > 0


def test_what_if_replay_with_other_settings():
    records = read_log(FIXTURE)
    plain = replay(records, Config.from_dict({"deadreckon": {"drift_correction": False}}))
    assert not plain.used_logged_config
    assert plain.replayed_events["turn_snap"] == 0
    assert plain.max_pose_error_m > 0.3          # the corrections were doing something
    assert not plain.errors


def test_replay_command(capsys):
    assert main(["replay", str(FIXTURE), "--taps"]) == 0
    out = capsys.readouterr().out
    assert "REPLAY OK" in out and "double_tap right" in out
