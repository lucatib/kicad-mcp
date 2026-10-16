"""The PCB read tools answer from a .kicad_pcb file when given `path`.

kicad_status reports headless_pcb, yet every PCB tool needed the live editor,
so offline inspection meant raw pcbnew scripts. With `path` the same tools
read the saved file through pcbnew, and return the same shapes as live.
"""

from __future__ import annotations

import pytest

from kicad_mcp import server
from kicad_mcp.errors import NotConnectedError
from pcb_fixture import pcb_file  # noqa: F401 - fixture

pytestmark = pytest.mark.requires_kicad


@pytest.fixture(autouse=True)
def kicad_closed(monkeypatch):
    """A file read must not need KiCad at all."""

    def refuse(*_args, **_kwargs):
        raise NotConnectedError("Could not reach KiCad: connection refused")

    monkeypatch.setattr(server.SESSION, "connect", refuse)
    monkeypatch.setattr(server.SESSION, "board", refuse)


def test_board_summary_from_file(pcb_file):
    out = server.get_board_summary(path=str(pcb_file))

    assert out["name"] == "fixture.kicad_pcb"
    assert out["copper_layers"] == 2
    assert out["counts"] == {
        "footprints": 1, "tracks": 1, "vias": 1, "zones": 1, "pads": 4,
        "shapes": 6, "text": 0, "nets": 2,
    }


def test_without_path_the_live_error_still_names_its_remedy():
    out = server.get_board_summary()

    assert out["error"] == "NotConnectedError"
    assert out["remedy"]


def test_list_footprints_from_file(pcb_file):
    out = server.list_footprints(reference="J*", path=str(pcb_file))

    (fp,) = out["items"]
    assert fp["reference"] == "J1"
    assert fp["value"] == "CONN"
    assert fp["library_id"] == "Test:J1_Mixed"
    assert fp["position"] == {"x": 10.0, "y": 10.0}
    assert fp["layer"] == "F.Cu"


def test_get_footprint_from_file_lists_pads_with_nets(pcb_file):
    out = server.get_footprint("j1", path=str(pcb_file))

    assert out["pad_count"] == 4
    nets = {p["number"]: p["net"] for p in out["pads"]}
    assert nets["1"] == "VCC"
    assert nets["2"] == "GND"


def test_get_footprint_unknown_reference_from_file(pcb_file):
    out = server.get_footprint("U99", path=str(pcb_file))

    assert out["error"] == "NotFound"


def test_list_nets_from_file(pcb_file):
    out = server.list_nets(path=str(pcb_file))

    assert [n["name"] for n in out["items"]] == ["GND", "VCC"]


def test_get_layers_from_file(pcb_file):
    out = server.get_layers(path=str(pcb_file))

    names = [layer["name"] for layer in out["layers"]]
    assert out["copper_layer_count"] == 2
    assert {"F.Cu", "B.Cu", "Edge.Cuts", "User.1"} <= set(names)


def test_get_stackup_from_file(pcb_file):
    text = pcb_file.read_text(encoding="utf-8").replace(
        "(setup",
        '(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035))'
        ' (layer "dielectric 1" (type "core") (thickness 1.51) (material "FR4")'
        ' (epsilon_r 4.5) (loss_tangent 0.02))'
        ' (layer "B.Cu" (type "copper") (thickness 0.035)))',
        1,
    )
    pcb_file.write_text(text, encoding="utf-8")

    out = server.get_stackup(path=str(pcb_file))

    assert out["layer_count"] == 3
    core = out["layers"][1]
    assert core["name"] == "dielectric 1"
    assert core["material"] == "FR4"
    assert core["thickness_mm"] == 1.51
    assert core["dielectric"] == {"epsilon_r": 4.5, "loss_tangent": 0.02}


def test_get_stackup_from_a_file_without_one_says_so(pcb_file):
    out = server.get_stackup(path=str(pcb_file))

    assert out["layer_count"] == 0
    assert "default" in out["note"]


def test_list_tracks_vias_from_file(pcb_file):
    out = server.list_tracks_vias(net="GND", path=str(pcb_file))

    kinds = {i["kind"]: i for i in out["items"]}
    assert kinds["Track"]["start"] == {"x": 10.0, "y": 10.0}
    assert kinds["Track"]["width_mm"] == 0.25
    assert kinds["Via"]["position"] == {"x": 20.0, "y": 10.0}


def test_list_zones_from_file(pcb_file):
    out = server.list_zones(path=str(pcb_file))

    assert out["items"] == [{"name": "GND_POUR", "net": "GND", "filled": False}]


def test_a_missing_file_is_an_error_not_an_exception(tmp_path):
    out = server.list_nets(path=str(tmp_path / "nope.kicad_pcb"))

    assert out["error"] == "ToolInputError"


def test_an_edited_file_is_read_again(pcb_file):
    server.list_nets(path=str(pcb_file))
    text = pcb_file.read_text(encoding="utf-8").replace('"VCC"', '"VBUS"')
    pcb_file.write_text(text, encoding="utf-8")

    out = server.list_nets(path=str(pcb_file))

    assert [n["name"] for n in out["items"]] == ["GND", "VBUS"]
