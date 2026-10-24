"""Altium project variants, read from the .PrjPcb and mapped onto KiCad's.

An Altium variant is a list of variations, one per changed part, naming it
by designator and by the same UniqueId path the board uses: Kind=1 is "not
fitted", Kind=0 an alternate part (other library reference, description or
footprint). KiCad 10 records the same per symbol instance and per footprint,
so each variation lands on exactly the instance its path names -- a channel
copy included -- and on that instance's footprint.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import sexpr
from ..sexpr import make

#: Alternate-part keys -> the KiCad field they change (as KiCad's own importer maps them).
ALTERNATE_FIELDS = {"libreference": "Value", "description": "Description", "footprint": "Footprint"}


@dataclass
class Variation:
    designator: str
    id_path: tuple[str, ...]
    kind: int
    alternate: dict[str, str] = field(default_factory=dict)


@dataclass
class Variant:
    name: str
    description: str
    variations: list[Variation] = field(default_factory=list)
    param_variations: int = 0


@dataclass
class KicadChange:
    """What one variant changes on one KiCad symbol instance or footprint."""
    variant: str
    dnp: bool = False
    fields: dict[str, str] = field(default_factory=dict)

    def symbol_node(self) -> sexpr.SExpr:
        node = make("variant", make("name", self.variant))
        if self.dnp:
            node += [make("dnp", True), make("in_bom", False), make("in_pos_files", False)]
        for name, value in self.fields.items():
            node.append(make("field", make("name", name), make("value", value)))
        return node


def _sections(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = out.setdefault(line[1:-1], {})
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip().lower()] = value
    return out


def _variation(value: str) -> Variation | None:
    fields: dict[str, str] = {}
    alternate: dict[str, str] = {}
    in_alternate = False
    for token in value.split("|"):
        if "=" not in token:
            continue
        key, val = (s.strip() for s in token.split("=", 1))
        if key.lower() == "alternatepart":
            in_alternate = True
            if val:
                alternate["LibReference"] = val
            continue
        if in_alternate:
            if val:
                alternate[key] = val
        else:
            fields[key.lower()] = val
    designator = fields.get("designator", "")
    if not designator:
        return None
    try:
        kind = int(fields.get("kind", "0"))
    except ValueError:
        kind = -1
    ids = tuple(s for s in fields.get("uniqueid", "").split("\\") if s)
    return Variation(designator=designator, id_path=ids, kind=kind, alternate=alternate)


def read_variants(prjpcb: Path | None) -> list[Variant]:
    """The [ProjectVariantN] sections of a .PrjPcb, in file order."""
    if prjpcb is None:
        return []
    sections = _sections(Path(prjpcb).read_text(encoding="latin-1", errors="replace"))
    out = []
    for name, sec in sections.items():
        if not name.startswith("ProjectVariant") or not name[14:].isdigit():
            continue
        variant = Variant(name=sec.get("description", "").strip() or name,
                          description=sec.get("description", "").strip())
        try:
            count = int(sec.get("variationcount", "0"))
            params = int(sec.get("paramvariationcount", "0"))
        except ValueError:
            count, params = 0, 0
        for i in range(1, count + 1):
            v = _variation(sec.get(f"variation{i}", ""))
            if v is not None:
                variant.variations.append(v)
        variant.param_variations = params
        out.append(variant)
    return out


def kicad_change(variant: str, v: Variation) -> KicadChange | None:
    """KiCad's reading of one variation; None for kinds KiCad has no counterpart for."""
    if v.kind == 1:
        return KicadChange(variant, dnp=True)
    if v.kind == 0:
        fields = {ALTERNATE_FIELDS[k.lower()]: val for k, val in v.alternate.items()
                  if k.lower() in ALTERNATE_FIELDS}
        return KicadChange(variant, fields=fields)
    return None


def plan(design, refs: dict[tuple[str, str], str], variants: list[Variant]):
    """Where each variation lands.

    Returns the changes per symbol instance ((sheet instance path, part uid)),
    per footprint reference, and a report: per variant, how many parts it
    leaves out or swaps, and the variations that match no part.
    """
    instance_changes: dict[tuple[str, str], list[KicadChange]] = {}
    footprint_changes: dict[str, list[KicadChange]] = {}
    report = []
    by_ref: dict[str, list[tuple[str, str]]] = {}
    for key, ref in refs.items():
        by_ref.setdefault(ref, []).append(key)

    for variant in variants:
        entry = {"name": variant.name, "description": variant.description,
                 "not_fitted": 0, "alternate_parts": 0, "unmatched": [], "unsupported": []}
        if variant.param_variations:
            entry["unsupported"].append(f"{variant.param_variations} parameter variations")
        for v in variant.variations:
            change = kicad_change(variant.name, v)
            if change is None:
                entry["unsupported"].append(f"{v.designator}: variation kind {v.kind}")
                continue
            keys = _instances(design, refs, by_ref, v)
            if not keys:
                entry["unmatched"].append(v.designator)
                continue
            for key in keys:
                instance_changes.setdefault(key, []).append(change)
            for ref in {refs.get(k) for k in keys} - {None}:
                footprint_changes.setdefault(ref, []).append(change)
            entry["not_fitted" if change.dnp else "alternate_parts"] += 1
        report.append(entry)
    return instance_changes, footprint_changes, report


def _instances(design, refs, by_ref, v: Variation) -> list[tuple[str, str]]:
    """The symbol instances a variation names: by UniqueId path, else by designator.

    Every unit of a multi-part component takes the change, since KiCad keeps
    fitting per symbol.
    """
    key = v.id_path[-1:] if design.flat else v.id_path
    found = design.part_paths.get(tuple(key))
    if found is not None:
        node, part = found
        units = [u for u in node.sheet.parts
                 if u is part or (part.part_count > 1 and u.designator == part.designator
                                  and u.libref == part.libref)]
        return [(node.kicad_path, u.uid) for u in units]
    return list(by_ref.get(v.designator, []))
