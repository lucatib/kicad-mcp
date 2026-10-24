"""Components of an Altium board, as far as linking them to the schematic needs.

KiCad's own `kicad-cli pcb import` converts the board; this reads only the
Components6 table for what that import throws away: SOURCEUNIQUEID, the path
of UniqueIds from the root sheet to the part.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .records import Record, read_pcbdoc_components

MIL_MM = 0.0254


@dataclass(frozen=True)
class BoardPart:
    source_designator: str
    id_path: tuple[str, ...]
    sheet_path: str
    x_mm: float
    y_mm: float
    rotation: float
    pattern: str
    footprint_library: str
    index: int


def _mil(value: str) -> float:
    text = value.strip().lower().removesuffix("mil")
    try:
        return float(text) * MIL_MM
    except ValueError:
        return 0.0


def _part(row: Record) -> BoardPart:
    ids = tuple(s for s in row.text("SOURCEUNIQUEID").split("\\") if s)
    return BoardPart(source_designator=row.text("SOURCEDESIGNATOR"), id_path=ids,
                     sheet_path=row.text("SOURCEHIERARCHICALPATH"),
                     x_mm=_mil(row.text("X")), y_mm=_mil(row.text("Y")),
                     rotation=row.float("ROTATION"), pattern=row.text("PATTERN"),
                     footprint_library=row.text("SOURCEFOOTPRINTLIBRARY"), index=row.index)


def read_board_parts(path: str | Path) -> list[BoardPart]:
    return [_part(r) for r in read_pcbdoc_components(path)]
