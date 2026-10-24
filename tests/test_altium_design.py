"""The design behind a set of Altium sheets: root, instances, KiCad paths.

KiCad links a footprint to a symbol by the path of sheet UUIDs down to the
symbol. Altium's board stores the same path as UniqueIds, so the design maps
every UniqueId through the UUID KiCad's own board importer derives from it.
"""

from __future__ import annotations

import shutil

import pytest

from altium_fixtures import altium_fixture, altium_set
from kicad_mcp.altium import design as dz
from kicad_mcp.altium import model, pcbdoc
from kicad_mcp.errors import ToolInputError


def _sheets(prefix):
    return [model.load_sheet(p) for p in altium_set(prefix) if p.suffix.lower() == ".schdoc"]


@pytest.fixture(scope="module")
def edp_parts():
    return pcbdoc.read_board_parts(altium_fixture("eDP/eDP_adapter_dvt1.PcbDoc"))


@pytest.fixture(scope="module")
def sampler_sheets():
    return _sheets("sampler")


@pytest.fixture(scope="module")
def sampler_parts():
    return pcbdoc.read_board_parts(altium_fixture("sampler/Sampler.PCBDOC"))


def test_uuid_matches_kicad():
    assert dz.altium_kiid("GHHNYTHT") == "0a04476f-831d-59cf-a134-9523a5894990"
    assert dz.altium_kiid("\\IMJRJHOQ") == "cb8c9dff-72b7-5ec6-8c1f-0963372f7ffe"


def test_flat_design_synthesizes_root(edp_parts):
    d = dz.build_design(_sheets("eDP"), "eDP", edp_parts, None)
    assert d.flat and d.root.sheet is None
    assert [c.sheet.stem for c in d.root.children] == ["it6251_core", "power"]
    assert any(i["what"].startswith("flat design") for i in d.inferred)
    (power,) = [c for c in d.root.children if c.sheet.stem == "power"]
    assert power.kicad_uuid == dz.flat_sheet_uuid("power")
    assert dz.footprint_kicad_path(d, ("GHHNYTHT",)) == (
        "/" + dz.flat_sheet_uuid("power") + "/" + dz.altium_kiid("GHHNYTHT"))


def test_hierarchy_sampler(sampler_sheets, sampler_parts):
    d = dz.build_design(sampler_sheets, "Sampler", sampler_parts, None)
    assert not d.flat and d.root.sheet.stem == "Sampler"
    assert len(d.root.children) == 12
    channels = [c for c in d.root.children if c.sheet and c.sheet.stem == "Input_channel"]
    assert len(channels) == 8
    (ch,) = [c for c in channels if c.sheet_symbol.uid == "GBVAIBAE"]
    assert ch.kicad_uuid == dz.altium_kiid("GBVAIBAE")
    assert ch.kicad_path == "/" + d.root_uuid + "/" + dz.altium_kiid("GBVAIBAE")
    assert dz.footprint_kicad_path(d, ("GBVAIBAE", "JSBUTAQD")) == (
        "/" + dz.altium_kiid("GBVAIBAE") + "/" + dz.altium_kiid("JSBUTAQD"))
    assert dz.footprint_kicad_path(d, ("GBVAIBAE", "NOSUCHID")) is None
    assert d.mode == "hierarchical"


def test_root_from_board_when_extra_sheet(sampler_sheets, sampler_parts):
    stray = model.load_sheet(altium_fixture("eDP/power.SchDoc"))
    d = dz.build_design(sampler_sheets + [stray], "Sampler", sampler_parts, None)
    assert d.root.sheet.stem == "Sampler"
    assert d.unreferenced == ["power"]


def test_two_roots_without_board_is_an_error(sampler_sheets):
    stray = model.load_sheet(altium_fixture("eDP/power.SchDoc"))
    with pytest.raises(ToolInputError, match="Sampler"):
        dz.build_design(sampler_sheets + [stray], "Sampler", None, None)


