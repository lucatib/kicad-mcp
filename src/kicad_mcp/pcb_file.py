"""Read a saved .kicad_pcb through pcbnew, for the PCB tools' `path` mode.

KiCad 10 still ships the SWIG `pcbnew` module, which loads a board file with no
GUI and no IPC connection. Every function here returns the same dicts as its
live counterpart in pcb_live, so a tool's answer does not depend on where it
came from -- only on whether the file was saved.

pcbnew is not thread-safe and a dense board takes seconds to load, so loads go
through one lock and the last few boards are kept, keyed by the file's mtime
and size: an edited file is read again, an unchanged one is not.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from . import sexpr, units
from .errors import ToolInputError

_LOCK = threading.RLock()
_CACHE: OrderedDict[tuple, object] = OrderedDict()
_CACHE_SIZE = 2


def _pcbnew():
    try:
        import pcbnew  # noqa: PLC0415 - heavy, optional
    except Exception as exc:  # noqa: BLE001
        raise ToolInputError(
            f"pcbnew is not importable, so board files cannot be read: {exc}",
            remedy="Run the server with KiCad's own Python (see the README), "
                   "or open the board in KiCad and omit `path`.",
        ) from exc
    return pcbnew


def require_board_file(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_file():
        raise ToolInputError(f"File not found: {p}",
                             remedy="Pass the full path of a .kicad_pcb file.")
    if p.suffix != ".kicad_pcb":
        raise ToolInputError(f"Expected a .kicad_pcb file, got {p.name}",
                             remedy="Pass a .kicad_pcb file.")
    return p.resolve()


def load(path: str | Path):
    """The pcbnew BOARD for `path`, loaded once per saved version of the file."""
    p = require_board_file(path)
    stat = p.stat()
    key = (str(p), stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        board = _pcbnew().LoadBoard(str(p))
        if board is None:
            raise ToolInputError(f"pcbnew could not load {p.name}.",
                                 remedy="Check that the file opens in KiCad 10.")
        for stale in [k for k in _CACHE if k[0] == key[0]]:
            del _CACHE[stale]
        _CACHE[key] = board
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
        return board


def _mm(v) -> float | None:
    return units.nm_to_mm(v)


# --- board-level reads ----------------------------------------------------


def summary(path: str) -> dict:
    pcbnew = _pcbnew()
    with _LOCK:
        board = load(path)
        drawings = list(board.GetDrawings())
        tracks = list(board.GetTracks())
        out = {
            "name": Path(path).name,
            "copper_layers": board.GetCopperLayerCount(),
            "counts": {
                "footprints": len(board.GetFootprints()),
                "tracks": sum(not isinstance(t, pcbnew.PCB_VIA) for t in tracks),
                "vias": sum(isinstance(t, pcbnew.PCB_VIA) for t in tracks),
                "zones": len(board.Zones()),
                "pads": len(board.GetPads()),
                "shapes": sum(isinstance(d, pcbnew.PCB_SHAPE) for d in drawings),
                "text": sum(isinstance(d, (pcbnew.PCB_TEXT, pcbnew.PCB_TEXTBOX))
                            for d in drawings),
                "nets": len(_net_items(board)),
            },
            "source": "file",
        }
        tb = board.GetTitleBlock()
        out["title_block"] = {
            "title": str(tb.GetTitle()), "date": str(tb.GetDate()),
            "revision": str(tb.GetRevision()), "company": str(tb.GetCompany()),
        }
        out["origin"] = units.point_to_mm(board.GetDesignSettings().GetGridOrigin())
        return out


def _net_items(board) -> list[tuple[str, int]]:
    return sorted(
        (str(name), info.GetNetCode())
        for name, info in board.GetNetsByName().items()
        if str(name)
    )


def nets(path: str) -> list[dict]:
    with _LOCK:
        return [{"name": n, "code": c} for n, c in _net_items(load(path))]


def layers(path: str) -> dict:
    with _LOCK:
        board = load(path)
        return {
            "copper_layer_count": board.GetCopperLayerCount(),
            "active_layer": None,  # a file has no active layer; that is GUI state
            "layers": [
                {
                    "name": board.GetLayerName(layer),
                    "visible": bool(board.IsLayerVisible(layer)),
                    "active": False,
                }
                for layer in board.GetEnabledLayers().Seq()
            ],
        }


def stackup(path: str) -> dict:
    """Read from the file's (setup (stackup ...)) section.

    pcbnew's GetStackupDescriptor() comes back as an unwrapped SWIG pointer in
    KiCad 10, so the stackup is parsed from the S-expression instead.
    """
    p = require_board_file(path)
    root = sexpr.parse(p.read_text(encoding="utf-8"))
    setup = sexpr.child(root, "setup")
    node = sexpr.child(setup, "stackup") if setup else None
    if node is None:
        return {"layer_count": 0, "layers": [],
                "note": "No stackup in the file; KiCad uses its default stackup."}
    out = []
    for layer in sexpr.children(node, "layer"):
        dielectric = {
            k: sexpr.value(layer, k) for k in ("epsilon_r", "loss_tangent")
            if sexpr.value(layer, k) is not None
        }
        out.append({
            "name": sexpr.as_text(layer[1]),
            "type": sexpr.value(layer, "type", ""),
            "material": sexpr.value(layer, "material", ""),
            "thickness_mm": sexpr.value(layer, "thickness"),
            "dielectric": dielectric or None,
            "enabled": True,
        })
    return {"layer_count": len(out), "layers": out}


def tracks_vias(path: str) -> list[dict]:
    pcbnew = _pcbnew()
    with _LOCK:
        items = []
        tracks = list(load(path).GetTracks())
        for t in tracks:
            if isinstance(t, pcbnew.PCB_VIA):
                continue
            items.append({
                "kind": "ArcTrack" if isinstance(t, pcbnew.PCB_ARC) else "Track",
                "net": str(t.GetNetname()),
                "start": units.point_to_mm(t.GetStart()),
                "end": units.point_to_mm(t.GetEnd()),
                "width_mm": _mm(t.GetWidth()),
            })
        for v in tracks:
            if isinstance(v, pcbnew.PCB_VIA):
                items.append({
                    "kind": "Via",
                    "net": str(v.GetNetname()),
                    "position": units.point_to_mm(v.GetPosition()),
                })
        return items


def zones(path: str) -> list[dict]:
    with _LOCK:
        return [
            {"name": str(z.GetZoneName()), "net": str(z.GetNetname()),
             "filled": bool(z.IsFilled())}
            for z in load(path).Zones()
        ]


# --- graphic shapes ------------------------------------------------------

#: Points listed per polygon; the count is always given in full.
POLY_POINT_CAP = 200


def _shape_kinds(pcbnew) -> dict:
    return {
        pcbnew.SHAPE_T_SEGMENT: "Segment",
        pcbnew.SHAPE_T_ARC: "Arc",
        pcbnew.SHAPE_T_CIRCLE: "Circle",
        pcbnew.SHAPE_T_RECTANGLE: "Rectangle",
        pcbnew.SHAPE_T_POLY: "Polygon",
        pcbnew.SHAPE_T_BEZIER: "Bezier",
    }


def shape_row(pcbnew, shape, footprint: str | None = None) -> dict:
    """One PCB_SHAPE as the dict pcb_live.shape_row builds from kipy."""
    kind = _shape_kinds(pcbnew).get(shape.GetShape(), "Unknown")
    row: dict = {
        "uuid": str(shape.m_Uuid.AsString()),
        "layer": str(shape.GetLayerName()),
        "type": kind,
        "width_mm": _mm(shape.GetWidth()),
        "filled": bool(shape.IsSolidFill()),
    }
    if footprint is not None:
        row["footprint"] = footprint
    pt = units.point_to_mm
    if kind == "Segment":
        row.update(start=pt(shape.GetStart()), end=pt(shape.GetEnd()))
    elif kind == "Arc":
        row.update(start=pt(shape.GetStart()), mid=pt(shape.GetArcMid()),
                   end=pt(shape.GetEnd()), center=pt(shape.GetCenter()),
                   radius_mm=_mm(shape.GetRadius()))
    elif kind == "Circle":
        row.update(center=pt(shape.GetCenter()), radius_mm=_mm(shape.GetRadius()))
    elif kind == "Rectangle":
        a, b = shape.GetStart(), shape.GetEnd()
        row.update(top_left={"x": _mm(min(a.x, b.x)), "y": _mm(min(a.y, b.y))},
                   bottom_right={"x": _mm(max(a.x, b.x)), "y": _mm(max(a.y, b.y))})
    elif kind == "Polygon":
        poly = shape.GetPolyShape()
        points = []
        holes = 0
        for i in range(poly.OutlineCount()):
            holes += poly.HoleCount(i)
            outline = poly.Outline(i)
            points.extend(pt(outline.CPoint(j)) for j in range(outline.PointCount()))
        row.update(outline_count=poly.OutlineCount(), hole_count=holes,
                   point_count=len(points), points=points[:POLY_POINT_CAP])
    elif kind == "Bezier":
        row.update(start=pt(shape.GetStart()), control1=pt(shape.GetBezierC1()),
                   control2=pt(shape.GetBezierC2()), end=pt(shape.GetEnd()))
    return row


def shapes(path: str, include_footprints: bool = False) -> list[dict]:
    pcbnew = _pcbnew()
    with _LOCK:
        board = load(path)
        rows = [shape_row(pcbnew, d) for d in board.GetDrawings()
                if isinstance(d, pcbnew.PCB_SHAPE)]
        if include_footprints:
            for fp in board.GetFootprints():
                ref = str(fp.GetReference())
                rows.extend(shape_row(pcbnew, g, ref) for g in fp.GraphicalItems()
                            if isinstance(g, pcbnew.PCB_SHAPE))
        return rows


# --- footprints ----------------------------------------------------------


def _footprint_row(fp) -> dict:
    return {
        "reference": str(fp.GetReference()),
        "value": str(fp.GetValue()),
        "library_id": str(fp.GetFPIDAsString()),
        "position": units.point_to_mm(fp.GetPosition()),
        "rotation": round(fp.GetOrientationDegrees(), 4),
        "layer": str(fp.GetLayerName()),
    }


def footprints(path: str) -> list[dict]:
    with _LOCK:
        return [_footprint_row(fp) for fp in load(path).GetFootprints()]


def footprint(path: str, reference: str) -> dict | None:
    with _LOCK:
        for fp in load(path).GetFootprints():
            if str(fp.GetReference()).lower() != reference.lower():
                continue
            row = _footprint_row(fp)
            pads = [
                {
                    "number": str(pad.GetNumber()),
                    "net": str(pad.GetNetname()),
                    "position": units.point_to_mm(pad.GetPosition()),
                }
                for pad in fp.Pads()
            ]
            return {**row, "pad_count": len(pads), "pads": pads[:200]}
    return None
