"""get_footprint shows what a footprint is made of, not only its pad nets.

Slots, NPTH mounting holes, courtyards and 3D models all live inside the
footprint; without them, checking milling or a 3D mismatch took a script.
Every pad now carries its type, copper shape and size, and drill; models and,
on request, the footprint's graphics grouped by layer come with it.
"""

from __future__ import annotations

import pytest

from kicad_mcp import pcb_live, server
from pcb_fixture import pcb_file  # noqa: F401 - fixture


@pytest.mark.requires_kicad
def test_pads_carry_type_shape_size_and_drill(pcb_file):
    out = server.get_footprint("J1", path=str(pcb_file))

    pads = {p["number"]: p for p in out["pads"]}
    assert pads["1"]["type"] == "SMD"
    assert pads["1"]["shape"] == "rectangle"
    assert pads["1"]["size_mm"] == {"x": 1.5, "y": 1.0}
    assert pads["1"]["drill"] is None
    assert pads["2"]["type"] == "PTH"
    assert pads["2"]["drill"] == {"shape": "circle", "size_mm": {"x": 1.0, "y": 1.0}}
    assert pads["3"]["shape"] == "oval"
    assert pads["3"]["drill"] == {"shape": "oblong", "size_mm": {"x": 1.0, "y": 2.0}}
    assert pads[""]["type"] == "NPTH"
    assert pads[""]["drill"]["size_mm"] == {"x": 3.2, "y": 3.2}


@pytest.mark.requires_kicad
def test_3d_models_are_listed(pcb_file):
    out = server.get_footprint("J1", path=str(pcb_file))

    (model,) = out["models"]
    assert model["filename"] == "${KICAD10_3DMODEL_DIR}/Connector.3dshapes/J1.step"
    assert model["offset_mm"] == {"x": 0.0, "y": 0.0, "z": 1.5}
    assert model["rotation_deg"] == {"x": 0.0, "y": 0.0, "z": 90.0}
    assert model["scale"] == {"x": 1.0, "y": 1.0, "z": 1.0}
    assert model["visible"] is True


@pytest.mark.requires_kicad
def test_graphics_grouped_by_layer_only_on_request(pcb_file):
    plain = server.get_footprint("J1", path=str(pcb_file))
    full = server.get_footprint("J1", include_shapes=True, path=str(pcb_file))

    assert "shapes" not in plain
    assert set(full["shapes"]) == {"F.Silkscreen", "F.Courtyard"}
    (crtyd,) = full["shapes"]["F.Courtyard"]
    assert crtyd["type"] == "Rectangle"
    assert crtyd["bottom_right"] == {"x": 15.0, "y": 18.0}


def _kipy_pad(number, pad_type, copper_shape, size, drill=None, drill_shape=None):
    from kipy.board_types import Pad
    from kipy.proto.board import board_types_pb2 as bt

    proto = bt.Pad()
    proto.number = number
    proto.type = pad_type
    layer = proto.pad_stack.copper_layers.add()
    layer.shape = copper_shape
    layer.size.x_nm, layer.size.y_nm = size
    if drill:
        proto.pad_stack.drill.diameter.x_nm, proto.pad_stack.drill.diameter.y_nm = drill
        proto.pad_stack.drill.shape = drill_shape
    return Pad(proto)


def test_live_pad_rows_match_the_file_rows():
    from kipy.proto.board import board_types_pb2 as bt

    smd = pcb_live.pad_row(_kipy_pad("1", bt.PT_SMD, bt.PSS_RECTANGLE, (1_500_000, 1_000_000)))
    slot = pcb_live.pad_row(_kipy_pad("3", bt.PT_PTH, bt.PSS_OVAL, (2_000_000, 3_000_000),
                                      (1_000_000, 2_000_000), bt.DS_OBLONG))

    assert smd["type"] == "SMD"
    assert smd["shape"] == "rectangle"
    assert smd["size_mm"] == {"x": 1.5, "y": 1.0}
    assert smd["drill"] is None
    assert slot["type"] == "PTH"
    assert slot["shape"] == "oval"
    assert slot["drill"] == {"shape": "oblong", "size_mm": {"x": 1.0, "y": 2.0}}


def test_live_model_row():
    from kipy.board_types import Footprint3DModel
    from kipy.proto.board import board_types_pb2 as bt

    proto = bt.Footprint3DModel(filename="J1.step", visible=True)
    proto.offset.z_nm = 1.5  # raw mm, like scale and rotation
    proto.rotation.z_nm = 90
    proto.scale.x_nm = proto.scale.y_nm = proto.scale.z_nm = 1

    row = pcb_live.model_row(Footprint3DModel(proto))

    assert row["filename"] == "J1.step"
    assert row["offset_mm"] == {"x": 0.0, "y": 0.0, "z": 1.5}
    assert row["rotation_deg"]["z"] == 90
    assert row["visible"] is True
