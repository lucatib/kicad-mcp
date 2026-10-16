"""run_drc returns a parsed summary, not a slice of raw JSON.

On a dense board the first 20,000 characters of the report covered ~22
clearance violations and cut off mid-object; the 22 invalid_outline errors
further down never reached the caller. Counts over the whole report, plus a
filterable list, keep every kind of violation visible.
"""

from __future__ import annotations

import pytest

from kicad_mcp import cli, server
from pcb_fixture import build_board, pcbnew


def _violation(kind, severity="error"):
    return {
        "description": kind,
        "items": [{"description": "x", "pos": {"x": 1.0, "y": 2.0}, "uuid": "u"}],
        "severity": severity,
        "type": kind,
    }


REPORT = {
    "kicad_version": "10.0.6",
    "violations": [_violation("clearance") for _ in range(1300)]
    + [_violation("invalid_outline") for _ in range(22)]
    + [_violation("silk_over_copper", "warning") for _ in range(34)],
    "unconnected_items": [_violation("unconnected_items")] * 5,
    "schematic_parity": [_violation("missing_footprint", "warning")] * 2,
}


def test_counts_cover_the_whole_report():
    out = cli.summarize_drc_report(REPORT)

    assert out["violation_count"] == 1356
    assert out["counts_by_type"]["invalid_outline"] == 22
    assert out["counts_by_type"]["clearance"] == 1300
    assert out["counts_by_severity"] == {"error": 1322, "warning": 34}
    assert out["unconnected_count"] == 5
    assert out["schematic_parity_count"] == 2


def test_violations_filter_by_type_and_cap():
    out = cli.summarize_drc_report(REPORT, violation_type="invalid_outline", limit=10)

    assert out["matching"] == 22
    assert len(out["violations"]) == 10
    assert {v["type"] for v in out["violations"]} == {"invalid_outline"}
    assert out["truncated"] is True


def test_violations_filter_by_severity():
    out = cli.summarize_drc_report(REPORT, severity="warning", limit=100)

    assert out["matching"] == 34
    assert "truncated" not in out


def test_type_filter_takes_globs():
    out = cli.summarize_drc_report(REPORT, violation_type="invalid_*")

    assert out["matching"] == 22


@pytest.mark.requires_kicad
def test_run_drc_on_a_board_with_a_broken_outline_reports_it(tmp_path):
    path = build_board(tmp_path / "b.kicad_pcb")
    board = pcbnew.LoadBoard(str(path))
    edge = next(d for d in board.GetDrawings()
                if d.GetLayer() == pcbnew.Edge_Cuts and d.GetShape() == pcbnew.SHAPE_T_SEGMENT)
    board.Remove(edge)
    pcbnew.SaveBoard(str(path), board)

    out = server.run_drc(str(path))

    assert out["counts_by_type"].get("invalid_outline", 0) >= 1
    assert "report" not in out  # no raw text slice
    assert out["report_file"].endswith(".drc.json")
