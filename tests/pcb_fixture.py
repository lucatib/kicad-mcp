"""A small board built with pcbnew, for the file-based PCB tools.

40 x 30 mm outline with a 6 mm round cutout, one footprint (J1) holding an SMD
pad, a round PTH, a slotted PTH and an NPTH mounting hole, silkscreen and
courtyard graphics and a 3D model, plus a track, a via, a zone and a drawing on
User.1. Built per test rather than committed, so it always matches the
installed KiCad's format.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pcbnew = pytest.importorskip("pcbnew")


def mm(v: float) -> int:
    return pcbnew.FromMM(v)


def pt(x: float, y: float):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def _shape(board, parent, kind, layer, width=0.1):
    s = pcbnew.PCB_SHAPE(parent)
    s.SetShape(kind)
    s.SetLayer(layer)
    s.SetWidth(mm(width))
    return s


def _outline(board):
    corners = [(0, 0), (40, 0), (40, 30), (0, 30)]
    for (x1, y1), (x2, y2) in zip(corners, corners[1:] + corners[:1]):
        seg = _shape(board, board, pcbnew.SHAPE_T_SEGMENT, pcbnew.Edge_Cuts)
        seg.SetStart(pt(x1, y1))
        seg.SetEnd(pt(x2, y2))
        board.Add(seg)
    hole = _shape(board, board, pcbnew.SHAPE_T_CIRCLE, pcbnew.Edge_Cuts)
    hole.SetCenter(pt(30, 15))
    hole.SetEnd(pt(33, 15))  # radius 3 mm
    board.Add(hole)


def _pad(fp, number, attrib, x, y, size, drill=None, oblong=None, net=None):
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetAttribute(attrib)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE if drill else pcbnew.PAD_SHAPE_RECTANGLE)
    pad.SetSize(pcbnew.VECTOR2I(mm(size[0]), mm(size[1])))
    if attrib == pcbnew.PAD_ATTRIB_SMD:
        pad.SetLayerSet(pcbnew.PAD.SMDMask())
    elif attrib == pcbnew.PAD_ATTRIB_NPTH:
        pad.SetLayerSet(pcbnew.PAD.UnplatedHoleMask())
    else:
        pad.SetLayerSet(pcbnew.PAD.PTHMask())
    if oblong:
        pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
        pad.SetDrillShape(pcbnew.PAD_DRILL_SHAPE_OBLONG)
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(oblong[0]), mm(oblong[1])))
    elif drill:
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(drill), mm(drill)))
    pad.SetFPRelativePosition(pt(x, y))
    if net is not None:
        pad.SetNet(net)
    fp.Add(pad)
    return pad


def _footprint(board, gnd, vcc):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPIDAsString("Test:J1_Mixed")
    fp.SetReference("J1")
    fp.SetValue("CONN")
    board.Add(fp)
    fp.SetPosition(pt(10, 10))

    _pad(fp, "1", pcbnew.PAD_ATTRIB_SMD, -4, 0, (1.5, 1.0), net=vcc)
    _pad(fp, "2", pcbnew.PAD_ATTRIB_PTH, 0, 0, (2.0, 2.0), drill=1.0, net=gnd)
    _pad(fp, "3", pcbnew.PAD_ATTRIB_PTH, 4, 0, (2.0, 3.0), oblong=(1.0, 2.0), net=gnd)
    _pad(fp, "", pcbnew.PAD_ATTRIB_NPTH, 0, 6, (3.2, 3.2), drill=3.2)

    silk = _shape(board, fp, pcbnew.SHAPE_T_RECTANGLE, pcbnew.F_SilkS, 0.12)
    silk.SetStart(pt(4.5, 7))
    silk.SetEnd(pt(15.5, 13))
    fp.Add(silk)
    crtyd = _shape(board, fp, pcbnew.SHAPE_T_RECTANGLE, pcbnew.F_CrtYd, 0.05)
    crtyd.SetStart(pt(5, 8))
    crtyd.SetEnd(pt(15, 18))
    fp.Add(crtyd)

    model = pcbnew.FP_3DMODEL()
    model.m_Filename = "${KICAD10_3DMODEL_DIR}/Connector.3dshapes/J1.step"
    model.m_Offset = pcbnew.VECTOR3D(0.0, 0.0, 1.5)
    model.m_Rotation = pcbnew.VECTOR3D(0.0, 0.0, 90.0)
    fp.Models().push_back(model)
    return fp


def build_board(path: Path) -> Path:
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(2)
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(gnd)
    board.Add(vcc)

    _outline(board)
    _footprint(board, gnd, vcc)

    mech = _shape(board, board, pcbnew.SHAPE_T_SEGMENT, pcbnew.User_1)
    mech.SetStart(pt(2, 2))
    mech.SetEnd(pt(8, 2))
    board.Add(mech)

    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pt(10, 10))
    track.SetEnd(pt(20, 10))
    track.SetWidth(mm(0.25))
    track.SetLayer(pcbnew.F_Cu)
    track.SetNet(gnd)
    board.Add(track)

    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pt(20, 10))
    via.SetWidth(mm(0.6))
    via.SetDrill(mm(0.3))
    via.SetNet(gnd)
    board.Add(via)

    zone = pcbnew.ZONE(board)
    zone.SetLayer(pcbnew.B_Cu)
    zone.SetNet(gnd)
    zone.SetZoneName("GND_POUR")
    poly = zone.Outline()
    poly.NewOutline()
    for x, y in [(1, 1), (39, 1), (39, 29), (1, 29)]:
        poly.Append(mm(x), mm(y))
    board.Add(zone)

    pcbnew.SaveBoard(str(path), board)
    return path


@pytest.fixture
def pcb_file(tmp_path) -> Path:
    return build_board(tmp_path / "fixture.kicad_pcb")
