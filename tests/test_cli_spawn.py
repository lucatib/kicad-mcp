"""How kicad-cli is spawned, checked without running it.

MCP clients (Codex, Claude Desktop) launch the server with no console. Windows
then gives every console-subsystem child -- kicad-cli.exe -- a brand-new
visible console window, which steals focus on every ERC, netlist or export
call. Only the spawn flag prevents that, so the flag is what is asserted.
Linux and macOS have no such window, and no such flag.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from kicad_mcp import cli


def _spawn_kwargs(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    install = SimpleNamespace(cli_path=Path("kicad-cli"))
    cli.run_cli(install, ["version"])
    return seen


def test_kicad_cli_is_spawned_without_a_console_window(monkeypatch):
    seen = _spawn_kwargs(monkeypatch)

    if sys.platform == "win32":
        assert seen.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
    else:
        assert not seen.get("creationflags")


def test_kicad_cli_never_inherits_the_servers_stdin(monkeypatch):
    # stdin is the MCP client's pipe; a child reading it would eat protocol.
    assert _spawn_kwargs(monkeypatch)["stdin"] is subprocess.DEVNULL
