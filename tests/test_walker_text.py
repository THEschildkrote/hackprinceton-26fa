"""the walker texts isaac where they want to go, and the trip lands on their phone like a caretaker-sent one."""
import pytest
from fastapi.testclient import TestClient

from nudge import hub, server
from nudge.photon_bot import CaretakerBrain
from nudge.server import app

c = TestClient(app)
WALKER, CARE = "+15555550111", "+15555550199"


@pytest.fixture
def code(tmp_path, monkeypatch):
    monkeypatch.setenv("NUDGE_SITE", "synthetic")   # tests have no jrr room catalog (their dossier folder is empty)
    hub.reset(tmp_path / "hub.json")
    code = c.post("/api/traveller/register", json={"traveller_id": "W", "name": "Arkan"}).json()["code"]
    server._seen.clear()                            # no isaac phone online unless a test says so
    yield code
    server._seen.clear()
    hub.reset()


def test_asking_first_then_giving_the_code_sends_the_trip(code):
    b = CaretakerBrain()
    assert "6-digit code" in b.handle(WALKER, "I wanna go to the conference room")[0]
    assert hub.get_trip("W") is None
    out = b.handle(WALKER, f"I'm {code}")[0]
    assert "Conference Room" in out and "phone" in out
    trip = hub.get_trip("W")
    assert (trip["sid"], trip["to"], trip["by"]) == ("synthetic", "Conference Room", "walker")


def test_a_linked_walker_just_says_where(code):
    b = CaretakerBrain()
    assert "Got it, Arkan" in b.handle(WALKER, f"my code is {code}")[0]
    b.handle(WALKER, "Hey Isaac, take me to the kitchen at synthetic please")
    assert hub.get_trip("W")["sid"] == "synthetic"
    assert "Where to" in b.handle(WALKER, "I want to go here")[0]
    assert "moon" in b.handle(WALKER, "i want to go to the moon")[0]
    assert hub.get_trip("W")["to"] == "Kitchen"      # a bad ask leaves the last good one alone


def test_a_caretaker_can_text_a_trip_too(code):
    b = CaretakerBrain()
    b.handle(CARE, f"follow {code}")
    assert "Sent the Conference Room to Arkan" in b.handle(CARE, "take them to the conference room")[0]
    assert hub.get_trip("W")["by"] == "caretaker"


def test_with_one_isaac_phone_on_the_walker_just_talks(code):
    c.get("/api/traveller/W/trip")                  # the app on their back polls for trips
    b = CaretakerBrain()
    assert "starting a trip to the Conference Room" in b.handle(WALKER, "I want to go to the conference room.")[0]
    assert hub.get_trip("W")["by"] == "walker" and hub.walker(WALKER) == "W"


def test_a_call_asking_for_a_place_starts_it_on_their_phone(code):
    c.get("/api/traveller/W/trip")                  # isaac is open on the phone on their back
    r = c.post("/api/voice/call", json={"caller": "+15550001111", "text": "I wanna go to the conference room"}).json()
    assert "starting your trip to Conference Room" in r["say"]
    assert hub.get_trip("W")["to"] == "Conference Room" and hub.walker("+15550001111") == "W"


def test_a_caller_without_an_isaac_phone_still_gets_the_voice_guide(code):
    r = c.post("/api/voice/call", json={"caller": "+15550002222", "text": "where am I"}).json()
    assert r["say"] and hub.get_trip("W") is None


def test_thanks_bye_on_a_call_is_not_a_problem_to_reroute(code):
    assert "Bye" in c.post("/api/voice/call", json={"caller": "+15550003333", "text": "Ok, got it. Thanks, bye."}).json()["say"]


def test_how_callers_really_talk():
    from nudge.voice import TRIP, _norm, answer
    assert TRIP.match(_norm("Hey, uh, I wanna go to the seminar room."))["place"] == "the seminar room"
    assert answer(_norm("Uh, yeah sure, yes.")) is True and answer(_norm("nah, keep my route")) is False
    assert answer(_norm("sure, the elevator is out")) is None      # a sentence with "sure" in it isn't a yes


def test_two_calls_with_no_caller_number_dont_share_a_trip(code):
    a = c.post("/api/voice/call", json={"caller": None, "conversation": "conv_aaaaaaaaaaaa1", "text": "take me to the kitchen"}).json()
    b = c.post("/api/voice/call", json={"caller": None, "conversation": "conv_bbbbbbbbbbbb2", "text": "how far"}).json()
    assert a.get("session") and b["say"] == "You're not on a trip yet."


def test_a_call_with_no_caller_number_never_drives_someones_phone(code):
    c.get("/api/traveller/W/trip")                  # an isaac phone is on
    r = c.post("/api/voice/call", json={"caller": None, "conversation": "conv_web_test01", "text": "take me to the kitchen"}).json()
    assert hub.get_trip("W") is None and r.get("session")    # the voice guide on its own; the phone is left alone
