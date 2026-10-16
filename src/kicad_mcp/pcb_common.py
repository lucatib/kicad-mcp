"""What the live and file-based PCB readers share: layer matching and filters."""

from __future__ import annotations

import fnmatch

#: KiCad's short layer names, as files and older docs spell them, mapped to
#: the names pcbnew and the IPC API report.
LEGACY_LAYER_NAMES = {
    "f.silks": "f.silkscreen",
    "b.silks": "b.silkscreen",
    "f.crtyd": "f.courtyard",
    "b.crtyd": "b.courtyard",
    "f.adhes": "f.adhesive",
    "b.adhes": "b.adhesive",
    "dwgs.user": "user.drawings",
    "cmts.user": "user.comments",
    "eco1.user": "user.eco1",
    "eco2.user": "user.eco2",
}


def layer_matches(name: str, pattern: str | None) -> bool:
    """Glob-match a layer name, accepting the short names too (F.SilkS)."""
    if not pattern:
        return True
    name = str(name).lower()
    pattern = pattern.lower()
    candidates = {pattern}
    for short, full in LEGACY_LAYER_NAMES.items():
        if short in pattern:
            candidates.add(pattern.replace(short, full))
    # '*.silks' names no layer literally; expand the suffix the same way.
    for short, full in LEGACY_LAYER_NAMES.items():
        suffix = short.split(".", 1)[1]
        if pattern.endswith("." + suffix):
            candidates.add(pattern[: -len(suffix)] + full.split(".", 1)[1])
    return any(fnmatch.fnmatch(name, c) for c in candidates)


#: Shapes with two free ends; circles, rectangles and polygons close themselves.
OPEN_SHAPES = ("Segment", "Arc", "Bezier")


def dangling_ends(rows: list[dict], epsilon_mm: float) -> list[dict]:
    """End points that meet no other shape's end within `epsilon_mm`.

    An outline is a chain of open shapes joined end to end, so in a closed
    outline every end has a partner. The ones without are where it is open.
    Points are bucketed on an epsilon grid so a dense outline stays linear.
    """
    ends = []
    for i, r in enumerate(rows):
        if r["type"] in OPEN_SHAPES:
            ends.append((i, r["start"]))
            ends.append((i, r["end"]))

    cell = max(epsilon_mm, 1e-9)
    grid: dict[tuple[int, int], list[int]] = {}
    for k, (_, p) in enumerate(ends):
        grid.setdefault((int(p["x"] // cell), int(p["y"] // cell)), []).append(k)

    def has_partner(k: int) -> bool:
        owner, p = ends[k]
        cx, cy = int(p["x"] // cell), int(p["y"] // cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), ()):
                    if j == k or ends[j][0] == owner:
                        continue
                    q = ends[j][1]
                    if (p["x"] - q["x"]) ** 2 + (p["y"] - q["y"]) ** 2 <= epsilon_mm ** 2:
                        return True
        return False

    return [ends[k][1] for k in range(len(ends)) if not has_partner(k)]


def filter_shape_type(rows: list[dict], shape_type: str | None) -> list[dict]:
    if not shape_type:
        return rows
    return [r for r in rows if fnmatch.fnmatch(r["type"].lower(), shape_type.lower())]