def test_missing_child_reported(sampler_sheets, sampler_parts):
    kept = [s for s in sampler_sheets if s.stem != "LVDS_IFCB"]
    d = dz.build_design(kept, "Sampler", sampler_parts, None)
    (node,) = [c for c in d.root.children if c.file_name == "LVDS_IFCB.SchDoc"]
    assert node.sheet is None
    assert {"kind": "missing_sheet", "file": "LVDS_IFCB.SchDoc"} in d.issues


def test_child_file_matched_case_insensitively(sampler_sheets, sampler_parts, tmp_path):
    src = altium_fixture("sampler/Input_channel.SchDoc")
    shutil.copy(src, tmp_path / "INPUT_CHANNEL.SCHDOC")
    renamed = [s for s in sampler_sheets if s.stem != "Input_channel"]
    renamed.append(model.load_sheet(tmp_path / "INPUT_CHANNEL.SCHDOC"))
    d = dz.build_design(renamed, "Sampler", sampler_parts, None)
    assert all(c.sheet is not None for c in d.root.children)


def test_board_refs_win(sampler_sheets, sampler_parts):
    d = dz.build_design(sampler_sheets, "Sampler", sampler_parts, None)
    refs = dz.instance_references(d, {("GBVAIBAE", "JSBUTAQD"): "C10Q"})
    (ch,) = [c for c in d.root.children if c.sheet_symbol.uid == "GBVAIBAE"]
    assert refs[(ch.kicad_path, "JSBUTAQD")] == "C10Q"


def test_channel_suffix_inferred_without_board(sampler_sheets):
    d = dz.build_design(sampler_sheets, "Sampler", None, None)
    refs = dz.instance_references(d, {})
    channels = [c for c in d.root.children if c.sheet and c.sheet.stem == "Input_channel"]
    got = sorted(refs[(c.kicad_path, "JSBUTAQD")] for c in channels)
    assert got == [f"C10{x}" for x in "ABCDEFGH"]
    assert any("Input_channel" in i["what"] for i in d.inferred)
    # A sheet used once keeps the plain designator.
    (supply,) = [c for c in d.root.children if c.sheet.stem == "Supply_Sampler"]
    part = supply.sheet.parts[0]
    assert refs[(supply.kicad_path, part.uid)] == part.designator


def test_channel_format_from_prjpcb(sampler_sheets, tmp_path):
    prj = tmp_path / "x.PrjPcb"
    prj.write_text("[Design]\nChannelDesignatorFormatString=$Component_$ChannelIndex\n",
                   encoding="latin-1")
    d = dz.build_design(sampler_sheets, "Sampler", None, prj)
    refs = dz.instance_references(d, {})
    channels = [c for c in d.root.children if c.sheet and c.sheet.stem == "Input_channel"]
    assert sorted(refs[(c.kicad_path, "JSBUTAQD")] for c in channels)[0] == "C10_1"


def test_mode_automatic():
    # pagea holds a sheet symbol with entries: hierarchical.
    pagea = dz.build_design([model.load_sheet(altium_fixture("qa/ticket1303/pagea.SchDoc"))],
                            "t", None, None)
    assert pagea.mode == "hierarchical"
    # eDP has no sheet symbols: flat when it has ports, global otherwise.
    sheets = _sheets("eDP")
    expected = "flat" if any(s.ports for s in sheets) else "global"
    assert dz.build_design(sheets, "eDP", None, None).mode == expected


def test_mode_from_prjpcb(tmp_path):
    prj = tmp_path / "x.PrjPcb"
    prj.write_text("[Design]\nHierarchyMode=4\n", encoding="latin-1")
    assert dz.build_design(_sheets("eDP"), "eDP", None, prj).mode == "global"


