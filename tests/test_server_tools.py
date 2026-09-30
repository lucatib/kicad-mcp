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
