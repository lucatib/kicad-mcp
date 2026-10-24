"""How kicad-cli is spawned, checked without running it.

MCP clients (Codex, Claude Desktop) launch the server with no console. Windows
then gives every console-subsystem child -- kicad-cli.exe -- a brand-new
visible console window, which steals focus on every ERC, netlist or export
call. Only the spawn flag prevents that, so the flag is what is asserted.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from kicad_mcp import cli


def test_kicad_cli_is_spawned_without_a_console_window(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    install = SimpleNamespace(cli_path=Path(r"C:\KiCad\bin\kicad-cli.exe"))
    cli.run_cli(install, ["version"])

    assert seen.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
