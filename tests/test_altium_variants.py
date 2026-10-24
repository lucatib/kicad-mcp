"""Altium project variants carried into the KiCad project.

An Altium variant lists the parts it changes by designator and UniqueId path:
not fitted (Kind=1) or an alternate part (Kind=0). KiCad 10 keeps the same
idea per symbol instance and per footprint; the import writes both sides, so
an export for a variant gives the same build in either program.
"""

from __future__ import annotations

import pytest

from altium_fixtures import altium_fixture
from kicad_mcp.altium import board, variants

SAMPLE = '''(kicad_pcb
\t(version 20260206)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t)
\t(footprint "Lib:C"
\t\t(layer "F.Cu")
\t\t(at 10 20 90)
\t\t(property "Reference" "C17A"
\t\t\t(at 0 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t)
\t)
\t(footprint "Lib:J"
\t\t(layer "F.Cu")
\t\t(at 30 20 0)
\t\t(property "Reference" "J3"
\t\t\t(at 0 0 0)
\t\t)
\t)
)
'''


def test_read_sampler_variants():
    vs = variants.read_variants(altium_fixture("sampler/Sampler.PrjPCB"))
    assert [(v.name, len(v.variations)) for v in vs] == [("STD", 35), ("4HP", 4)]
    std = vs[0]
    (c17b,) = [e for e in std.variations if e.designator == "C17B"]
    assert c17b.id_path == ("YXCFEEXA", "SIJXDMPB") and c17b.kind == 1
    (j3,) = [e for e in vs[1].variations if e.designator == "J3"]
    assert j3.id_path == ("YHDIVBNN",)


def test_alternate_part_fields(tmp_path):
    prj = tmp_path / "p.PrjPcb"
    prj.write_text("[ProjectVariant1]\nDescription=Cheap\nVariationCount=1\n"
                   "Variation1=Designator=R1|UniqueId=\\ABCDEFGH|Kind=0|AlternatePart=RC0402"
                   "|Description=Thick film|Footprint=R0402\nParamVariationCount=2\n", encoding="latin-1")
    (v,) = variants.read_variants(prj)
    (e,) = v.variations
    assert e.kind == 0 and e.id_path == ("ABCDEFGH",)
    assert e.alternate == {"LibReference": "RC0402", "Description": "Thick film", "Footprint": "R0402"}
    assert v.param_variations == 2


def test_unnamed_variant_takes_its_section_name(tmp_path):
    prj = tmp_path / "p.PrjPcb"
    prj.write_text("[ProjectVariant3]\nDescription=\nVariationCount=1\n"
                   "Variation1=Designator=R1|UniqueId=\\ABCDEFGH|Kind=1|AlternatePart=\n", encoding="latin-1")
    assert [v.name for v in variants.read_variants(prj)] == ["ProjectVariant3"]


def test_no_variants_without_a_project():
    assert variants.read_variants(None) == []


def test_board_variants_written():
    out = board.add_variants(SAMPLE, [("STD", ""), ("4HP", "4HP front panel")],
                             {"C17A": [variants.KicadChange("STD", dnp=True)],
                              "J3": [variants.KicadChange("4HP", fields={"Value": "CONN"})]})
    assert '(variants\n\t\t(variant (name "STD"))\n\t\t(variant (name "4HP") (description "4HP front panel"))' in out
    c17a = out[out.index('"C17A"'):out.index('(footprint "Lib:J"')]
    assert '(variant (name "STD") (dnp yes) (exclude_from_bom yes) (exclude_from_pos_files yes))' in c17a
    j3 = out[out.index('"J3"'):]
    assert '(variant (name "4HP") (field (name "Value") (value "CONN")))' in j3
    # Still a well-formed board: the footprints parse as before.
    assert [f.ref for f in board.footprints(out)] == ["C17A", "J3"]


@pytest.fixture(scope="module")
def install():
    from kicad_mcp.discovery import find_install
    try:
        return find_install()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"KiCad not installed: {exc}")


@pytest.mark.requires_kicad
def test_edp_variant_reaches_schematic_board_and_bom(install, tmp_path):
    import csv
    import io
    import shutil

    from kicad_mcp import cli
    from kicad_mcp.altium import importer

    src = tmp_path / "src"
    src.mkdir()
    for name in ("eDP_adapter_dvt1.PcbDoc", "it6251_core.SchDoc", "power.SchDoc"):
        shutil.copy(altium_fixture(f"eDP/{name}"), src / name)
    (src / "eDP_adapter_dvt1.PrjPcb").write_text(
        "[Design]\nHierarchyMode=0\n[ProjectVariant1]\nDescription=Lite\nVariationCount=2\n"
        r"Variation1=Designator=C13P|UniqueId=\RKDRJMCU|Kind=1|AlternatePart=" "\n"
        r"Variation2=Designator=R10P|UniqueId=\FBGSHOAI|Kind=0|AlternatePart=Res 12k" "\n"
        "ParamVariationCount=0\n", encoding="latin-1")
    result = importer.import_altium(install, str(src))
    assert "error" not in result, result
    assert result["aligned"] is True, result["parity"]
    (lite,) = result["variants"]
    assert (lite["name"], lite["not_fitted"], lite["alternate_parts"], lite["unmatched"]) == ("Lite", 1, 1, [])

    out = importer.Path(result["output_dir"])
    root = out / "eDP_adapter_dvt1.kicad_sch"
    (summary,) = cli.variant_summary(root)["variants"]
    assert summary["not_fitted"] == ["C13P"]
    assert summary["changed_fields"] == [{"reference": "R10P", "field": "Value", "value": "Res 12k"}]

    pcb = (out / "eDP_adapter_dvt1.kicad_pcb").read_text(encoding="utf-8")
    assert '(variant (name "Lite"))' in pcb
    c13p = pcb[pcb.index('"Reference" "C13P"'):]
    assert '(variant (name "Lite") (dnp yes)' in c13p[:c13p.index("\n\t)")]

    def bom(variant):
        path = out / f"bom-{variant}.csv"
        assert cli.export_bom(install, root, path, variant=variant)["returncode"] == 0
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
        return {ref: r for r in rows for ref in r["Refs"].split(",")}

    default, lite = bom(None), bom("Lite")
    # Not fitted in Lite, as in Altium: left out of that build's BOM.
    assert "C13P" in default and "C13P" not in lite
    assert lite["R10P"]["Value"] == "Res 12k" and default["R10P"]["Value"] != "Res 12k"
