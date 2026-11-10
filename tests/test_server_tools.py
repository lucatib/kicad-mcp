"""Tool-level behaviour that lives in server.py rather than the modules below it."""

from __future__ import annotations

import re

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


def test_list_symbol_libraries_filters_and_caps():
    res = server.list_symbol_libraries(name="Valve")
    assert res["items"] == ["Valve"]
    assert server.list_symbol_libraries(name="Valve", include_paths=True)["items"][0]["path"]
    capped = server.list_symbol_libraries(limit=1)
    assert capped["shown"] == 1
    assert capped["truncated"] is (capped["count"] > 1)


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


def test_units_of_a_dual_triode_join_one_part(sch):
    first = server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {"A": "ANODE_B", "G": "GRID_B", "K": "GND"},
                                           reference="V1", unit=1)
    assert "error" not in first, first
    second = server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {"A": "ANODE_A", "G": "GRID_A"},
                                            reference="V1", unit=2)
    assert "error" not in second, second
    assert second["unit"] == 2 and second["warnings"] == []
    assert second["footprint"] == first["footprint"]
    heater = server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {"4": "HEATER", "5": "HEATER", "9": "GND"},
                                            reference="V1", unit=3)
    assert "error" not in heater, heater
    nets = {(n["reference"], n["pin"]): net["name"].split("/")[-1]
            for net in server.schematic_netlist(str(sch))["nets"] for n in net["nodes"]}
    assert nets[("V1", "6")] == "ANODE_B" and nets[("V1", "1")] == "ANODE_A"
    assert nets[("V1", "4")] == nets[("V1", "5")] == "HEATER"
    listed = [s for s in server.list_schematic_symbols(str(sch))["items"] if s["reference"] == "V1"]
    assert len({s.get("value") for s in listed}) == 1


def test_a_placed_unit_cannot_be_placed_twice_or_under_another_part(sch):
    assert "error" not in server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {}, reference="V1", unit=1)
    again = server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {}, reference="V1", unit=1)
    assert again["error"] == "ToolInputError" and "already placed" in again["message"]
    other = server.add_symbol_to_schematic(str(sch), "Device:R", {}, reference="V1")
    assert other["error"] == "ToolInputError" and "Valve:ECC83" in other["message"]
    missing = server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {}, reference="V2", unit=4)
    assert missing["error"] == "ToolInputError" and "no unit 4" in missing["message"]


def test_mark_pins_unused_finds_a_pin_on_a_later_unit(sch):
    server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {}, reference="V1", unit=1, x=60.96, y=60.96)
    server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {}, reference="V1", unit=2, x=101.6, y=60.96)
    res = server.mark_pins_unused(str(sch), "V1", ["1"])
    assert "error" not in res, res
    # the cross sits on unit 2's own pin 1, not where unit 1 would put it
    report = server.run_erc(str(sch))["report"]
    assert "V1 Pin 1 " not in report
    assert "V1 Pin 2 " in report  # an unmarked pin still shows, so the check can see pins at all


def test_mirror_and_angle_move_the_pins_they_should(sch):
    plain = server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {"G": "IN_A"}, reference="V1", unit=1,
                                           x=101.6, y=101.6)
    facing = server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {"G": "IN_B"}, reference="V2", unit=1,
                                            x=152.4, y=101.6, mirror="y")
    assert "error" not in plain and "error" not in facing, (plain, facing)
    lying = server.add_symbol_to_schematic(str(sch), "Device:R", {"1": "IN_A", "2": "IN_B"}, reference="R1",
                                           x=127.0, y=127.0, angle=90)
    assert "error" not in lying, lying
    assert lying["angle"] == 90
    assert "(mirror y)" in sch.read_text(encoding="utf-8")
    # each end of the lying resistor lands on the grid it names: V1's on its left, the mirrored V2's on its right
    nets = {(n["reference"], n["pin"]): net["name"].split("/")[-1]
            for net in server.schematic_netlist(str(sch))["nets"] for n in net["nodes"]}
    assert nets[("V1", "7")] == nets[("R1", "1")] == "IN_A"
    assert nets[("V2", "7")] == nets[("R1", "2")] == "IN_B"


def test_mirror_and_angle_need_a_position_and_a_legal_value(sch):
    for kwargs, word in (({"mirror": "y"}, "position"), ({"angle": 90}, "position"),
                         ({"angle": 45, "x": 127.0, "y": 127.0}, "angle"),
                         ({"mirror": "z", "x": 127.0, "y": 127.0}, "mirror")):
        res = server.add_symbol_to_schematic(str(sch), "Device:R", {}, reference="R1", **kwargs)
        assert res["error"] == "ToolInputError" and word in res["message"], (kwargs, res)


def test_library_fields_keeps_the_reference_where_the_library_draws_it(sch):
    def reference_at(ref):
        block = re.search(r'\(property "Reference" "%s"\s*\(at ([-\d.]+) ([-\d.]+)' % ref, sch.read_text(encoding="utf-8"))
        return float(block.group(1)), float(block.group(2))

    server.add_symbol_to_schematic(str(sch), "Device:R", {}, reference="R1", x=101.6, y=127.0)
    server.add_symbol_to_schematic(str(sch), "Device:R", {}, reference="R2", x=127.0, y=127.0, library_fields=True)
    assert reference_at("R1")[0] == 101.6, "default: centred over the pins"
    assert reference_at("R2")[0] > 127.0, "library: beside the body"


def test_a_derived_symbol_is_embedded_as_the_library_has_it(sch):
    # Valve:ECC83 extends ECC81 and D:1N4007 extends 1N4001; fields only the base
    # declares must come along, or ERC reports the copy as differing from the library.
    server.add_symbol_to_schematic(str(sch), "Valve:ECC83", {}, reference="V1", unit=1, x=101.6, y=127.0)
    server.add_symbol_to_schematic(str(sch), "Diode:1N4007", {}, reference="D1", x=152.4, y=127.0)
    report = server.run_erc(str(sch), severity_all=True)["report"]
    assert "lib_symbol_mismatch" not in report, report
