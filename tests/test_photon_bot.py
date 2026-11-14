import shutil

import pytest

from nudge.agents import tools as T
from nudge.agents.exception import create_exception_agent
from nudge.photon_bot import Brain, find_building
from tests.test_exception import script

U = "+15555550100"


@pytest.fixture(autouse=True)
def clean():
    shutil.rmtree(T.dossier_dir("synthetic"), ignore_errors=True)
    T._graph_cached.cache_clear()
    yield
    shutil.rmtree(T.dossier_dir("synthetic"), ignore_errors=True)


def brain(started=None):
    def factory(nav, g, profile_text):
        return create_exception_agent(nav, g, profile_text, model=script())

    def runner(sid):
        if started is not None:
            started.append(sid)
    return Brain(factory, runner)


def test_find_building():
    assert find_building("synthetic")[0] == "synthetic"
    sid, name = find_building("1255 S Atlantic")
    assert sid and "Atlantic" in name


def test_help_and_profile():
    b = brain()
    assert "Isaac" in b.handle(U, "hi")[0]
    assert "wheelchair" in b.handle(U, "I use a wheelchair")[0]
    assert b.profiles[U] == "wheelchair"


def test_start_trip_kicks_off_dossier_and_gives_first_step():
    started = []
    b = brain(started)
    out = b.handle(U, "take me to the conference room at synthetic")
    text = " ".join(out)
    assert started == ["synthetic"], "first visit surveys the building"
    assert "Route ready" in text and "Lobby" in text
    assert b.handle(U, "next")[0]
    assert "meters to go" in b.handle(U, "where am i")[0]


def test_unknown_place_lists_rooms():
    out = brain().handle(U, "take me to the swimming pool at synthetic")
    assert "Kitchen" in out[0] and "Conference Room" in out[0]


def test_briefing_is_read_when_dossier_exists():
    (T.dossier_dir("synthetic") / "briefing.md").write_text("Two floors. Elevator by the office.", encoding="utf-8")
    started = []
    out = brain(started).handle(U, "take me to the kitchen at synthetic")
    assert out[0].startswith("Two floors") and started == []


def test_elevator_out_over_imessage_needs_yes():
    b = brain()
    b.handle(U, "blind")
    b.handle(U, "take me to the conference room at synthetic")
    s = b.sessions[U]
    out = b.handle(U, "the elevator is out of service")
    assert s.pending and any("Reply yes" in o for o in out)
    assert "p_E1" not in s.nav.blocked_nodes
    out = b.handle(U, "yes")
    assert "p_E1" in s.nav.blocked_nodes and "p_S1" in s.nav.route.nodes
    assert s.pending is None
