"""The kicad-cli-imported board, re-linked to the converted schematic.

KiCad's Altium board importer keeps the designators but gives every footprint
a random sheet UUID, so its paths point nowhere. Rewriting them from the
board's SOURCEUNIQUEID makes each footprint find its symbol by path.
"""

from __future__ import annotations

import pytest

from altium_fixtures import altium_fixture, altium_set
from kicad_mcp.altium import board, model, pcbdoc
from kicad_mcp.altium import design as dz
from kicad_mcp.discovery import find_install

SAMPLE = '''(kicad_pcb
\t(version 20260206)
\t(footprint "Lib:R"
\t\t(layer "F.Cu")
\t\t(uuid "a")
\t\t(at 10 20 90)
\t\t(property "Reference" "R1"
\t\t\t(at 0 0 0)
\t\t)
\t\t(path "/x/y")
\t\t(sheetname "s")
\t\t(sheetfile "s.kicad_sch")
\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t\t(net "SDA")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 1 0)
\t\t\t(net "SDA2")
\t\t)
\t)
\t(segment
\t\t(net "SDA")
\t)
)
'''


def test_footprints_parsed():
    (fp,) = board.footprints(SAMPLE)
    assert (fp.ref, fp.lib_id, fp.x, fp.y, fp.path) == ("R1", "Lib:R", 10.0, 20.0, "/x/y")
    assert fp.pads == {"1": "SDA", "2": "SDA2"}


def test_rename_nets_whole_token():
    out = board.rename_nets(SAMPLE, {"SDA": "/power/SDA"})
    assert out.count('(net "/power/SDA")') == 2
    assert '(net "SDA2")' in out


def test_board_pad_nets():
    assert board.board_pad_nets(SAMPLE) == {"SDA": {("R1", "1")}, "SDA2": {("R1", "2")}}


def test_footprint_fields():
    assert board.footprint_fields(SAMPLE) == {"R1": "Lib:R"}


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"KiCad not installed: {exc}")


def _imported(install, tmp, pcbdoc_path):
    out = tmp / "b.kicad_pcb"
    report = board.import_board(install, pcbdoc_path, out)
    assert report["errors"] == [] and out.is_file()
    return out.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def edp(install, tmp_path_factory):
    src = altium_fixture("eDP/eDP_adapter_dvt1.PcbDoc")
    text = _imported(install, tmp_path_factory.mktemp("edp"), src)
    sheets = [model.load_sheet(p) for p in altium_set("eDP") if p.suffix == ".SchDoc"]
    parts = pcbdoc.read_board_parts(src)
    return text, parts, dz.build_design(sheets, "eDP", parts, None)


@pytest.mark.requires_kicad
def test_join_edp(edp):
    text, parts, _ = edp
    joins = board.join_footprints(text, parts)
    matched = [(fp, part) for fp, part in joins if part is not None]
    assert len(matched) == 112
    assert all(fp.ref == part.source_designator for fp, part in matched)


@pytest.mark.requires_kicad
def test_paths_rewritten_flat(edp):
    text, parts, d = edp
    new, refs, issues = board.rewrite_board(text, board.join_footprints(text, parts), d)
    (c10p,) = [f for f in board.footprints(new) if f.ref == "C10P"]
    assert c10p.path == "/" + dz.flat_sheet_uuid("power") + "/" + dz.altium_kiid("GHHNYTHT")
    assert '(sheetfile "power.kicad_sch")' in new
    assert refs[("GHHNYTHT",)] == "C10P"
    # C22P's board id is on no schematic sheet: reported, path left alone.
    assert any(i["ref"] == "C22P" and i["kind"] == "no_schematic_part" for i in issues)


@pytest.mark.requires_kicad
@pytest.mark.slow
def test_join_sampler_channels(install, tmp_path_factory):
    src = altium_fixture("sampler/Sampler.PCBDOC")
    text = _imported(install, tmp_path_factory.mktemp("sampler"), src)
    parts = pcbdoc.read_board_parts(src)
    joins = board.join_footprints(text, parts)
    by_ref = {fp.ref: part for fp, part in joins if part is not None}
    assert {by_ref[f"C10{x}"].id_path[0] for x in "ABCDEFGH"} == {
        "GBVAIBAE", "YXCFEEXA", "YMGJCCJH", "MHXVSFEO", "BGKERBNH", "JMNDQDBV", "NOURFBSN", "TJKXBNXR"}
    assert by_ref["C10A"].id_path[0] == "GBVAIBAE" and by_ref["C10H"].id_path[0] == "TJKXBNXR"


def test_assign_pad_nets_only_fills_empty_pads():
    text = SAMPLE.replace('\t\t\t(net "SDA2")\n', "")
    out = board.assign_pad_nets(text, {("R1", "2"): "unconnected-(R1-Pad2)", ("R1", "1"): "X"})
    (fp,) = board.footprints(out)
    assert fp.pads == {"1": "SDA", "2": "unconnected-(R1-Pad2)"}


def test_add_fields_only_missing_and_hidden():
    out = board.add_fields(SAMPLE, {"R1": {"Published": "2017", "Reference": "ignored"}})
    (fp,) = board.footprints(out)
    assert out.count('(property "Published" "2017"') == 1
    assert out.count('(property "Reference"') == 1
    block = out[out.index('(property "Published"'):]
    assert "(hide yes)" in block.split("\t\t)")[0]
    assert board.add_fields(out, {"R1": {"Published": "2017"}}) == out


def test_fill_values_only_where_empty():
    text = SAMPLE.replace('\t\t(path "/x/y")', '\t\t(property "Value" ""\n\t\t\t(at 0 0 0)\n\t\t)\n\t\t(path "/x/y")')
    out = board.fill_values(text, {"R1": "Connector 30 Male"})
    assert board.footprints(out)[0].value == "Connector 30 Male"
    assert board.fill_values(out, {"R1": "other"}) == out
