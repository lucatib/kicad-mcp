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


def filter_shapes(rows: list[dict], layer: str | None, shape_type: str | None) -> list[dict]:
    return [
        r for r in rows
        if layer_matches(r["layer"], layer)
        and (not shape_type or fnmatch.fnmatch(r["type"].lower(), shape_type.lower()))
    ]
