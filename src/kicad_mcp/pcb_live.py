"""Read the board open in KiCad over the IPC API (kipy).

The live counterpart of pcb_file: each function takes a kipy Board and returns
the same dicts pcb_file builds from a saved .kicad_pcb.
"""

from __future__ import annotations

from typing import Any

from . import pcb_common, units


def _ref(fp) -> str:
    return fp.reference_field.text.value if hasattr(fp, "reference_field") else ""


def _val(fp) -> str:
    return fp.value_field.text.value if hasattr(fp, "value_field") else ""


def summary(board) -> dict:
    counts = {
        "footprints": len(board.get_footprints()),
        "tracks": len(board.get_tracks()),
        "vias": len(board.get_vias()),
        "zones": len(board.get_zones()),
        "pads": len(board.get_pads()),
        "shapes": len(board.get_shapes()),
        "text": len(board.get_text()),
        "nets": len(board.get_nets()),
    }
    out: dict[str, Any] = {
        "name": getattr(board, "name", None),
        "copper_layers": board.get_copper_layer_count(),
        "counts": counts,
    }
    try:
        tb = board.get_title_block_info()
        out["title_block"] = {
            "title": tb.title, "date": tb.date, "revision": tb.revision,
            "company": tb.company,
        }
    except Exception:  # noqa: BLE001 - optional metadata
        pass
    try:
        out["origin"] = units.point_to_mm(board.get_origin())
    except Exception:  # noqa: BLE001
        pass
    return out


def _footprint_row(board, fp) -> dict:
    return {
        "reference": _ref(fp),
        "value": _val(fp),
        "library_id": str(getattr(fp, "definition", None) and fp.definition.id or ""),
        "position": units.point_to_mm(fp.position),
        "rotation": units.angle_to_deg(getattr(fp, "orientation", None)),
        "layer": board.get_layer_name(fp.layer) if hasattr(fp, "layer") else None,
    }


def footprints(board) -> list[dict]:
    return [_footprint_row(board, fp) for fp in board.get_footprints()]


_PAD_TYPES = {"PT_SMD": "SMD", "PT_PTH": "PTH", "PT_NPTH": "NPTH",
              "PT_EDGE_CONNECTOR": "Connector"}
_PAD_SHAPES = {"PSS_CIRCLE": "circle", "PSS_RECTANGLE": "rectangle", "PSS_OVAL": "oval",
               "PSS_TRAPEZOID": "trapezoid", "PSS_ROUNDRECT": "roundrect",
               "PSS_CHAMFEREDRECT": "chamfered_rect", "PSS_CUSTOM": "custom"}
_DRILL_SHAPES = {"DS_CIRCLE": "circle", "DS_OBLONG": "oblong"}


def _enum(enum_type, value, names: dict, default: str) -> str:
    try:
        return names.get(enum_type.Name(value), default)
    except ValueError:
        return default


def _size(v) -> dict:
    return {"x": units.nm_to_mm(v.x), "y": units.nm_to_mm(v.y)}


def pad_row(pad) -> dict:
    """One pad as pcb_file.pad_row builds it: type, copper shape, size, drill."""
    from kipy.proto.board import board_types_pb2 as bt  # noqa: PLC0415 - optional dep

    stack = pad.padstack
    copper = stack.copper_layers[0] if stack.copper_layers else None
    diameter = stack.drill.diameter
    return {
        "number": getattr(pad, "number", ""),
        "net": getattr(getattr(pad, "net", None), "name", ""),
        "position": units.point_to_mm(getattr(pad, "position", None)),
        "type": _enum(bt.PadType, pad.pad_type, _PAD_TYPES, "Unknown"),
        "shape": _enum(bt.PadStackShape, copper.shape, _PAD_SHAPES, "unknown") if copper else None,
        "size_mm": _size(copper.size) if copper else None,
        "drill": None if not (diameter.x or diameter.y) else {
            "shape": _enum(bt.DrillShape, stack.drill.shape, _DRILL_SHAPES, "unknown"),
            "size_mm": _size(diameter),
        },
    }


def model_row(model) -> dict:
    """A 3D model.

    The API reuses Vector3D (fields named *_nm) for scale, rotation and offset
    alike and documents scale as a factor and rotation in degrees, i.e. raw
    values; the offset is packed the same way, so it is FP_3DMODEL's mm.
    """
    def xyz(v, conv):
        return {"x": conv(v.x), "y": conv(v.y), "z": conv(v.z)}

    return {
        "filename": model.filename,
        "offset_mm": xyz(model.offset, lambda d: round(d, 6)),
        "rotation_deg": xyz(model.rotation, lambda a: round(a, 4)),
        "scale": xyz(model.scale, lambda s: round(s, 6)),
        "visible": bool(model.visible),
    }


def footprint(board, reference: str, include_shapes: bool = False) -> dict | None:
    for fp in board.get_footprints():
        if _ref(fp).lower() != reference.lower():
            continue
        definition = fp.definition
        pads = [pad_row(pad) for pad in definition.pads]
        out = {**_footprint_row(board, fp), "pad_count": len(pads), "pads": pads[:200],
               "models": [model_row(m) for m in definition.models]}
        if include_shapes:
            grouped: dict[str, list] = {}
            for s in definition.shapes:
                row = shape_row(board, s)
                grouped.setdefault(row["layer"], []).append(row)
            out["shapes"] = grouped
        return out
    return None


