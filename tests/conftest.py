"""Test-session setup shared by every test module."""

from __future__ import annotations

import os

import pytest

try:
    import xdist  # noqa: F401 - only to know whether the hook below exists
except ImportError:  # pragma: no cover - xdist is a dev dependency
    pass
else:
    @pytest.hookimpl(tryfirst=True)
    def pytest_xdist_auto_num_workers(config):
        """Workers for `-n auto`: one per core, at most 8.

        The suite waits on kicad-cli, and past ~8 workers it gets no faster.
        Answering here also skips xdist's own CPU detection, after which its
        workers die at startup on some Windows setups (an explicit `-n 8`
        works where `-n auto` fails).
        """
        return max(1, min(os.cpu_count() or 1, 8))

# pytest-xdist starts its workers as subprocesses, and on Windows that fails
# with "WinError 6: invalid handle" when this process has no usable stdin, as
# under some CI runners and agent shells. Give it the null device instead;
# nothing in the suite reads stdin.
def _usable_stdin() -> bool:
    try:
        os.fstat(0)
    except OSError:
        return False
    if os.name == "nt":
        import ctypes  # noqa: PLC0415
        handle = ctypes.windll.kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        return handle not in (0, -1, None) and ctypes.windll.kernel32.GetFileType(handle) != 0
    return True


if not _usable_stdin():
    _null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(_null, 0)
    os.close(_null)
    if os.name == "nt":
        import ctypes
        import msvcrt

        # The CRT's fd 0 is not what subprocess passes on; the process's
        # standard handle is.
        ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))
