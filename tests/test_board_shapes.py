"""list_board_shapes: the board's graphic shapes, filtered by layer and type.

get_board_summary counted shapes, but nothing listed them, so finding the
Edge.Cuts outline, a milling path on User.1 or a cutout inside a footprint
took a raw script. Shapes come from the live board or, with `path`, the file;
footprint graphics are included on request.
"""

from __future__ import annotations

import pytest

from kicad_mcp import pcb_common, server
from pcb_fixture import pcb_file  # noqa: F401 - fixture


@pytest.mark.requires_kicad
def test_edge_cuts_shapes_from_file(pcb_file):
    out = server.list_board_shapes(layer="Edge.Cuts", path=str(pcb_file))

    kinds = sorted(s["type"] for s in out["items"])
    assert kinds == ["Circle", "Segment", "Segment", "Segment", "Segment"]
    circle = next(s for s in out["items"] if s["type"] == "Circle")
    assert circle["center"] == {"x": 30.0, "y": 15.0}
    assert circle["radius_mm"] == 3.0
    assert circle["layer"] == "Edge.Cuts"
    assert circle["uuid"]
    seg = next(s for s in out["items"] if s["type"] == "Segment")
    assert seg["width_mm"] == 0.1
    assert {"start", "end"} <= set(seg)


@pytest.mark.requires_kicad
def test_shape_type_filter(pcb_file):
    out = server.list_board_shapes(shape_type="circle", path=str(pcb_file))

    assert [s["type"] for s in out["items"]] == ["Circle"]


@pytest.mark.requires_kicad
def test_user_layer_drawing(pcb_file):
    out = server.list_board_shapes(layer="User.1", path=str(pcb_file))

    (seg,) = out["items"]
    assert seg["start"] == {"x": 2.0, "y": 2.0}
    assert seg["end"] == {"x": 8.0, "y": 2.0}


@pytest.mark.requires_kicad
def test_footprint_graphics_only_on_request(pcb_file):
    plain = server.list_board_shapes(layer="F.SilkS", path=str(pcb_file))
    withfp = server.list_board_shapes(layer="F.SilkS", include_footprints=True,
                                      path=str(pcb_file))

    assert plain["count"] == 0
    (rect,) = withfp["items"]
    assert rect["type"] == "Rectangle"
    assert rect["footprint"] == "J1"
    assert rect["top_left"] == {"x": 4.5, "y": 7.0}
    assert rect["bottom_right"] == {"x": 15.5, "y": 13.0}


@pytest.mark.requires_kicad
def test_a_renamed_layer_matches_by_either_name(pcb_file):
    from pcb_fixture import pcbnew

    board = pcbnew.LoadBoard(str(pcb_file))
    board.SetLayerName(pcbnew.User_1, "Mechanical 1")
    pcbnew.SaveBoard(str(pcb_file), board)

    by_canonical = server.list_board_shapes(layer="User.1", path=str(pcb_file))
    by_custom = server.list_board_shapes(layer="Mechanical*", path=str(pcb_file))

    assert by_canonical["count"] == by_custom["count"] == 1
    assert by_custom["items"][0]["layer"] == "Mechanical 1"


def test_legacy_layer_names_match_canonical_ones():
    assert pcb_common.layer_matches("F.Silkscreen", "F.SilkS")
    assert pcb_common.layer_matches("F.Courtyard", "f.crtyd")
    assert pcb_common.layer_matches("User.Drawings", "Dwgs.User")
    assert pcb_common.layer_matches("Edge.Cuts", "edge.cuts")
    assert pcb_common.layer_matches("B.Silkscreen", "*.SilkS")
    assert not pcb_common.layer_matches("F.Silkscreen", "B.SilkS")


def test_live_shapes_from_kipy_objects():
    from kipy.board_types import BoardCircle, BoardSegment
    from kipy.geometry import Vector2
    from kipy.proto.board.board_types_pb2 import BoardLayer

    seg = BoardSegment()
    seg.layer = BoardLayer.BL_Edge_Cuts
    seg.start = Vector2.from_xy(0, 0)
    seg.end = Vector2.from_xy(40_000_000, 0)
    seg.attributes.stroke.width = 100_000
    circle = BoardCircle()
    circle.layer = BoardLayer.BL_User_1
    circle.center = Vector2.from_xy(30_000_000, 15_000_000)
    circle.radius_point = Vector2.from_xy(33_000_000, 15_000_000)

    class Board:
        def get_shapes(self):
            return [seg, circle]

        def get_footprints(self):
            return []

        def get_layer_name(self, layer):
            return {BoardLayer.BL_Edge_Cuts: "Edge.Cuts", BoardLayer.BL_User_1: "User.1"}[layer]

    server_board = Board()
    from kicad_mcp import pcb_live

    rows = pcb_live.shapes(server_board, include_footprints=False)

    assert rows[0] == {
        "uuid": "", "layer": "Edge.Cuts", "type": "Segment", "width_mm": 0.1,
        "filled": False, "start": {"x": 0.0, "y": 0.0}, "end": {"x": 40.0, "y": 0.0},
    }
    assert rows[1]["type"] == "Circle"
    assert rows[1]["radius_mm"] == 3.0