def nets(board) -> list[dict]:
    return [{"name": n.name, "code": getattr(n, "code", None)} for n in board.get_nets()]


def layers(board) -> dict:
    enabled = list(board.get_enabled_layers())
    visible = set(board.get_visible_layers())
    active = board.get_active_layer()
    return {
        "copper_layer_count": board.get_copper_layer_count(),
        "active_layer": board.get_layer_name(active),
        "layers": [
            {
                "name": board.get_layer_name(layer),
                "visible": layer in visible,
                "active": layer == active,
            }
            for layer in enabled
        ],
    }


def stackup(board) -> dict:
    stack = board.get_stackup()
    out = []
    for layer in getattr(stack, "layers", []):
        out.append({
            "name": getattr(layer, "user_name", "") or board.get_layer_name(layer.layer),
            "type": str(getattr(layer, "type", "")),
            "material": getattr(layer, "material_name", ""),
            "thickness_mm": units.nm_to_mm(getattr(layer, "thickness", None)),
            "dielectric": getattr(layer, "dielectric", None),
            "enabled": getattr(layer, "enabled", None),
        })
    return {"layer_count": len(out), "layers": out}


def tracks_vias(board) -> list[dict]:
    items: list[dict] = []
    for track in board.get_tracks():
        items.append({
            "kind": type(track).__name__,
            "net": getattr(getattr(track, "net", None), "name", ""),
            "start": units.point_to_mm(getattr(track, "start", None)),
            "end": units.point_to_mm(getattr(track, "end", None)),
            "width_mm": units.nm_to_mm(getattr(track, "width", None)),
        })
    for via in board.get_vias():
        items.append({
            "kind": "Via",
            "net": getattr(getattr(via, "net", None), "name", ""),
            "position": units.point_to_mm(getattr(via, "position", None)),
        })
    return items


#: Points listed per polygon; the count is always given in full.
POLY_POINT_CAP = 200


def _value(v):
    """kipy exposes some geometry as methods (Arc.center(), Circle.radius())."""
    return v() if callable(v) else v


def shape_row(board, shape, footprint: str | None = None) -> dict:
    """One kipy BoardShape as the dict pcb_file.shape_row builds from pcbnew."""
    kind = type(shape).__name__.removeprefix("Board")
    pt = units.point_to_mm
    row: dict[str, Any] = {
        "uuid": str(shape.id.value),
        "layer": board.get_layer_name(shape.layer),
        "type": kind,
        "width_mm": units.nm_to_mm(shape.attributes.stroke.width),
        "filled": bool(shape.attributes.fill.filled),
    }
    if footprint is not None:
        row["footprint"] = footprint
    if kind == "Segment":
        row.update(start=pt(shape.start), end=pt(shape.end))
    elif kind == "Arc":
        row.update(start=pt(shape.start), mid=pt(shape.mid), end=pt(shape.end),
                   center=pt(_value(shape.center)),
                   radius_mm=units.nm_to_mm(_value(shape.radius)))
    elif kind == "Circle":
        row.update(center=pt(shape.center), radius_mm=units.nm_to_mm(_value(shape.radius)))
    elif kind == "Rectangle":
        row.update(top_left=pt(shape.top_left), bottom_right=pt(shape.bottom_right))
    elif kind == "Polygon":
        points = []
        holes = 0
        for poly in shape.polygons:
            holes += len(poly.holes)
            points.extend(pt(n.point if n.has_point else n.arc.start)
                          for n in poly.outline.nodes)
        row.update(outline_count=len(shape.polygons), hole_count=holes,
                   point_count=len(points), points=points[:POLY_POINT_CAP])
    elif kind == "Bezier":
        row.update(start=pt(shape.start), control1=pt(shape.control1),
                   control2=pt(shape.control2), end=pt(shape.end))
    return row


def shapes(board, include_footprints: bool = False, layer: str | None = None) -> list[dict]:
    """Shapes on layers matching `layer` by the board's name or the standard one."""
    from kipy.util.board_layer import canonical_name  # noqa: PLC0415 - optional dep

    names: dict[int, bool] = {}

    def wanted(shape) -> bool:
        if shape.layer not in names:
            names[shape.layer] = (
                pcb_common.layer_matches(board.get_layer_name(shape.layer), layer)
                or pcb_common.layer_matches(canonical_name(shape.layer), layer)
            )
        return names[shape.layer]

    rows = [shape_row(board, s) for s in board.get_shapes() if wanted(s)]
    if include_footprints:
        for fp in board.get_footprints():
            ref = _ref(fp)
            rows.extend(shape_row(board, s, ref) for s in fp.definition.shapes if wanted(s))
    return rows


def zones(board) -> list[dict]:
    return [
        {
            "name": getattr(zone, "name", ""),
            "net": getattr(getattr(zone, "net", None), "name", ""),
            "filled": getattr(zone, "filled", None),
        }
        for zone in board.get_zones()
    ]


