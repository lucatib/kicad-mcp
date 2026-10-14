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


def test_move_symbol_fields_tool_writes(sch):
    res = server.move_symbol_fields(str(sch), "U1", {"Reference": {"dx": 2.54}})
    assert "error" not in res, res
    assert res["moved"][0]["field"] == "Reference"


def test_move_symbol_fields_unknown_field_is_an_error_not_an_exception(sch):
    res = server.move_symbol_fields(str(sch), "U1", {"Nope": {"dx": 1}})
    assert res["error"] == "ToolInputError"
    assert "Reference" in res["message"], "names the fields it does have"


def test_disconnect_pins_dry_run_reports_without_writing(sch):
    before = sch.read_text(encoding="utf-8")
    res = server.disconnect_pins(str(sch), "U1", pin_numbers=["8"], dry_run=True)
    assert "error" not in res, res
    assert res["cleaned_up"]["labels"] == ["LED"]
    assert res["written"] is False
    assert sch.read_text(encoding="utf-8") == before


def test_disconnect_pins_tool_writes(sch):
    res = server.disconnect_pins(str(sch), "U1", pin_numbers=["8"])
    assert res["written"] is True
    assert '(label "LED"' not in sch.read_text(encoding="utf-8")
    assert server.list_schematic_symbols(str(sch), reference="U1")["count"] == 1


def test_disconnect_pins_unknown_pin_is_an_error_not_an_exception(sch):
    res = server.disconnect_pins(str(sch), "U1", pin_numbers=["999"])
    assert res["error"] == "ToolInputError"
    assert "999" in res["message"]


def _with_note(sch, text):
    from kicad_mcp.editor import SchematicEditor

    editor = SchematicEditor.load(sch)
    builder = editor._builder()
    builder.add_text(text, (25.4, 160.02))
    editor._merge(builder)
    editor.save()


def test_edit_notes_dry_run_reports_without_writing(sch):
    _with_note(sch, "spare GPIO8, 9")
    before = sch.read_text(encoding="utf-8")
    res = server.edit_notes(str(sch), {"GPIO8, 9": "GPIO35-42"}, dry_run=True)
    assert "error" not in res, res
    assert res["edited"][0]["text"] == "spare GPIO35-42"
    assert res["written"] is False
    assert sch.read_text(encoding="utf-8") == before


def test_edit_notes_tool_writes_and_open_schematic_shows_it(sch):
    _with_note(sch, "spare GPIO8, 9")
    res = server.edit_notes(str(sch), {"GPIO8, 9": "GPIO35-42"})
    assert res["written"] is True
    assert server.open_schematic(str(sch))["notes"][0]["text"] == "spare GPIO35-42"


def test_edit_notes_missing_fragment_is_an_error_not_an_exception(sch):
    res = server.edit_notes(str(sch), {"nowhere": "x"})
    assert res["error"] == "ToolInputError"
    assert "nowhere" in res["message"]


def test_a_new_library_symbol_places_without_naming_the_project(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    sheet = folder / "proj.kicad_sch"
    try:
        generate_pinout_schematic(server.symbol_index(None), sheet, "Connector_Generic:Conn_01x03",
                                  {"1": "SDA"}, mcu_reference="J1")
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")

    made = server.create_library_symbol(
        str(sheet), "proj", "Shifter", [{"number": "1", "name": "LV1"}, {"number": "2", "name": "HV1"}],
        footprint="PCM_Nope:Some_Footprint",
    )
    assert "error" not in made, made
    assert made["lib_id"] == "proj:Shifter"
    assert any("PCM_Nope:Some_Footprint" in w for w in made["warnings"])

    placed = server.add_symbol_to_schematic(str(sheet), "proj:Shifter", {"LV1": "SDA"}, reference="U9")
    assert "error" not in placed, placed
    assert placed["connected"][0]["net"] == "SDA"


def test_create_library_symbol_outside_a_project_is_an_error_not_an_exception(tmp_path):
    res = server.create_library_symbol(str(tmp_path), "proj", "X", [{"number": "1"}])
    assert res["error"] == "ToolInputError"
    assert ".kicad_pro" in res["message"]
