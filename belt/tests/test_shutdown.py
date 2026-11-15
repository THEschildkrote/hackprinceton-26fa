"""`systemctl stop` sends SIGTERM: the run must end cleanly with status 0."""

import asyncio
import signal

import pytest

from nudge import main


def long_run(tmp_path):
    script = tmp_path / "stand.walk"
    script.write_text("heading 0\nwait 60\n", encoding="utf-8")
    return main.parse_run_args(["--sim", "--script", str(script), "--no-server", "--no-log"])


def test_sigterm_ends_the_run_with_status_zero(monkeypatch, tmp_path):
    args = long_run(tmp_path)
    handlers = {}

    async def scenario():
        loop = asyncio.get_running_loop()
        # Capture the handler instead of relying on real signals (Windows has none for this).
        monkeypatch.setattr(type(loop), "add_signal_handler",
                            lambda self, sig, callback, *a: handlers.__setitem__(sig, callback))
        loop.call_later(0.3, lambda: handlers[signal.SIGTERM]())
        return await asyncio.wait_for(main._run(args), timeout=10)

    assert asyncio.run(scenario()) == 0
    assert signal.SIGTERM in handlers


def test_a_cancellation_that_is_not_sigterm_still_propagates(monkeypatch, tmp_path):
    args = long_run(tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(type(loop), "add_signal_handler", lambda self, sig, callback, *a: None)
        task = asyncio.ensure_future(main._run(args))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
