"""render_pcb_3d: a headless 3D image of the board, via kicad-cli pcb render.

Checking milled cutouts or 3D models meant asking the user to open the 3D
viewer. kicad-cli renders the same scene to a PNG; the tool returns the file
and, by default, the image itself so the model can look at it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_mcp import cli, server
from kicad_mcp.errors import ToolInputError
from mcp.server.mcpserver import Image
from pcb_fixture import pcb_file  # noqa: F401 - fixture


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    seen = {}

    def fake_run(install, args, timeout=cli.DEFAULT_TIMEOUT):
        seen["args"] = args
        out = Path(args[args.index("-o") + 1])
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        return {"command": " ".join(args), "returncode": 0,
                "stdout": "Loading\n" + "Build Tech layer\n" * 80 + "Successfully created 3D render image",
                "stderr": ""}

    monkeypatch.setattr(cli, "run_cli", fake_run)
    board = tmp_path / "b.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    return seen, board


INSTALL = SimpleNamespace(cli_path=Path("kicad-cli.exe"))


def test_render_passes_the_view_through(fake_cli, tmp_path):
    seen, board = fake_cli
    out = cli.render_pcb(INSTALL, board, output=tmp_path / "iso.png", side="bottom",
                         width=800, height=600, quality="high", rotate="-45,0,45",
                         zoom=1.5, perspective=True)

    args = seen["args"]
    assert args[:2] == ["pcb", "render"]
    assert args[args.index("--side") + 1] == "bottom"
    assert args[args.index("--width") + 1] == "800"
    assert args[args.index("--height") + 1] == "600"
    assert args[args.index("--quality") + 1] == "high"
    assert args[args.index("--rotate") + 1] == "-45,0,45"
    assert args[args.index("--zoom") + 1] == "1.5"
    assert "--perspective" in args
    assert args[-1] == str(board)
    assert out["output_file"] == str(tmp_path / "iso.png")
    assert len(out["stdout"]) < 400  # progress chatter trimmed


def test_render_defaults_outside_the_project(fake_cli):
    seen, board = fake_cli
    out = cli.render_pcb(INSTALL, board)

    assert Path(out["output_file"]).parent != board.parent
    assert Path(out["output_file"]).name == "b-top.png"


@pytest.mark.parametrize("kwargs", [
    {"side": "diagonal"}, {"quality": "ultra"}, {"output": "x.bmp"}, {"width": 0},
])
def test_render_rejects_bad_options(fake_cli, kwargs):
    _, board = fake_cli

    with pytest.raises(ToolInputError):
        cli.render_pcb(INSTALL, board, **kwargs)


@pytest.mark.requires_kicad
def test_render_tool_returns_the_image(pcb_file, tmp_path):
    result = server.render_pcb_3d(str(pcb_file), output=str(tmp_path / "top.png"),
                                  width=400, height=300)

    info, image = result
    assert isinstance(image, Image)
    assert info["returncode"] == 0
    assert Path(info["output_file"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.requires_kicad
def test_render_tool_without_inline_returns_only_the_path(pcb_file, tmp_path):
    info = server.render_pcb_3d(str(pcb_file), output=str(tmp_path / "top.png"),
                                width=400, height=300, inline=False)

    assert isinstance(info, dict)
    assert Path(info["output_file"]).is_file()


def test_render_tool_error_is_data(tmp_path):
    out = server.render_pcb_3d(str(tmp_path / "nope.kicad_pcb"))

    assert out["error"] == "ToolInputError"
