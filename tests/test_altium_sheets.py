"""Converted sheets: KiCad loads them, and wiring, ports and notes survive.

kicad-cli is the judge: a sheet it cannot netlist is not converted, and the
netlist it produces is what the board will be compared against.
"""

from __future__ import annotations

import pytest

from altium_fixtures import altium_fixture, altium_set
from kicad_mcp import cli
from kicad_mcp.altium import design as dz
from kicad_mcp.altium import model, sheets
from kicad_mcp.discovery import find_install

pytestmark = pytest.mark.requires_kicad


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"KiCad not installed: {exc}")


def _write(prefix, name, tmp_path, install):
    loaded = [model.load_sheet(p) for p in altium_set(prefix) if p.suffix.lower() == ".schdoc"]
    d = dz.build_design(loaded, name, None, None)
    refs = dz.instance_references(d, {})
    out = sheets.write_sheets(d, tmp_path, refs, {}, cli.schematic_format_version(install))
    return d, out


def _nets(install, root, tmp_path):
    xml = tmp_path / "n.xml"
    res = cli.export_netlist(install, root, xml)
    assert res["returncode"] == 0, res
    return cli.parse_kicadxml_netlist(xml)


@pytest.fixture(scope="module")
def edp(install, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("edp")
    d, out = _write("eDP", "eDP", tmp, install)
    return tmp, d, out


@pytest.fixture(scope="module")
def sampler(install, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sampler")
    d, out = _write("sampler", "Sampler", tmp, install)
    return tmp, d, out


def test_edp_sheets_load(install, edp):
    tmp, d, out = edp
    names = sorted(p.split("\\")[-1].split("/")[-1] for p in out["files"])
    assert names == ["eDP.kicad_sch", "it6251_core.kicad_sch", "power.kicad_sch"]
    netlist = _nets(install, tmp / "eDP.kicad_sch", tmp)
    refs = {c["reference"] for c in netlist["components"]}
    assert {"C13P", "U10C", "C10C"} <= refs


def test_power_ports_are_power_symbols(install, edp):
    tmp, _, _ = edp
    text = (tmp / "power.kicad_sch").read_text(encoding="utf-8")
    assert "(power)" in text and '"GND"' in text
    netlist = _nets(install, tmp / "eDP.kicad_sch", tmp)
    assert "GND" in {n["name"] for n in netlist["nets"]}


def test_notes_survive(sampler):
    tmp, d, _ = sampler
    root = d.root.sheet
    text = (tmp / "Sampler.kicad_sch").read_text(encoding="utf-8")
    for g in root.graphics:
        if g.kind in ("textframe", "note", "label"):
            wanted = g.rec.text("TEXT").replace("~1", "\n")
            escaped = wanted.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            assert escaped in text, wanted[:60]


def test_ports_become_hierarchical_labels(sampler):
    tmp, d, _ = sampler
    text = (tmp / "Input_channel.kicad_sch").read_text(encoding="utf-8")
    (ch,) = [c for c in d.root.children if c.sheet_symbol.uid == "TJKXBNXR"]
    for entry in ch.sheet_symbol.entries:
        assert f'(hierarchical_label "{entry.name}"' in text


def test_channel_sheet_written_once_with_eight_instances(install, sampler):
    tmp, _, out = sampler
    assert sum(1 for f in out["files"] if f.endswith("Input_channel.kicad_sch")) == 1
    netlist = _nets(install, tmp / "Sampler.kicad_sch", tmp)
    refs = {c["reference"] for c in netlist["components"]}
    assert {f"C10{x}" for x in "ABCDEFGH"} <= refs


def test_not_converted_reported(sampler):
    _, _, out = sampler
    kinds = {n["record"] for n in out["not_converted"]}
    assert "PARAMETER_SET" in kinds
