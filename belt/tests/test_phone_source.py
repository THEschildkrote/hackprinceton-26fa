from nudge.sensors import phone
from nudge.sensors.phone import PhoneSource


def source(**kwargs):
    src = PhoneSource(**kwargs)
    clock = [0.0]
    src.now = lambda: clock[0]
    return src, clock


def reading(heading, steps, **extra):
    return {"type": "sensor", "compass_deg": heading, "heading_deg": heading, "step_count": steps, **extra}


def drain(src):
    out = []
    while not src._queue.empty():
        out.append(src._queue.get_nowait())
    return out


def test_a_second_phone_is_refused_while_the_first_is_live():
    src, clock = source(takeover_s=1.0)
    assert src.feed(reading(10.0, 5, sid="a"), "ws", "ws-1")
    clock[0] = 0.5
    assert not src.feed(reading(200.0, 80, sid="b"), "ws", "ws-2")
    assert src.last_status == phone.BUSY and src.client == "a"
    clock[0] = 0.6
    assert src.feed(reading(11.0, 6, sid="a"), "ws", "ws-1")
    assert [(s.heading_deg, s.step_count) for s in drain(src)] == [(10.0, 5), (11.0, 6)]
    assert src.received == 2 and src.rejected == 1


def test_a_silent_phone_gives_up_its_place_and_steps_carry_on():
    src, clock = source(takeover_s=1.0)
    src.feed(reading(10.0, 5, sid="a"))
    src.feed(reading(10.0, 6, sid="a"))
    clock[0] = 1.5                                  # "a" has said nothing for longer than takeover_s
    assert src.feed(reading(200.0, 80, sid="b"))    # a reloaded page, or another phone
    assert src.feed(reading(200.0, 82, sid="b"))
    # The newcomer's own count starts somewhere else entirely; only its changes are passed on.
    assert [s.step_count for s in drain(src)] == [5, 6, 6, 8]
    clock[0] = 1.6
    assert not src.feed(reading(10.0, 7, sid="a")) and src.last_status == phone.BUSY


def test_one_page_keeps_its_place_when_it_changes_transport():
    src, _ = source()
    assert src.feed(reading(10.0, 0, sid="a"), "ws", "ws-1")
    assert src.feed(reading(10.0, 1, sid="a"), "post", "post-10.0.0.2")
    assert src.transport == "post" and src.client == "a"
    # Without a sid the connection is all there is to go on.
    plain, _ = source()
    assert plain.feed(reading(10.0, 0), "ws", "ws-1")
    assert not plain.feed(reading(10.0, 0), "ws", "ws-2")


def test_a_count_that_goes_backwards_is_rebased():
    src, _ = source()
    for steps in (5, 6, 2, 3):
        src.feed(reading(0.0, steps, sid="a"))
    assert [s.step_count for s in drain(src)] == [5, 6, 6, 7]


def test_readings_the_phone_calls_old_are_refused():
    src, _ = source(max_age_s=1.0)
    assert src.feed(reading(10.0, 0, age_ms=120))
    assert not src.feed(reading(10.0, 0, age_ms=1500))
    assert src.last_status == phone.OLD
    assert not src.feed({"step_count": 1})
    assert src.last_status == phone.INVALID
    assert len(drain(src)) == 1


def test_a_refused_newcomer_does_not_take_the_place():
    src, clock = source(takeover_s=1.0)
    src.feed(reading(10.0, 0, sid="a"))
    clock[0] = 2.0
    assert not src.feed(reading(float("nan"), 0, sid="b"))     # unusable: "a" is still the sensor
    assert src.client == "a"