# --- edits -----------------------------------------------------------------
# The editing counterparts of board_edit's file functions, for the board open
# in KiCad: each call is one undo step there.


def _commit(board, message: str, change) -> None:
    commit = board.begin_commit()
    try:
        change()
    except BaseException:
        board.drop_commit(commit)
        raise
    board.push_commit(commit, message)


def _layer(name: str):
    from kipy.proto.board.board_types_pb2 import BoardLayer
    return BoardLayer.Value("BL_" + name.replace(".", "_"))


def file_path(board) -> str | None:
    """Where the open board is saved, or None when that cannot be told."""
    try:
        import os
        return os.path.join(board.get_project().path, board.name)
    except Exception:  # noqa: BLE001 - older KiCad without project info
        return None


def apply_route(board, removed: list[str], tracks: list[tuple], vias: list[tuple]) -> None:
    """Take up the tracks with ids in `removed`, lay `tracks` (net, layer name,
    x1, y1, x2, y2, width) and `vias` (net, x, y, diameter, drill), in mm."""
    from kipy.board_types import Track, Via
    from kipy.geometry import Vector2

    nets = {n.name: n for n in board.get_nets()}
    gone = set(removed)
    old = [t for t in list(board.get_tracks()) + list(board.get_vias()) if t.id.value in gone]
    new = []
    for net, layer, x1, y1, x2, y2, width in tracks:
        t = Track()
        t.start, t.end = Vector2.from_xy_mm(x1, y1), Vector2.from_xy_mm(x2, y2)
        t.width = units.mm_to_nm(width)
        t.layer = _layer(layer)
        t.net = nets[net]
        new.append(t)
    for net, x, y, diameter, drill in vias:
        v = Via()
        v.position = Vector2.from_xy_mm(x, y)
        v.diameter, v.drill_diameter = units.mm_to_nm(diameter), units.mm_to_nm(drill)
        v.net = nets[net]
        new.append(v)

    def change():
        if old:
            board.remove_items(old)
        if new:
            board.create_items(new)
    _commit(board, "Autoroute", change)


def unroute(board, nets: list[str] | None = None) -> int:
    """Remove the tracks and vias of the nets matching `nets`, or all; returns how many."""
    import fnmatch
    items = [t for t in list(board.get_tracks()) + list(board.get_vias())
             if nets is None or any(fnmatch.fnmatchcase(t.net.name, n) for n in nets)]
    if items:
        _commit(board, "Unroute", lambda: board.remove_items(items))
    return len(items)


def _pad_points(fp) -> dict[str, tuple[int, int, str]]:
    return {f"{i}:{p.number}": (p.position.x, p.position.y, p.net.name)
            for i, p in enumerate(fp.definition.pads)}


def place(board, placements: list[dict], drag_tracks: bool = False) -> tuple[list[dict], int]:
    """Move, turn and flip footprints: see board_edit.place_footprints. Returns the
    rows and how many tracks were dragged."""
    from kipy.geometry import Angle, Vector2
    from kipy.proto.board.board_types_pb2 import BoardLayer

    found = {_ref(fp): fp for fp in board.get_footprints()}
    rows, moves = [], []

    def change():
        for spec in placements:
            ref = spec["reference"]
            fp = found.get(ref) or next((f for r, f in found.items() if r.lower() == str(ref).lower()), None)
            if fp is None:
                raise ValueError(f"No footprint {ref!r} on the board.")
            before = _pad_points(fp)
            side = spec.get("side")
            if side is not None and (side == "back") != (fp.layer == BoardLayer.BL_B_Cu):
                fp = board.flip_items([fp])[0]
            if "angle" in spec:
                fp.orientation = Angle.from_degrees(float(spec["angle"]))
            if "x" in spec:
                fp.position = Vector2.from_xy_mm(float(spec["x"]), float(spec["y"]))
            fp = board.update_items([fp])[0]
            moves.append((before, _pad_points(fp)))
            rows.append({"reference": _ref(fp), **units.point_to_mm(fp.position),
                         "angle": units.angle_to_deg(fp.orientation),
                         "side": "back" if fp.layer == BoardLayer.BL_B_Cu else "front"})
        if drag_tracks:
            dragged.append(_drag(board, moves))

    dragged: list[int] = []
    _commit(board, "Place footprints", change)
    return rows, sum(dragged)


def _drag(board, moves) -> int:
    """Bring track ends that sat on a moved pad to where the pad is now."""
    from kipy.geometry import Vector2
    shift = {}
    for before, after in moves:
        for key, (x, y, net) in before.items():
            if key in after:
                shift[(x, y, net)] = after[key][:2]
    changed = []
    for t in board.get_tracks():
        hit = False
        for end in ("start", "end"):
            p = getattr(t, end)
            to = shift.get((p.x, p.y, t.net.name))
            if to is not None and to != (p.x, p.y):
                setattr(t, end, Vector2.from_xy(*to))
                hit = True
        if hit:
            changed.append(t)
    if changed:
        board.update_items(changed)
    return len(changed)
