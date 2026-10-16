"""Read the board open in KiCad over the IPC API (kipy).

The live counterpart of pcb_file: each function takes a kipy Board and returns
the same dicts pcb_file builds from a saved .kicad_pcb.
"""

from __future__ import annotations

from typing import Any

from . import units


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


def footprint(board, reference: str) -> dict | None:
    for fp in board.get_footprints():
        if _ref(fp).lower() != reference.lower():
            continue
        pads = [
            {
                "number": getattr(pad, "number", ""),
                "net": getattr(getattr(pad, "net", None), "name", ""),
                "position": units.point_to_mm(getattr(pad, "position", None)),
            }
            for pad in (fp.definition.pads if hasattr(fp, "definition") else [])
        ]
        return {**_footprint_row(board, fp), "pad_count": len(pads), "pads": pads[:200]}
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


def shapes(board, include_footprints: bool = False) -> list[dict]:
    rows = [shape_row(board, s) for s in board.get_shapes()]
    if include_footprints:
        for fp in board.get_footprints():
            ref = _ref(fp)
            rows.extend(shape_row(board, s, ref) for s in fp.definition.shapes)
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
