import asyncio

import pytest

from nudge.sensors.sim import ScriptError, SimSource, parse_script, script_events


def samples(text, dt=0.05):
    return [e[1] for e in script_events(text, dt) if e[0] == "sample"]


def test_parse_rejects_unknown_commands_and_bad_arguments():
    for bad in ["jump 3", "step", "step 1.5", "turn left", "cmd dance", "wait 1 2"]:
        with pytest.raises(ScriptError):
            parse_script(bad)


def test_comments_and_blank_lines_are_ignored():
    assert parse_script("# hello\n\n  step 2  # two steps\n") == [("step", [2])]


def test_steps_and_time_advance():
    out = samples("heading 30\nstep 3 0.5")
    assert len(out) == 30                       # 3 steps x 0.5 s at 20 Hz
    assert out[-1].t == pytest.approx(1.5)
    assert out[-1].step_count == 3
    assert [s.step_count for s in out[8:11]] == [0, 1, 1]
    assert {s.heading_deg for s in out} == {30.0}


def test_turn_is_gradual_and_wraps():
    out = samples("heading 350\nturn 90 1")
    assert len(out) == 20
    assert out[-1].heading_deg == pytest.approx(80.0)
    assert out[9].heading_deg == pytest.approx(35.0)


def test_offset_shifts_the_reported_heading_only():
    out = samples("heading 10\noffset 15\nwait 0.1")
    assert out[0].heading_deg == pytest.approx(25.0)


def test_arc_turns_while_stepping():
    out = samples("arc 90 2 0.5")
    assert out[-1].step_count == 2
    assert out[-1].heading_deg == pytest.approx(90.0)


def test_gap_and_commands_are_reported_in_order():
    events = list(script_events("wait 0.1\ncmd calibrate\ngap 2\ncmd stop\nquit\nwait 5"))
    kinds = [e[0] for e in events]
    assert kinds == ["sample", "sample", "cmd", "gap", "cmd", "quit"]
    assert events[3][1:] == pytest.approx((0.1, 2.1))


def test_a_command_first_gets_a_reading_to_act_on():
    events = list(script_events("cmd calibrate"))
    assert [e[0] for e in events] == ["sample", "cmd"]


def test_simsource_plays_a_script_then_stops_on_quit():
    async def run():
        source = SimSource(script="heading 90\nstep 2 0.1\ncmd start\nquit", speed=20)
        seen_cmds = []
        source.on_command = seen_cmds.append
        out = [s async for s in source]
        return out, seen_cmds

    out, cmds = asyncio.run(run())
    assert out[-1].step_count == 2 and out[-1].heading_deg == 90.0
    assert [b.t > a.t for a, b in zip(out, out[1:])] == [True] * (len(out) - 1)
    assert cmds == ["start"]


def test_simsource_keys_change_heading_and_steps():
    async def run():
        source = SimSource()
        stream = source.samples()
        first = await stream.__anext__()
        source.key("right")
        source.key("right")
        source.key("step")
        second = await stream.__anext__()
        source.key("left")
        third = await stream.__anext__()
        source.quit()
        rest = [s async for s in stream]
        return first, second, third, rest

    first, second, third, rest = asyncio.run(run())
    assert (first.heading_deg, first.step_count) == (0.0, 0)
    assert (second.heading_deg, second.step_count) == (30.0, 1)
    assert third.heading_deg == 15.0
    assert rest == []
