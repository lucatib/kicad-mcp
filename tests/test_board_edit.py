"""Exercise native library creation and SWIG footprint identity on a real board."""

import json

import pytest

from kicad_mcp import board_edit, pcb_file
from kicad_mcp.discovery import find_install
from kicad_mcp.generator import generate_pinout_schematic
from kicad_mcp.symbols import SymbolIndex

pytestmark = pytest.mark.requires_kicad


def test_empty_native_library_can_save_first_footprint(tmp_path):
    (tmp_path / "test.kicad_pro").write_text("{}")
    install = find_install()
    result = board_edit.create_footprint(install, tmp_path, "trial", "Wire", [
        {"number": "1", "x": 0, "y": 0, "size": 3, "drill": 1.2},
    ])
    pcbnew = pcb_file._pcbnew()
    fp = pcbnew.FootprintLoad(str(tmp_path / "trial.pretty"), "Wire")
    assert fp is not None
    assert len(list(fp.Pads())) == 1
    assert result["registered"] is True


def test_sync_keeps_new_footprints_and_their_placement(tmp_path):
    (tmp_path / "test.kicad_pro").write_text("{}")
    install = find_install()
    sch = tmp_path / "test.kicad_sch"
    generate_pinout_schematic(SymbolIndex(install, tmp_path), sch,
                             "Converter_ACDC:HLK-PM01", {})
    first = board_edit.update_from_schematic(install, sch)
    assert first["added"] == ["U1"]
    assert first["removed"] == []
    board_edit.place_footprints(first["board"], [
        {"reference": "U1", "x": 80, "y": 60, "angle": 90},
    ])
    second = board_edit.update_from_schematic(install, sch)
    assert second["added"] == second["removed"] == []
    board = pcb_file._pcbnew().LoadBoard(first["board"])
    fp, = list(board.GetFootprints())
    assert fp.GetReference() == "U1"
    assert fp.GetPosition().x == 80_000_000
    assert fp.GetPosition().y == 60_000_000
    assert fp.GetOrientationDegrees() == 90


def test_net_classes_keep_other_classes_patterns(tmp_path):
    (tmp_path / "test.kicad_pro").write_text("{}")
    pcbnew = pcb_file._pcbnew()
    path = tmp_path / "test.kicad_pcb"
    board = pcbnew.NewBoard(str(path))
    for name in ("HV", "GND", "SIG"):
        board.Add(pcbnew.NETINFO_ITEM(board, name))
    pcbnew.SaveBoard(str(path), board)
    board_edit.set_net_classes(path, [{"name": "POWER", "nets": ["HV"]},
                                      {"name": "GROUND", "nets": ["GND"]}])
    board_edit.set_net_classes(path, [{"name": "POWER", "nets": ["HV", "SIG"]}])
    project = json.loads((tmp_path / "test.kicad_pro").read_text())
    patterns = {(r["pattern"], r["netclass"]) for r in project["net_settings"]["netclass_patterns"]}
    assert patterns == {("HV", "POWER"), ("SIG", "POWER"), ("GND", "GROUND")}
