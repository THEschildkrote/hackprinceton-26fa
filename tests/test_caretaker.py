"""caretaker over imessage: the hub, server publishing, and the few calm messages a walk produces."""
import pytest
from fastapi.testclient import TestClient

from nudge import hub
from nudge.photon_bot import CaretakerBrain, Notifier
from nudge.server import app, get_navigator
from nudge.sim import walk

c = TestClient(app)
CARE = "+15555550199"


@pytest.fixture
def clock(tmp_path, monkeypatch):
    hub.reset(tmp_path / "hub.json")
    t = [1_000_000.0]
    monkeypatch.setattr(hub, "now", lambda: t[0])
    yield t
    hub.reset()


@pytest.fixture
def inbox(clock):
    sent = []
    n = Notifier(lambda handle, space, agent, text, key: sent.append(text), sync=True)
    yield sent
    n.unsub()


def follow(tid="T", name="Sam"):
    r = c.post("/api/traveller/register", json={"name": name, "traveller_id": tid}).json()
    brain = CaretakerBrain()
    out = brain.handle(CARE, f"follow {r['code']}")
    assert "now following Sam" in out[0]
    hub.set_space(CARE, "space-1", "agent-1")
    return brain


def start(tid="T", to="Conference Room", profile="wheelchair", at=(2.0, 11.0, "f1")):
    s = c.post("/api/nav/start", json={"sid": "synthetic", "to": to, "profile": profile, "x": at[0], "y": at[1],
                                       "floor": at[2], "traveller": tid}).json()
    assert s["traveller"] == tid
    return s["session"]


def update(session, x, y, floor, clock, dt=2.0):
    clock[0] += dt
    return c.post("/api/nav/update", json={"session": session, "x": x, "y": y, "floor": floor}).json()


def test_pairing_rejects_bad_codes(clock):
    c.post("/api/traveller/register", json={"name": "Sam", "traveller_id": "T"})
    code = hub.register_traveller("T")
    bad = "000000" if code != "000000" else "111111"
    assert "didn't match" in CaretakerBrain().handle(CARE, f"follow {bad}")[0]
    assert "not following" in CaretakerBrain().handle("+15550000000", "where are they?")[0]


def test_a_walk_sends_start_floor_change_and_arrival_only(inbox, clock):
    follow()
    session = start()
    nav = get_navigator(session)
    for x, y, f, h in walk(nav.g, nav.route, step=0.4):
        update(session, x, y, f, clock)
        if nav.done:
            break
    assert nav.done
    kinds = [("started" if "started walking" in m else "floor" if "now on Floor 2" in m else
              "arrived" if "arrived at the conference room" in m.lower() else m) for m in inbox]
    assert kinds == ["started", "floor", "arrived"], inbox
    assert "meters" in inbox[0] and "Sam started walking to the Conference Room" in inbox[0]


def test_unmapped_alerts_once(inbox, clock):
    follow()
    session = start(to="Kitchen", profile="blind", at=(2.0, 1.5, "f1"))
    for _ in range(4):
        update(session, 60.0, 60.0, "f1", clock)
    alerts = [m for m in inbox if "outside the mapped area" in m]
    assert len(alerts) == 1, inbox


def test_help_is_immediate_with_place(inbox, clock):
    follow()
    session = start(to="Kitchen", profile="blind", at=(2.0, 1.5, "f1"))
    update(session, 2.2, 1.6, "f1", clock, dt=1.0)
    r = c.post("/api/traveller/T/help", json={"text": "I can't find the door"}).json()
    assert r["state"] == "help"
    help_msgs = [m for m in inbox if "asked for help" in m]
    assert help_msgs and "lobby" in help_msgs[0].lower() and "can't find the door" in help_msgs[0]


def test_status_question_in_meters(inbox, clock):
    brain = follow()
    session = start()
    nav = get_navigator(session)
    pts = list(walk(nav.g, nav.route, step=0.4))[:8]
    for x, y, f, h in pts:
        update(session, x, y, f, clock)
    out = brain.handle(CARE, "where are they?")[0]
    assert out.startswith("Sam is walking to the Conference Room") and "meters" in out and "feet" not in out
    assert "Yes." in brain.handle(CARE, "is everything ok?")[0]
    assert "min" in brain.handle(CARE, "eta")[0]


def test_stuck_after_three_minutes(inbox, clock):
    follow()
    session = start(to="Kitchen", profile="blind", at=(2.0, 1.5, "f1"))
    for _ in range(12):  # standing still, updates still coming in
        update(session, 2.0, 1.5, "f1", clock, dt=20.0)
    stuck = [m for m in inbox if "hasn't moved" in m]
    assert len(stuck) == 1 and hub.status("T")["state"] in ("help", "walking")


def test_notify_all_adds_room_updates(inbox, clock):
    brain = follow()
    assert "room-by-room" in brain.handle(CARE, "notify all")[0]
    session = start()
    nav = get_navigator(session)
    for x, y, f, h in walk(nav.g, nav.route, step=0.4):
        update(session, x, y, f, clock, dt=25.0)
        if nav.done:
            break
    assert any("Entering" in m for m in inbox), inbox


def test_travel_mode_keeps_old_commands(clock):
    brain = CaretakerBrain(travel_brain=None)
    assert "Travel mode on" in brain.handle(CARE, "travel mode")[0]
    assert "Caretaker mode on" in brain.handle(CARE, "caretaker mode")[0]
