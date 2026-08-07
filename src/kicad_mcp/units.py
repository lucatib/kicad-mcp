"""Unit conversion, applied only at the tool boundary.

KiCad's IPC API works in nanometres. Every tool in this server accepts and
returns millimetres, because that is what a person reading a board thinks in and
what a model is most likely to get right. Internal code stays in nanometres.

Silent unit errors are the most likely correctness bug in a project like this,
so conversion lives in exactly one module and nowhere else.
"""

from __future__ import annotations

NM_PER_MM = 1_000_000


def nm_to_mm(nm: float | int | None) -> float | None:
    return None if nm is None else round(nm / NM_PER_MM, 6)


def mm_to_nm(mm: float | int | None) -> int | None:
    return None if mm is None else int(round(mm * NM_PER_MM))


def point_to_mm(pt) -> dict | None:
    """Convert a kipy Vector2/point-like object to {x, y} in millimetres."""
    if pt is None:
        return None
    return {"x": nm_to_mm(pt.x), "y": nm_to_mm(pt.y)}


def angle_to_deg(angle) -> float | None:
    """kipy angles expose `.degrees`; plain numbers are already degrees."""
    if angle is None:
        return None
    if hasattr(angle, "degrees"):
        return round(angle.degrees, 4)
    return round(float(angle), 4)


def box_to_mm(box) -> dict | None:
    """Convert a bounding box to millimetres, including derived size.

    Size is included because callers almost always want it and computing it
    from corners is an easy place to reintroduce a unit mistake.
    """
    if box is None:
        return None
    pos, size = box.pos, box.size
    return {
        "x": nm_to_mm(pos.x),
        "y": nm_to_mm(pos.y),
        "width": nm_to_mm(size.x),
        "height": nm_to_mm(size.y),
    }
