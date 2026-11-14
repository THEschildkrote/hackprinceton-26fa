"""tests get their own dossier folder. many fixtures wipe the 'synthetic' dossier; on the shared cache/dossiers
that deletes the output of a real agent run happening at the same time (files vanished mid-run)."""
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="nudge-test-dossiers-"))


def pytest_configure(config):
    from nudge import building
    from nudge.agents import tools
    tools.DOSSIERS = _TMP
    building.DOSSIERS = _TMP


def pytest_unconfigure(config):
    shutil.rmtree(_TMP, ignore_errors=True)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_band_left_attached():
    """the pi collar is one global; a test that attaches it must not leak into the next."""
    from nudge import bandserve
    bandserve.detach()
    yield
    bandserve.detach()
