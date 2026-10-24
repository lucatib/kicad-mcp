"""Checks that a converted project is really aligned.

Nets are compared as partitions of (reference, pad) sets, so a schematic net
called /power/SDA matches a board net called SDA when they join the same
pads; only a different grouping of pads is a topology difference.
"""

from __future__ import annotations

from kicad_mcp.altium import verify


def test_equal_partitions_match_despite_names():
    board = {"SDA": {("U1", "1"), ("R1", "2")}, "GND": {("U1", "2"), ("C1", "1")}}
    sch = {"/power/SDA": {("U1", "1"), ("R1", "2")}, "GND": {("U1", "2"), ("C1", "1")}}
    out = verify.compare_nets(board, sch, set())
    assert out["matched"] == {"SDA": "/power/SDA", "GND": "GND"}
    assert out["topology_differences"] == []


def test_split_net_is_topology_difference():
    board = {"N1": {("U1", "1"), ("R1", "2"), ("C1", "1")}}
    sch = {"A": {("U1", "1"), ("R1", "2")}, "B": {("C1", "1"), ("C2", "2")}}
    (diff,) = verify.compare_nets(board, sch, set())["topology_differences"]
    assert diff["board_net"] == "N1" and diff["closest_schematic_net"] == "A"
    assert diff["only_on_board"] == ["C1.1"] and diff["only_in_schematic"] == []


def test_excluded_refs_removed_first():
    board = {"N1": {("U1", "1"), ("R1", "2"), ("C22P", "1")}}
    sch = {"A": {("U1", "1"), ("R1", "2")}}
    out = verify.compare_nets(board, sch, {"C22P"})
    assert out["matched"] == {"N1": "A"} and out["excluded_parts"] == ["C22P"]


def test_single_pad_nets_ignored():
    # A board net on one pad and an unconnected schematic pin say the same thing.
    board = {"TP": {("TP1", "1")}, "N": {("R1", "1"), ("R2", "1")}}
    sch = {"unconnected-(TP1-Pad1)": {("TP1", "1")}, "/N": {("R1", "1"), ("R2", "1")}}
    out = verify.compare_nets(board, sch, set())
    assert out["topology_differences"] == []
    assert out["matched"] == {"N": "/N", "TP": "unconnected-(TP1-Pad1)"}


def test_schematic_only_net_reported():
    board = {"N": {("R1", "1"), ("R2", "1")}}
    sch = {"/N": {("R1", "1"), ("R2", "1")}, "/X": {("R3", "1"), ("R4", "1")}}
    (diff,) = verify.compare_nets(board, sch, set())["topology_differences"]
    assert diff["schematic_net"] == "/X" and diff["board_net"] is None


PARITY = {"schematic_parity": [
    {"type": "footprint_symbol_field_mismatch", "description": "x",
     "items": [{"description": "Footprint C10C"}]},
    {"type": "net_conflict", "description": "No corresponding pin found in schematic",
     "items": [{"description": "Pad 1 [C_EDP_TX1_N] of C10C on Top Layer"}]},
    {"type": "missing_footprint", "description": "Missing footprint R99 (Res)",
     "items": [{"description": "Symbol R99 [Res]"}]},
    {"type": "extra_footprint", "description": "x", "items": [{"description": "Footprint LOGO1"}]},
]}


def test_parity_summary_counts_and_unexplained():
    out = verify.summarize_parity(PARITY, known_refs={"C10C", "LOGO1"})
    assert (out["missing"], out["extra"], out["duplicates"]) == (1, 1, 0)
    assert out["other"] == {"footprint_symbol_field_mismatch": 1, "net_conflict": 1}
    assert [u["refs"] for u in out["unexplained"]] == [["R99"]]


def test_parity_refs_parsed():
    assert verify.refs_in({"description": "Pad 12 [N] of U10C on Top Layer"}) == {"U10C"}
    assert verify.refs_in({"description": "Footprint C10C"}) == {"C10C"}
    assert verify.refs_in({"description": "Symbol R99 [Res]"}) == {"R99"}


def test_auto_net_names_keep_kicads_slash_escape():
    assert verify.kicad_net_name("unconnected-(U11P-NC/ADJ-Pad5)") == "unconnected-(U11P-NC{slash}ADJ-Pad5)"
    assert verify.kicad_net_name("Net-(U1-A/B)") == "Net-(U1-A{slash}B)"
    assert verify.kicad_net_name("/power/SDA") == "/power/SDA"


def test_single_pad_nets_matched_for_renaming():
    # A one-pad net is not a topology question, but its name still has to follow
    # the schematic, or KiCad's update renames it.
    board = {"EEM1_5_P": {("J2", "7")}, "N": {("R1", "1"), ("R2", "1")}}
    sch = {"/U_LVDS_EEM1/EEM1_5_P": {("J2", "7")}, "/N": {("R1", "1"), ("R2", "1")}}
    out = verify.compare_nets(board, sch, set())
    assert out["matched"] == {"EEM1_5_P": "/U_LVDS_EEM1/EEM1_5_P", "N": "/N"}
    assert out["topology_differences"] == []


def test_parity_without_a_report_is_an_error(monkeypatch, tmp_path):
    from kicad_mcp import cli
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    stale = tmp_path / "parity.json"
    stale.write_text('{"schematic_parity": []}', encoding="utf-8")
    monkeypatch.setattr(cli, "run_cli", lambda *a, **k: {"returncode": 1, "stdout": "", "stderr": "boom"})
    out = verify.parity(None, pcb, set(), output=stale)
    assert out.get("error") and out["unexplained"]
