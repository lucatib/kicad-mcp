"""import_altium end to end: loose Altium files in, a linked KiCad project out.

The promise is that the schematic and the board agree by construction --
same designators, footprints linked by path -- and that anything the Altium
files themselves disagree on is listed rather than silently fixed.
"""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from altium_fixtures import altium_fixture, altium_set
from kicad_mcp import server
from kicad_mcp.altium import importer
from kicad_mcp.discovery import find_install
from kicad_mcp.errors import KicadNotFoundError


def _copy(prefix, tmp_path, names=None):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for p in altium_set(prefix):
        if names is None or p.name in names:
            shutil.copy(p, src / p.name)
    return src


def _hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}


@pytest.fixture
def no_kicad(monkeypatch):
    """Input refusals must not depend on KiCad being installed."""
    def missing():
        raise KicadNotFoundError("No KiCad installation found.")
    monkeypatch.setattr(server, "install", missing)


def test_no_altium_files_refused(tmp_path, no_kicad):
    (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
    out = server.import_altium(str(tmp_path))
    assert out["error"] == "ToolInputError" and "remedy" in out


def test_two_boards_refused(tmp_path, no_kicad):
    for name in ("a.PcbDoc", "b.PcbDoc", "s.SchDoc"):
        (tmp_path / name).write_bytes(b"x")
    out = server.import_altium(str(tmp_path))
    assert out["error"] == "ToolInputError"
    assert "a.PcbDoc" in out["message"] and "b.PcbDoc" in out["message"]


def test_existing_output_refused(tmp_path, no_kicad):
    src = _copy("eDP", tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine", encoding="utf-8")
    out = server.import_altium(str(src), output_dir=str(dest))
    assert out["error"] == "ToolInputError" and "overwrite" in out["remedy"]
    assert [p.name for p in dest.iterdir()] == ["keep.txt"]


def test_valid_inputs_then_need_kicad(tmp_path, no_kicad):
    # Past the checks, a missing KiCad is reported -- and nothing is written.
    src = _copy("eDP", tmp_path)
    dest = tmp_path / "out"
    out = server.import_altium(str(src), output_dir=str(dest))
    assert out["error"] == "KicadNotFoundError"
    assert not dest.exists()


def test_server_tool_registered():
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "import_altium" in names


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"KiCad not installed: {exc}")


@pytest.fixture(scope="module")
def edp(install, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("edp")
    src = _copy("eDP", tmp, {"eDP_adapter_dvt1.PcbDoc", "it6251_core.SchDoc", "power.SchDoc"})
    before = _hashes(src)
    result = importer.import_altium(install, str(src))
    return src, before, result


@pytest.mark.requires_kicad
def test_edp_end_to_end(edp):
    src, before, result = edp
    assert "error" not in result, result
    report = json.loads((importer.Path(result["output_dir"]) / "import_report.json").read_text(encoding="utf-8"))
    assert report["parity"]["missing"] == 0 and report["parity"]["duplicates"] == 0
    kinds = {(d["kind"], d.get("ref")) for d in report["source_disagreements"]}
    assert ("moved_sheet", "C22P") in kinds
    assert report["aligned"] == (report["net_check"]["topology_differences"] == []
                                 and report["parity"]["unexplained"] == [])
    assert _hashes(src) == before  # inputs untouched
    assert importer.Path(result["output_dir"]) == src / "eDP_adapter_dvt1_kicad"


@pytest.mark.requires_kicad
def test_edp_references_identical_on_both_sides(install, edp):
    _, _, result = edp
    out = importer.Path(result["output_dir"])
    from kicad_mcp.altium import board, verify
    pcb = (out / "eDP_adapter_dvt1.kicad_pcb").read_text(encoding="utf-8")
    board_refs = {fp.ref for fp in board.footprints(pcb) if fp.path}
    sch = verify.schematic_pin_nets(install, out / "eDP_adapter_dvt1.kicad_sch")
    sch_refs = {r for pads in sch.values() for r, _ in pads}
    assert board_refs - {"C22P"} <= sch_refs | board_refs
    assert {r for r in sch_refs if not r.startswith("#")} - board_refs <= {"C22P"}


@pytest.mark.requires_kicad
def test_rerun_refused_then_overwrite(install, edp):
    src, _, result = edp
    with pytest.raises(importer.ToolInputError, match="not empty"):
        importer.import_altium(install, str(src))
    forced = importer.import_altium(install, str(src), overwrite=True)
    assert "error" not in forced and forced["output_dir"] == result["output_dir"]


@pytest.mark.requires_kicad
def test_schematics_only(install, tmp_path):
    src = _copy("qa/issue16903", tmp_path)
    result = importer.import_altium(install, str(src))
    assert "error" not in result, result
    assert result["counts"]["footprints"] == 0 and result["aligned"] is None
    records = {n["record"] for n in result["not_converted"]["items"]}
    assert "HARNESS_PORT" in records


@pytest.mark.requires_kicad
def test_board_only(install, tmp_path):
    src = _copy("eDP", tmp_path, {"eDP_adapter_dvt1.PcbDoc"})
    result = importer.import_altium(install, str(src))
    assert "error" not in result, result
    assert result["aligned"] is None
    assert result["source_disagreements"]["count"] >= 112


@pytest.mark.requires_kicad
@pytest.mark.slow
def test_sampler_end_to_end(install, tmp_path):
    src = _copy("sampler", tmp_path, {"ADC_8CH.SchDoc", "Input_channel.SchDoc", "LVDS_IFCA.SchDoc",
                                      "LVDS_IFCB.SchDoc", "Sampler.PCBDOC", "Sampler.schdoc",
                                      "Supply_Sampler.SchDoc"})
    result = importer.import_altium(install, str(src))
    assert "error" not in result, result
    out = importer.Path(result["output_dir"])
    assert (out / "Input_channel.kicad_sch").is_file()
    report = json.loads((out / "import_report.json").read_text(encoding="utf-8"))
    assert report["parity"]["missing"] == 0 and report["parity"]["extra"] == 0
    assert report["parity"]["duplicates"] == 0


@pytest.mark.requires_kicad
def test_flat_schematics_only_keeps_every_sheet(install, tmp_path):
    # The default project name is a sheet's stem; the root must not overwrite it.
    src = _copy("eDP", tmp_path, {"it6251_core.SchDoc", "power.SchDoc"})
    result = importer.import_altium(install, str(src))
    assert "error" not in result, result
    out = importer.Path(result["output_dir"])
    root = (out / f"{result['project'].rsplit(chr(92), 1)[-1].rsplit('/', 1)[-1][:-10]}.kicad_sch").read_text(encoding="utf-8")
    assert root.count("(sheet\n") == 2
    assert len(list(out.glob("*.kicad_sch"))) == 3
