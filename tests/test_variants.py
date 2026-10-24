"""Design variants: exports for one variant, and the list of variants a project has.

KiCad 10 keeps the variant names in the project file and each symbol's
differences (DNP, BOM, fields) inside its instance. kicad-cli renders any
export for a variant with --variant; the tools pass it through, after checking
the name exists, since kicad-cli would otherwise quietly fall back.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_mcp import cli
from kicad_mcp.discovery import find_install
from kicad_mcp.errors import ToolInputError

EXAMPLE = Path(__file__).parent.parent / "examples" / "esp32_s3_pinout.kicad_sch"
INSTALL = SimpleNamespace(cli_path=Path("kicad-cli.exe"))


def _project(tmp_path: Path, variants=(("Lite", "no MCU fitted"),)) -> Path:
    sch = tmp_path / "v.kicad_sch"
    shutil.copy(EXAMPLE, sch)
    pro = {"meta": {"filename": "v.kicad_pro", "version": 3},
           "schematic": {"variants": [{"name": n, "description": d} for n, d in variants]}}
    (tmp_path / "v.kicad_pro").write_text(json.dumps(pro), encoding="utf-8")
    return sch


def test_project_variants_listed(tmp_path):
    sch = _project(tmp_path, (("Lite", "no MCU fitted"), ("Full", "")))
    assert cli.project_variants(sch) == [{"name": "Lite", "description": "no MCU fitted"},
                                         {"name": "Full", "description": ""}]


def test_no_project_means_no_variants(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    shutil.copy(EXAMPLE, sch)
    assert cli.project_variants(sch) == []


def test_unknown_variant_refused_with_the_names_there_are(tmp_path, monkeypatch):
    sch = _project(tmp_path)
    monkeypatch.setattr(cli, "run_cli", lambda *a, **k: pytest.fail("must not run kicad-cli"))
    with pytest.raises(ToolInputError, match="Lite"):
        cli.export_bom(INSTALL, sch, variant="Pro")


@pytest.fixture
def seen(monkeypatch):
    calls = []

    def fake(install, args, timeout=cli.DEFAULT_TIMEOUT):
        calls.append(args)
        return {"command": " ".join(args), "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli, "run_cli", fake)
    return calls


@pytest.mark.parametrize("export", [
    lambda sch: cli.export_bom(INSTALL, sch, variant="Lite"),
    lambda sch: cli.export_netlist(INSTALL, sch, variant="Lite"),
    lambda sch: cli.export_plot(INSTALL, sch, "pdf", variant="Lite"),
])
def test_variant_reaches_kicad_cli(tmp_path, seen, export):
    export(_project(tmp_path))
    args = seen[-1]
    assert args[args.index("--variant") + 1] == "Lite"


def test_no_variant_no_flag(tmp_path, seen):
    cli.export_bom(INSTALL, _project(tmp_path))
    assert "--variant" not in seen[-1]


def test_render_takes_a_variant(tmp_path, seen):
    board = tmp_path / "v.kicad_pcb"
    board.write_text("(kicad_pcb)", encoding="utf-8")
    _project(tmp_path)
    cli.render_pcb(INSTALL, board, output=tmp_path / "r.png", variant="Lite")
    assert seen[-1][seen[-1].index("--variant") + 1] == "Lite"


@pytest.mark.requires_kicad
def test_bom_for_a_variant_marks_its_dnp_parts(tmp_path):
    try:
        install = find_install()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"KiCad not installed: {exc}")
    sch = _project(tmp_path)
    text = sch.read_text(encoding="utf-8")
    # U1 is not fitted in the Lite variant.
    marker = '(reference "U1")\n\t\t\t\t\t(unit 1)'
    assert marker in text
    sch.write_text(text.replace(marker, marker + '\n\t\t\t\t\t(variant (name "Lite") (dnp yes))', 1),
                   encoding="utf-8")

    def dnp_of_u1(variant):
        out = tmp_path / f"bom-{variant}.csv"
        res = cli.export_bom(install, sch, out, variant=variant)
        assert res["returncode"] == 0, res
        rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
        (row,) = [r for r in rows if "U1" in r["Refs"].split(",")]
        return row["DNP"]

    assert dnp_of_u1(None) == ""
    assert dnp_of_u1("Lite") != ""


def _with_lite_variant(tmp_path):
    sch = _project(tmp_path, (("Lite", "no MCU fitted"), ("Full", "")))
    text = sch.read_text(encoding="utf-8")
    marker = '(reference "U1")\n\t\t\t\t\t(unit 1)'
    sch.write_text(text.replace(marker, marker + '\n\t\t\t\t\t(variant (name "Lite") (dnp yes) (in_bom no)'
                                         ' (field (name "Value") (value "-")))', 1), encoding="utf-8")
    return sch


def test_variant_summary_says_what_each_variant_changes(tmp_path):
    out = cli.variant_summary(_with_lite_variant(tmp_path))
    by_name = {v["name"]: v for v in out["variants"]}
    assert by_name["Lite"]["description"] == "no MCU fitted"
    assert by_name["Lite"]["not_fitted"] == ["U1"]
    assert by_name["Lite"]["excluded_from_bom"] == ["U1"]
    assert by_name["Lite"]["changed_fields"] == [{"reference": "U1", "field": "Value", "value": "-"}]
    assert by_name["Full"]["not_fitted"] == [] and by_name["Full"]["changed_fields"] == []
    assert out["default"] == "the design as drawn, used when no variant is given"


def test_list_variants_tool(tmp_path):
    from kicad_mcp import server
    out = server.list_variants(str(_with_lite_variant(tmp_path)))
    assert [v["name"] for v in out["variants"]] == ["Lite", "Full"]


def test_list_variants_without_any(tmp_path):
    from kicad_mcp import server
    out = server.list_variants(str(_project(tmp_path, ())))
    assert out["variants"] == [] and "no variants" in out["note"]


@pytest.mark.requires_kicad
def test_bom_keeps_designators_that_end_in_a_letter(tmp_path):
    # KiCad's BOM shows "U1A?" for a reference ending in a letter (Altium's
    # channel style, C10A); only a really unannotated part keeps its "?".
    try:
        install = find_install()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"KiCad not installed: {exc}")
    sch = _project(tmp_path)
    text = sch.read_text(encoding="utf-8")
    sch.write_text(text.replace('(reference "U1")', '(reference "U1A")', 1)
                   .replace('(property "Reference" "U1"', '(property "Reference" "U1A"', 1), encoding="utf-8")
    out = tmp_path / "b.csv"
    res = cli.export_bom(install, sch, out)
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert [r["Refs"] for r in rows if r["Refs"].startswith("U1")] == ["U1A"]
    assert res["references_fixed"] == 1


@pytest.mark.requires_kicad
def test_bom_keeps_the_question_mark_of_an_unannotated_part(tmp_path):
    # "U?" means not annotated yet, in KiCad as in Altium: it must stay visible.
    try:
        install = find_install()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"KiCad not installed: {exc}")
    sch = _project(tmp_path)
    text = sch.read_text(encoding="utf-8")
    sch.write_text(text.replace('(reference "U1")', '(reference "U?")', 1)
                   .replace('(property "Reference" "U1"', '(property "Reference" "U?"', 1), encoding="utf-8")
    out = tmp_path / "b.csv"
    res = cli.export_bom(install, sch, out)
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert [r["Refs"] for r in rows if r["Refs"].startswith("U")] == ["U?"]
    assert res["references_fixed"] == 0
