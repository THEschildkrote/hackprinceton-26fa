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