def test_duplicate_sheet_names_get_channel_suffix(sampler_sheets, sampler_parts):
    # KiCad needs sibling sheet names to be unique; Altium allows eight
    # "U_Input_channel". Designators are untouched by this.
    d = dz.build_design(sampler_sheets, "Sampler", sampler_parts, None)
    channels = {c.sheet_symbol.uid: c for c in d.root.children if c.sheet.stem == "Input_channel"}
    assert channels["GBVAIBAE"].name == "U_Input_channel_A"
    assert channels["TJKXBNXR"].name == "U_Input_channel_H"
    assert channels["GBVAIBAE"].human_path == "/U_Input_channel_A/"
    (adc,) = [c for c in d.root.children if c.sheet_symbol.uid == "UMAXLLEH"]
    assert adc.name == "U_ADC_8CH" and d.root.human_path == "/"
    assert any("U_Input_channel" in i["what"] for i in d.inferred)


def test_flat_sheet_names_are_stems(edp_parts):
    d = dz.build_design(_sheets("eDP"), "eDP", edp_parts, None)
    assert [c.human_path for c in d.root.children] == ["/it6251_core/", "/power/"]


def test_other_units_take_the_linked_units_reference(sampler_sheets, sampler_parts):
    d = dz.build_design(sampler_sheets, "Sampler", sampler_parts, None)
    (ch,) = [c for c in d.root.children if c.sheet_symbol.uid == "GBVAIBAE"]
    rn1 = [p for p in ch.sheet.parts if p.designator == "RN1"]
    assert len(rn1) == 4
    linked = next(p for p in rn1 if (ch.id_path + (p.uid,)) in {bp.id_path for bp in sampler_parts})
    board_refs = {ch.id_path + (linked.uid,): "RN1Q"}
    refs = dz.instance_references(d, board_refs)
    assert {refs[(ch.kicad_path, p.uid)] for p in rn1} == {"RN1Q"}


def test_sheet_file_name_without_extension_resolves(tmp_path):
    # Altium may store FILE_NAME without ".SchDoc" (KiCad's issue22943 sample).
    cover = model.load_sheet(altium_fixture("qa/issue22943/1_cover.SchDoc"))
    child = tmp_path / "2_cubesat_bus.SchDoc"
    shutil.copy(altium_fixture("eDP/power.SchDoc"), child)
    d = dz.build_design([cover, model.load_sheet(child)], "cover", None, None)
    assert d.root.sheet.stem == "1_cover"
    (bus,) = [c for c in d.root.children if c.sheet_symbol.uid == "MQALSEGY"]
    assert bus.sheet is not None and bus.sheet.stem == "2_cubesat_bus"


def test_shared_or_missing_unique_ids_keep_their_designators(tmp_path):
    f = tmp_path / "dup.SchDoc"
    lines = ["|HEADER=Protel for Windows - Schematic Capture Ascii File Version 5.0",
             "|RECORD=31|FONTIDCOUNT=1|SIZE1=10|FONTNAME1=Arial"]
    for i, (des, uid) in enumerate((("R1", "AAAAAAAA"), ("R2", "AAAAAAAA"), ("R3", ""), ("R4", ""))):
        idx = len(lines) - 1
        lines.append(f"|RECORD=1|LIBREFERENCE=Res|LOCATION.X={100 + 50 * i}|LOCATION.Y=100"
                     f"|CURRENTPARTID=1|PARTCOUNT=2" + (f"|UNIQUEID={uid}" if uid else ""))
        lines.append(f"|RECORD=34|NAME=Designator|TEXT={des}|OWNERINDEX={idx}")
    f.write_text("\r\n".join(lines) + "\r\n", encoding="latin-1")
    sheet = model.load_sheet(f)
    assert len({p.uid for p in sheet.parts}) == 4
    assert sorted(sheet.id_problems) == ["R1", "R2", "R3", "R4"]
    d = dz.build_design([sheet], "dup", None, None)
    refs = dz.instance_references(d, {})
    assert sorted(refs.values()) == ["R1", "R2", "R3", "R4"]


def test_sheet_named_like_the_project_gets_its_own_file(edp_parts):
    sheets = _sheets("eDP")
    d = dz.build_design(sheets, "power", edp_parts, None)
    names = {dz.sheet_file_name(d, n) for n in d.nodes()} | {dz.sheet_file_name(d, d.root)}
    assert len(names) == 3 and "power.kicad_sch" in names and "power_sheet.kicad_sch" in names
