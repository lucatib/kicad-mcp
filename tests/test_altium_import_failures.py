"""import_altium either finishes or leaves nothing behind.

The project is built in a staging folder beside the output and moved into
place only once complete, so a failure halfway never leaves a half-written
folder that the next run then refuses. ERC only informs: when it cannot run,
the import still completes and says so.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from altium_fixtures import altium_set
from kicad_mcp.altium import importer, sheets
from kicad_mcp.errors import KicadMcpError

#: No kicad-cli behind it: ERC fails the way it does with KiCad missing or broken.
NO_KICAD = SimpleNamespace(cli_path=Path("Z:/nowhere/kicad-cli.exe"))


def _sources(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for p in altium_set("eDP"):
        if p.suffix == ".SchDoc":
            shutil.copy(p, src / p.name)
    return src


def test_erc_failure_is_reported_not_fatal(tmp_path):
    src = _sources(tmp_path)
    result = importer.import_altium(NO_KICAD, str(src), output_dir=str(tmp_path / "out"))
    assert result["erc_violations"] is None
    assert any(i["kind"] == "erc_failed" for i in result["issues"]["items"])
    assert (tmp_path / "out" / "import_report.json").is_file()


def test_failure_halfway_leaves_no_folder(tmp_path, monkeypatch):
    src = _sources(tmp_path)
    out = tmp_path / "out"

    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sheets, "write_sheets", broken)
    with pytest.raises(KicadMcpError, match="writing the schematic.*disk full"):
        importer.import_altium(NO_KICAD, str(src), output_dir=str(out))
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["src"]  # no staging folder left either

    monkeypatch.undo()
    result = importer.import_altium(NO_KICAD, str(src), output_dir=str(out))  # no overwrite needed
    assert Path(result["output_dir"]) == out and (out / "import_report.json").is_file()


def test_failure_halfway_keeps_the_previous_import(tmp_path, monkeypatch):
    src = _sources(tmp_path)
    out = tmp_path / "out"
    importer.import_altium(NO_KICAD, str(src), output_dir=str(out))
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    monkeypatch.setattr(sheets, "write_sheets", lambda *a, **k: 1 / 0)
    with pytest.raises(KicadMcpError):
        importer.import_altium(NO_KICAD, str(src), output_dir=str(out), overwrite=True)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_overwrite_replaces_only_what_the_import_wrote(tmp_path):
    src = _sources(tmp_path)
    out = tmp_path / "out"
    importer.import_altium(NO_KICAD, str(src), output_dir=str(out))
    (out / "my_notes.txt").write_text("keep me", encoding="utf-8")
    # A sheet an earlier import wrote and this one no longer does.
    (out / "old_sheet.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    report = json.loads((out / "import_report.json").read_text(encoding="utf-8"))
    report["files"].append(str(out / "old_sheet.kicad_sch"))
    (out / "import_report.json").write_text(json.dumps(report), encoding="utf-8")

    result = importer.import_altium(NO_KICAD, str(src), output_dir=str(out), overwrite=True)
    names = {p.name for p in out.iterdir()}
    assert "my_notes.txt" in names and "old_sheet.kicad_sch" not in names
    assert {Path(f).name for f in result["files"]["items"]} <= names


def test_report_lists_every_file_it_wrote(tmp_path):
    src = _sources(tmp_path)
    out = tmp_path / "out"
    result = importer.import_altium(NO_KICAD, str(src), output_dir=str(out))
    listed = {Path(f).name for f in json.loads((out / "import_report.json").read_text(encoding="utf-8"))["files"]}
    assert listed == {p.name for p in out.iterdir()}
    assert all(str(out) in f for f in result["files"]["items"])
