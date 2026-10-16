"""validate_board_outline: does KiCad see a closed outline, and which cutouts?

When Edge.Cuts does not close, KiCad builds no board polygon and the 3D viewer
and exports fall back to the bounding box -- every cutout and milled slot
vanishes without an error in the editor. The tool runs KiCad's own outline
builder and, when it fails, points at the open ends.
"""

from __future__ import annotations

import math

import pytest

from kicad_mcp import pcb_common, server
from pcb_fixture import build_board, mm, pcbnew, pt

pytestmark = pytest.mark.requires_kicad


def _edit(path, change):
    board = pcbnew.LoadBoard(str(path))
    change(board)
    pcbnew.SaveBoard(str(path), board)
    return path


def test_a_closed_outline_with_a_round_cutout(tmp_path):
    path = build_board(tmp_path / "b.kicad_pcb")

    out = server.validate_board_outline(str(path))

    assert out["valid"] is True
    assert out["outline_count"] == 1
    (outline,) = out["outlines"]
    assert outline["bbox"] == {"x": 0.0, "y": 0.0, "width": 40.0, "height": 30.0}
    assert outline["cutout_count"] == 1
    assert outline["area_mm2"] == pytest.approx(40 * 30 - math.pi * 9, rel=0.01)
    assert outline["cutouts"][0]["bbox"]["width"] == pytest.approx(6.0, abs=0.01)
    assert out["dangling_ends"] == []
    assert out["edge_cuts_shapes"] == {"board": 5, "footprints": 0}


def test_a_slot_drawn_inside_a_footprint_counts_as_a_cutout(tmp_path):
    def add_slot(board):
        fp = board.FindFootprintByReference("J1")
        slot = pcbnew.PCB_SHAPE(fp)
        slot.SetShape(pcbnew.SHAPE_T_RECTANGLE)
        slot.SetLayer(pcbnew.Edge_Cuts)
        slot.SetStart(pt(12, 20))
        slot.SetEnd(pt(14, 25))
        slot.SetWidth(mm(0.05))
        fp.Add(slot)

    path = _edit(build_board(tmp_path / "b.kicad_pcb"), add_slot)

    out = server.validate_board_outline(str(path))

    assert out["valid"] is True
    assert out["outlines"][0]["cutout_count"] == 2
    assert out["edge_cuts_shapes"] == {"board": 5, "footprints": 1}


def test_a_gap_in_the_outline_is_invalid_and_its_ends_are_named(tmp_path):
    def open_top_edge(board):
        top = next(d for d in board.GetDrawings()
                   if d.GetLayer() == pcbnew.Edge_Cuts
                   and d.GetShape() == pcbnew.SHAPE_T_SEGMENT
                   and d.GetStart().y == 0 and d.GetEnd().y == 0)
        board.Remove(top)

    path = _edit(build_board(tmp_path / "b.kicad_pcb"), open_top_edge)

    out = server.validate_board_outline(str(path))

    assert out["valid"] is False
    assert out["outline_count"] == 0
    assert sorted((p["x"], p["y"]) for p in out["dangling_ends"]) == [(0.0, 0.0), (40.0, 0.0)]
    assert any("bounding box" in p for p in out["problems"])


def test_a_missing_file_is_an_error_not_an_exception(tmp_path):
    out = server.validate_board_outline(str(tmp_path / "nope.kicad_pcb"))

    assert out["error"] == "ToolInputError"


def test_dangling_ends_ignore_closed_shapes_and_joined_ends():
    rows = [
        {"type": "Segment", "start": {"x": 0, "y": 0}, "end": {"x": 10, "y": 0}},
        {"type": "Arc", "start": {"x": 10, "y": 0}, "end": {"x": 10, "y": 10.004}},
        {"type": "Segment", "start": {"x": 10, "y": 10}, "end": {"x": 0, "y": 10}},
        {"type": "Circle", "center": {"x": 5, "y": 5}, "radius_mm": 1},
    ]

    ends = pcb_common.dangling_ends(rows, epsilon_mm=0.01)

    assert sorted((p["x"], p["y"]) for p in ends) == [(0, 0), (0, 10)]
