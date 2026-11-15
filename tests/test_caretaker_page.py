"""the caretaker page's text box answers like the imessage line: it follows the traveller, then answers questions."""
import pytest
from fastapi.testclient import TestClient

from nudge import hub
from nudge.server import app
from tests import belt_maps

c = TestClient(app)


@pytest.fixture(autouse=True)
def _test_maps(monkeypatch):
    """the drawn straight line from tests/belt_maps.py loads like any building, for these tests only."""
    from nudge import fetch
    real = fetch.load
    monkeypatch.setattr(fetch, "load",
                        lambda sid, refresh=False: belt_maps.model(sid) if sid in belt_maps.NAMES else real(sid, refresh))


@pytest.fixture
def maya(tmp_path):
    hub.reset(tmp_path / "hub.json")
    c.post("/api/traveller/register", json={"traveller_id": "M", "name": "Maya"})
    yield "M"
    hub.reset()


def ask(text):
    return c.post("/api/caretaker/text", json={"handle": "page-M", "text": text, "traveller": "M"}).json()["replies"]


def test_the_page_follows_the_traveller_and_answers(maya):
    assert "Maya" in " ".join(ask("where is she?"))
    assert "not following" not in " ".join(ask("is she okay?"))


def test_a_question_mid_walk_gets_the_distance_left(maya):
    s = c.post("/api/nav/start", json={"sid": belt_maps.LINE, "to": "Finish", "profile": "blind", "traveller": "M"}).json()
    c.post("/api/nav/calibrate", json={"session": s["session"], "heading_deg": 0})
    c.post("/api/nav/step", json={"session": s["session"], "steps": 3, "heading_deg": 0, "step_m": 0.7})
    assert "left" in " ".join(ask("how far?"))
