"""Tool-level behaviour that lives in server.py rather than the modules below it."""

from __future__ import annotations

import pytest

from kicad_mcp import server
from kicad_mcp.generator import generate_pinout_schematic

pytestmark = pytest.mark.requires_kicad


@pytest.fixture
def sch(tmp_path):
    try:
        index = server.symbol_index(None)
        out = tmp_path / "t.kicad_sch"
        generate_pinout_schematic(index, out, "RF_Module:ESP32-S3-MINI-1", {"IO4": "LED"})
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")
    return out


def test_placing_with_an_unresolvable_footprint_warns(sch):
    res = server.add_symbol_to_schematic(
        str(sch), "Connector_Generic:Conn_01x03", {}, reference="J1",
        footprint="PCM_Nope:Some_Footprint",
    )
    assert "error" not in res, res
    assert any("PCM_Nope:Some_Footprint" in w for w in res["warnings"])


def test_placing_with_a_resolvable_library_default_does_not_warn(sch):
    res = server.add_symbol_to_schematic(str(sch), "Converter_ACDC:HLK-PM01", {}, reference="PS1")
    assert res["footprint"] == "Converter_ACDC:Converter_ACDC_Hi-Link_HLK-PMxx"
    assert res["warnings"] == []


def test_explicit_coordinates_are_honoured(sch):
    res = server.add_symbol_to_schematic(
        str(sch), "Connector_Generic:Conn_01x03", {}, reference="J1", x=50.8, y=50.8,
    )
    assert res["position"] == {"x": 50.8, "y": 50.8}


def test_wiring_tools_pass_global_nets_through(sch):
    placed = server.add_symbol_to_schematic(
        str(sch), "Connector_Generic:Conn_01x03", {"1": "SDA", "2": "GND"},
        reference="J1", global_nets=["SDA"],
    )
    assert "error" not in placed, placed
    assert [c["via"] for c in placed["connected"]] == ["global_label", "power_symbol"]
    labelled = server.label_pins(str(sch), "J1", {"3": "SCL"}, global_nets=["SCL"])
    assert "error" not in labelled, labelled
    assert labelled["connected"][0]["via"] == "global_label"


def test_set_symbol_fields_tool_round_trip(sch):
    res = server.set_symbol_fields(str(sch), "U1", {"Value": "MCU", "MPN": "X-1"})
    assert "error" not in res, res
    assert res["changed"]["Value"]["new"] == "MCU"
    assert res["added"] == ["MPN"]


def test_footprint_tools_are_registered_and_answer():
    hits = server.search_footprints("HLK-PM")
    assert "Converter_ACDC:Converter_ACDC_Hi-Link_HLK-PMxx" in [h["lib_id"] for h in hits["results"]]
    info = server.get_library_footprint("Converter_ACDC:Converter_ACDC_Hi-Link_HLK-PMxx")
    assert info["pad_count"] == 4


def test_remove_symbols_dry_run_reports_without_writing(sch):
    before = sch.read_text(encoding="utf-8")
    res = server.remove_symbols(str(sch), ["U1"], dry_run=True)
    assert "error" not in res, res
    assert res["removed"] == ["U1"]
    assert res["written"] is False
    assert res["cleaned_up"]["power_symbols"], "reports what would go"
    assert sch.read_text(encoding="utf-8") == before


def test_remove_symbols_tool_writes(sch):
    res = server.remove_symbols(str(sch), ["U1"])
    assert "error" not in res, res
    assert res["written"] is True
    assert server.list_schematic_symbols(str(sch))["count"] == 0


def test_remove_symbols_unknown_reference_is_an_error_not_an_exception(sch):
    res = server.remove_symbols(str(sch), ["J99"])
    assert res["error"] == "ToolInputError"
    assert "J99" in res["message"]


def test_rename_labels_dry_run_reports_without_writing(sch):
    before = sch.read_text(encoding="utf-8")
    res = server.rename_labels(str(sch), {"LED": "STATUS"}, dry_run=True)
    assert "error" not in res, res
    assert res["renamed"] == [{"from": "LED", "to": "STATUS", "labels": 1}]
    assert res["written"] is False
    assert sch.read_text(encoding="utf-8") == before


def test_rename_labels_tool_writes(sch):
    res = server.rename_labels(str(sch), {"LED": "STATUS"})
    assert res["written"] is True
    assert '(label "STATUS"' in sch.read_text(encoding="utf-8")


def test_rename_labels_collision_is_an_error_not_an_exception(sch):
    res = server.rename_labels(str(sch), {"LED": "GND"})
    assert res["error"] == "ToolInputError"
