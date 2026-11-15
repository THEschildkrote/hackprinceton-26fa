import asyncio
import os
import select
import sys
import time
import types

from nudge.sensors.sim import Keyboard, _unfinished_escape, decode_keys


def test_windows_console_keys_are_decoded(monkeypatch):
    # A stand-in for msvcrt: arrows arrive as a prefix byte plus a letter.
    typed = ["\xe0", "K", " ", "c", "\x00", "M", "\xe0", "H", "S", "\xe0", "Z", "\x03"]
    fake = types.ModuleType("msvcrt")
    fake.kbhit = lambda: bool(typed)
    fake.getwch = lambda: typed.pop(0)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)

    async def run():
        seen = []
        keyboard = Keyboard(asyncio.get_running_loop(), seen.append)
        task = asyncio.get_running_loop().run_in_executor(None, keyboard._run_windows)
        for _ in range(200):
            if not typed:
                break
            await asyncio.sleep(0.01)
        keyboard.stop()
        await task
        await asyncio.sleep(0)   # let the last call_soon_threadsafe land
        return seen

    assert asyncio.run(run()) == ["left", "space", "c", "right", "up", "s", "q"]


def test_terminal_escape_sequences_are_decoded():
    assert decode_keys(b"\x1b[D") == ["left"]
    assert decode_keys(b"\x1b[C\x1b[A c") == ["right", "up", "space", "c"]
    assert decode_keys(b"\x1bOD\x1bOB") == ["left", "down"]     # application cursor mode
    assert decode_keys(b"\x1b[1;5C") == ["right"]               # with a modifier held
    assert decode_keys(b"\x1b[3~S\x1b") == ["s"]                # Delete and a bare Escape are not keys
    assert decode_keys(b"") == []
    assert _unfinished_escape(b"\x1b") and _unfinished_escape(b"x\x1b[") and _unfinished_escape(b"\x1b[1;")
    assert not _unfinished_escape(b"\x1b[D") and not _unfinished_escape(b"abc")


def test_posix_terminal_keys_arrive_even_when_an_arrow_is_split(monkeypatch):
    # Stand-ins for the terminal: the reader must use the descriptor (os.read),
    # and cope with an arrow key's three bytes arriving in two pieces.
    FD = 987654
    chunks = [b"\x1b", b"[D", b" ", b"\x1b[C\x1b[A", b"x"]
    real_read, real_select = os.read, select.select
    modes = []

    def fake_read(fd, n):
        return chunks.pop(0) if fd == FD else real_read(fd, n)

    def fake_select(r, w, x, timeout=None):
        if r != [FD]:
            return real_select(r, w, x, timeout)
        if chunks:
            return ([FD], [], [])
        time.sleep(min(timeout or 0.01, 0.01))
        return ([], [], [])

    class Terminal:
        def fileno(self):
            return FD

        def isatty(self):
            return True

        def read(self, n=-1):
            raise AssertionError("buffered stdin must not be read: select() cannot see into it")

    termios = types.ModuleType("termios")
    termios.TCSADRAIN = 1
    termios.tcgetattr = lambda fd: "saved"
    termios.tcsetattr = lambda fd, when, attrs: modes.append(attrs)
    tty = types.ModuleType("tty")
    tty.setcbreak = lambda fd: modes.append("cbreak")
    monkeypatch.setitem(sys.modules, "termios", termios)
    monkeypatch.setitem(sys.modules, "tty", tty)
    monkeypatch.setattr(os, "read", fake_read)
    monkeypatch.setattr(select, "select", fake_select)
    monkeypatch.setattr(sys, "stdin", Terminal())

    async def run():
        seen = []
        keyboard = Keyboard(asyncio.get_running_loop(), seen.append)
        task = asyncio.get_running_loop().run_in_executor(None, keyboard._run_posix)
        for _ in range(200):
            if not chunks:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        keyboard.stop()
        await task
        await asyncio.sleep(0)
        return seen

    assert asyncio.run(run()) == ["left", "space", "right", "up", "x"]
    assert modes[0] == "cbreak" and modes[-1] == "saved"      # the terminal is put back as it was


def test_keyboard_is_skipped_without_a_terminal(monkeypatch):
    class NotATerminal:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", NotATerminal())

    async def run():
        return Keyboard(asyncio.get_running_loop(), lambda key: None).start()

    assert asyncio.run(run()) is False
