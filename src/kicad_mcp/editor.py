"""Modify schematics that already exist.

`generator.py` authors a document from nothing. This module changes one in
place, which is a different problem: existing content must survive untouched,
and new symbols must join the same project and sheet instance as the old ones or
KiCad treats them as unannotated strays.

Edits append rather than rewrite. Anything the user drew by hand between our
edits is preserved, because we only ever add nodes and never regenerate the
document.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from . import sexpr
from .errors import ToolInputError
from .footprints import FootprintIndex, footprint_warning
from .generator import GRID, LABEL_STUB, PlacedSymbol, SchematicBuilder, _normalise_key, _property, _snap
from .schematic import Schematic
from .sexpr import Sym
from .symbols import PinInfo, SymbolIndex

#: Where a two-pin part's pins sit relative to its centre, for Device:C and
#: friends: pin 1 above, pin 2 below, both on the 1.27 mm grid.
STUB = 2.54

#: Landscape paper sizes in mm, under the names `(paper ...)` uses.
PAPER_MM = {
    "A5": (210.0, 148.0), "A4": (297.0, 210.0), "A3": (420.0, 297.0),
    "A2": (594.0, 420.0), "A1": (841.0, 594.0), "A0": (1189.0, 841.0),
    "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
    "D": (863.6, 558.8), "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4),
}
#: From KiCad's pagelayout_default.kicad_wks: 10 mm page margins, then a 2 mm
#: band of border coordinates, then the drawing area.
PAGE_MARGIN = 10.0
PAGE_INSET = 12.0
#: The title block's outer rectangle, measured from the margin's bottom-right
#: corner (the layout's `start 110 34`); its inner 2 mm is the border band.
TITLE_BLOCK = (110.0, 34.0)
#: Clearance kept between a newly placed symbol and anything already drawn.
PLACE_MARGIN = 12.7

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in canvas mm


class SchematicEditor:
    """Loads a `.kicad_sch`, appends content, writes it back."""

    def __init__(self, path: Path, tree: sexpr.SExpr) -> None:
        self.path = Path(path)
        self.tree = tree

    @classmethod
    def load(cls, path: str | Path) -> "SchematicEditor":
        sch = Schematic.load(path)
        return cls(sch.path, sch.tree)

    # --- identity -------------------------------------------------------

    @property
    def root_uuid(self) -> str:
        return str(sexpr.value(self.tree, "uuid", ""))

    @property
    def project_name(self) -> str:
        """The project name recorded on existing symbols.

        New symbols must use the same name; a mismatch makes KiCad treat them as
        belonging to a different project and drop their annotation.
        """
        for symbol in sexpr.children(self.tree, "symbol"):
            instances = sexpr.child(symbol, "instances")
            if instances is None:
                continue
            project = sexpr.child(instances, "project")
            if project is not None and len(project) > 1 and isinstance(project[1], str):
                return project[1]
        return self.path.stem

    def _builder(self) -> SchematicBuilder:
        builder = SchematicBuilder(project_name=self.project_name)
        builder.uuid = self.root_uuid
        return builder

    # --- queries --------------------------------------------------------

    def references(self) -> set[str]:
        refs = set()
        for symbol in sexpr.children(self.tree, "symbol"):
            for prop in sexpr.children(symbol, "property"):
                if len(prop) >= 3 and prop[1] == "Reference":
                    refs.add(str(prop[2]))
        return refs

    def next_reference(self, prefix: str) -> str:
        """First free `prefix<N>`, so repeated edits do not collide."""
        existing = self.references()
        n = 1
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"

    # --- page and placement ---------------------------------------------

    def page_size(self) -> tuple[float, float]:
        """Paper width and height in mm, from `(paper ...)`; A4 if absent."""
        vals = sexpr.values(self.tree, "paper")
        name = sexpr.as_text(vals[0]) if vals else "A4"
        if name == "User" and len(vals) >= 3:
            w, h = float(vals[1]), float(vals[2])
        else:
            w, h = PAPER_MM.get(name, PAPER_MM["A4"])
        if any(sexpr.as_text(v) == "portrait" for v in vals[1:]):
            w, h = h, w
        return w, h

    def drawing_area(self) -> Box:
        w, h = self.page_size()
        return (PAGE_INSET, PAGE_INSET, w - PAGE_INSET, h - PAGE_INSET)

    def title_block(self) -> Box:
        w, h = self.page_size()
        right, bottom = w - PAGE_MARGIN, h - PAGE_MARGIN
        return (right - TITLE_BLOCK[0], bottom - TITLE_BLOCK[1], right - 2, bottom - 2)

    def free_spot(self, index: SymbolIndex, lib_id: str, inflate: float = 0.0) -> tuple[float, float]:
        """An on-page origin for `lib_id` that clears everything already drawn.

        Tries, in order: right of existing content, row by row from the top
        (so parts extend the row they belong with); then under existing
        content, either stacked below a part or starting a new row at the
        left edge, whichever is higher on the sheet; and only then the free
        strip along the top, before calling the sheet full. Each candidate's whole
        box -- body, pins, reference/value text, plus `inflate` for labels or
        rails the caller is about to hang off it -- must fit inside the
        drawing area, off the title block, and clear of every symbol, wire,
        label and sheet by at least half of PLACE_MARGIN.
        """
        rl, rt, rr, rb = _relative_box(index, lib_id)
        rl, rt, rr, rb = rl - inflate, rt - inflate, rr + inflate, rb + inflate
        area = self.drawing_area()
        anchors, blockers = self._obstacles(index)
        blockers.append(self.title_block())
        gap = PLACE_MARGIN / 2

        def fits(x: float, y: float) -> bool:
            box = (x + rl, y + rt, x + rr, y + rb)
            if not _inside(box, area):
                return False
            grown = (box[0] - gap, box[1] - gap, box[2] + gap, box[3] + gap)
            return not any(_intersects(grown, b) for b in blockers)

        candidates: list[tuple[int, float, float]] = []
        if not anchors:
            candidates.append((0, 101.6, 190.5))  # the historical default spot
            candidates.append((1, area[1] + PLACE_MARGIN - rt, area[0] + PLACE_MARGIN - rl))
        for box, anchor_y in anchors:
            candidates.append((0, anchor_y, box[2] + PLACE_MARGIN - rl))
            below = box[3] + PLACE_MARGIN - rt
            candidates.append((1, below, area[0] + PLACE_MARGIN - rl))  # new row, left edge
            candidates.append((1, below, box[0] - rl))  # stacked under this one
            # Last resort, before calling the sheet full: the strip above.
            candidates.append((2, area[1] + PLACE_MARGIN - rt, box[0] - rl))
            candidates.append((2, area[1] + PLACE_MARGIN - rt, area[0] + PLACE_MARGIN - rl))
        for _, y, x in sorted((tier, _snap_up(y), _snap_up(x)) for tier, y, x in candidates):
            if fits(x, y):
                return x, y

        w, h = self.page_size()
        raise ToolInputError(
            f"No room for {lib_id} on the {w:g}x{h:g} mm sheet of {self.path.name}: every "
            "free spot would leave the page, cover the title block, or touch existing content.",
            remedy="Enlarge the sheet in KiCad (File > Page Settings, e.g. A3 or A2), "
                   "or pass explicit x/y coordinates.",
        )

    def placement_warnings(self, index: SymbolIndex, lib_id: str, x: float, y: float) -> list[str]:
        """Why an explicitly chosen position is a bad one, if it is."""
        rl, rt, rr, rb = _relative_box(index, lib_id)
        box = (x + rl, y + rt, x + rr, y + rb)
        out = []
        if not _inside(box, self.drawing_area()):
            w, h = self.page_size()
            out.append(f"{lib_id} at ({x:g}, {y:g}) extends past the {w:g}x{h:g} mm drawing area.")
        if _intersects(box, self.title_block()):
            out.append(f"{lib_id} at ({x:g}, {y:g}) overlaps the title block.")
        return out

    def _obstacles(self, index: SymbolIndex) -> tuple[list[tuple[Box, float]], list[Box]]:
        """(symbol boxes with their origin y, every box that blocks placement)."""
        anchors: list[tuple[Box, float]] = []
        blockers: list[Box] = []
        points_cache: dict[tuple[str, int], list[tuple[float, float]]] = {}
        # The copies embedded in the file are what KiCad actually draws, and
        # they survive the source library being renamed or removed.
        embedded = {
            str(n[1]): n
            for n in sexpr.children(sexpr.child(self.tree, "lib_symbols") or [], "symbol")
            if len(n) > 1
        }

        for symbol in sexpr.children(self.tree, "symbol"):
            lib_id = str(sexpr.value(symbol, "lib_id", ""))
            at = sexpr.values(symbol, "at")
            if not lib_id or not at:
                continue
            unit = int(sexpr.value(symbol, "unit", 1) or 1)
            if (lib_id, unit) not in points_cache:
                definition = embedded.get(lib_id)
                if definition is None:
                    try:
                        definition = index.definition(lib_id)
                    except ToolInputError:
                        definition = None
                points_cache[lib_id, unit] = (
                    _local_points(definition, unit) if definition is not None
                    # Unknown shape: claim a generous square rather than nothing.
                    else [(-PLACE_MARGIN, -PLACE_MARGIN), (PLACE_MARGIN, PLACE_MARGIN)]
                )
            mirror = sexpr.value(symbol, "mirror")
            placed = PlacedSymbol(
                "", "", lib_id, x=float(at[0]), y=float(at[1]),
                angle=float(at[2]) if len(at) > 2 else 0.0, unit=unit, uuid="",
                mirror=str(mirror) if mirror else None,
            )
            pts = [placed.to_canvas(px, py) for px, py in points_cache[lib_id, unit]]
            # Visible fields (reference, value) are part of what a reader sees.
            for prop in sexpr.children(symbol, "property"):
                p_at = sexpr.values(prop, "at")
                if p_at and "(hide yes)" not in sexpr.dumps(prop):
                    pts.append((float(p_at[0]), float(p_at[1])))
            box = _bounds(pts)
            blockers.append(box)
            anchors.append((box, placed.y))

        for wire in sexpr.children(self.tree, "wire"):
            pts_node = sexpr.child(wire, "pts")
            pts = [(float(xy[1]), float(xy[2])) for xy in sexpr.children(pts_node or [], "xy")]
            if pts:
                blockers.append(_grow(_bounds(pts), 0.5))
        for kind in ("label", "global_label", "hierarchical_label", "text"):
            for node in sexpr.children(self.tree, kind):
                at = sexpr.values(node, "at")
                if not at:
                    continue
                text = str(node[1]) if len(node) > 1 else ""
                blockers.append(_text_box(float(at[0]), float(at[1]),
                                          float(at[2]) if len(at) > 2 else 0.0, text))
        for kind in ("junction", "no_connect"):
            for node in sexpr.children(self.tree, kind):
                at = sexpr.values(node, "at")
                if at:
                    blockers.append(_grow(_bounds([(float(at[0]), float(at[1]))]), 1.0))
        for sheet in sexpr.children(self.tree, "sheet"):
            at, size = sexpr.values(sheet, "at"), sexpr.values(sheet, "size")
            if at and size:
                x, y = float(at[0]), float(at[1])
                box = (x, y, x + float(size[0]), y + float(size[1]))
                blockers.append(box)
                anchors.append((box, y))
        return anchors, blockers

    def placement(self, reference: str) -> tuple[str, float, float, float]:
        """The lib_id and (x, y, angle) of an already-placed symbol.

        Every edit that touches an existing symbol -- wiring a new part to one
        of its pins, marking pins unused -- starts by reconstructing this from
        the file, since the symbol was placed by an earlier call (or by hand in
        KiCad) and isn't something the current edit knows about otherwise.
        """
        for symbol in sexpr.children(self.tree, "symbol"):
            ref = next(
                (p[2] for p in sexpr.children(symbol, "property") if p[1] == "Reference"),
                None,
            )
            if ref == reference:
                lib_id = str(sexpr.value(symbol, "lib_id", ""))
                at = sexpr.values(symbol, "at")
                if not lib_id or not at:
                    break
                return lib_id, float(at[0]), float(at[1]), float(at[2]) if len(at) > 2 else 0.0
        raise ToolInputError(
            f"No symbol with reference {reference!r} in {self.path.name}.",
            remedy="Call list_schematic_symbols to see what's placed.",
        )

    # --- edits ----------------------------------------------------------

    def mark_pins_unused(
        self,
        index: SymbolIndex,
        reference: str,
        pin_numbers: list[str] | None = None,
        name_contains: str | None = None,
        annotate: str | None = None,
    ) -> list[dict]:
        """No-connect a set of an existing symbol's pins, optionally labelled.

        Selects by `pin_numbers` (exact) and/or `name_contains` (case-insensitive
        substring on the pin's own name, e.g. "SD_" to catch every SD-bus pin at
        once). `annotate` places that literal text next to every matched pin --
        pass the pin's real function (e.g. "/RES") when the symbol's own pin
        name is generic or the net was never actually wired to anything.
        """
        lib_id, x, y, angle = self.placement(reference)
        placed = PlacedSymbol(reference, "", lib_id, x=x, y=y, angle=angle, unit=1, uuid="")
        all_pins = index.pins(lib_id)

        wanted = set(pin_numbers or [])
        needle = name_contains.lower() if name_contains else None
        matched = [
            p for p in all_pins
            if p.number in wanted or (needle and needle in p.name.lower())
        ]
        if not matched:
            raise ToolInputError(
                f"No pins on {reference} matched pin_numbers={pin_numbers!r} "
                f"name_contains={name_contains!r}.",
                remedy="Call get_symbol_pins to see the real pin names and numbers.",
            )

        builder = self._builder()
        marked = []
        for pin in matched:
            pt = placed.pin_point(pin)
            builder.add_no_connect(pt)
            if annotate:
                dx, dy = placed.pin_outward(pin)
                text_at = (_snap(pt[0] + dx * 5.08), _snap(pt[1] + dy * 5.08))
                builder.add_text(annotate, text_at, size=1.27)
            marked.append({"number": pin.number, "name": pin.name})

        self._merge(builder)
        return marked

    def place_symbol(
        self,
        index: SymbolIndex,
        lib_id: str,
        assignments: dict[str, str],
        reference: str | None = None,
        value: str | None = None,
        footprint: str = "",
        at: tuple[float, float] | None = None,
    ) -> dict:
        """Place a new symbol into this (already-existing) schematic and wire
        named pins to nets by label.

        This is `generate_pinout_schematic`'s pin-matching and label-wiring,
        aimed at a file that already has content instead of authoring a fresh
        one. Unlike that function there is no MCU-specific power auto-wiring --
        a generic connector's pins 1/5/6 are not "VDD"/"GND" by convention, so
        guessing would be wrong more often than it helped. Call
        `mark_pins_unused` separately for pins that should be no-connected
        rather than left floating.
        """
        prefix = "".join(c for c in lib_id.split(":")[-1] if c.isalpha())[:1] or "U"
        reference = reference or self.next_reference(prefix)
        value = value if value is not None else lib_id.split(":")[-1]
        if at:
            x, y = _snap(at[0]), _snap(at[1])
            warnings = self.placement_warnings(index, lib_id, x, y)
        else:
            # Room for the labels about to hang off its pins, not just the body.
            longest = max((len(str(net)) for net in assignments.values()), default=0)
            inflate = LABEL_STUB + longest * 1.27 if assignments else 0.0
            x, y = self.free_spot(index, lib_id, inflate=inflate)
            warnings = []

        builder = self._builder()
        placed = builder.add_symbol(index, lib_id, reference, value, x=x, y=y, footprint=footprint)

        matched, unmatched = _match_pins([placed], assignments)
        connected = []
        for sym, pin, net in matched:
            builder.label_pin(sym, pin, net)
            connected.append({"pin": pin.number, "pin_name": pin.name, "net": net})

        self._merge(builder)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "position": {"x": x, "y": y},
            "footprint": placed.footprint,
            "warnings": warnings,
            "connected": connected,
            "unmatched_assignments": unmatched,
            "available_pin_names": [p.name for p in placed.pins][:200],
        }

    def label_pins(
        self,
        index: SymbolIndex,
        reference: str,
        assignments: dict[str, str],
    ) -> dict:
        """Wire named pins of an already-placed symbol to nets by label.

        `place_symbol` can only label pins at the moment it places a symbol;
        this is the same pin-matching and label-wiring for one that is already
        on the sheet -- placed by an earlier call or by hand, rotated or
        mirrored. Multi-unit symbols are searched across every placed unit.

        A pin that already has something at its connection point (a wire, a
        label, a no-connect, another symbol's pin) is skipped and reported, not
        labelled: a second label there would silently short two nets together.
        """
        placed = self.placements(index, reference)
        matched, unmatched = _match_pins(placed, assignments)
        occupied = self._occupied_points(index, exclude=reference)

        builder = self._builder()
        connected, skipped = [], []
        for sym, pin, net in matched:
            entry = {"pin": pin.number, "pin_name": pin.name, "net": net}
            if _key(sym.pin_point(pin)) in occupied:
                skipped.append(entry)
                continue
            builder.label_pin(sym, pin, net)
            connected.append(entry)

        self._merge(builder)
        return {
            "reference": reference,
            "connected": connected,
            "already_connected": skipped,
            "unmatched_assignments": unmatched,
            "available_pin_names": [p.name for s in placed for p in s.pins][:200],
        }

    def set_fields(
        self,
        reference: str,
        fields: dict[str, str],
        footprints: FootprintIndex | None = None,
    ) -> dict:
        """Set fields of a placed symbol: Value, Footprint, Datasheet, or any custom one.

        Applied to every unit of a multi-unit symbol, since KiCad expects the
        units of one part to agree. A field the symbol lacks is added hidden.
        Renaming `Reference` also rewrites the `instances` block -- KiCad
        annotates from there, so changing only the visible field would revert
        on the next annotation. With `footprints`, a Footprint that does not
        resolve is still written (the library may be registered later) but
        reported, naming the same footprint under a library that does resolve.
        """
        nodes = [s for s in sexpr.children(self.tree, "symbol") if _reference(s) == reference]
        if not nodes:
            raise ToolInputError(
                f"No symbol with reference {reference!r} in {self.path.name}.",
                remedy="Call list_schematic_symbols to see what's placed.",
            )
        new_ref = fields.get("Reference")
        if new_ref is not None and new_ref != reference and new_ref in self.references():
            raise ToolInputError(
                f"Reference {new_ref!r} is already used in {self.path.name}.",
                remedy="Pick a free reference, or rename the other symbol first.",
            )

        changed: dict[str, dict] = {}
        added: list[str] = []
        for node in nodes:
            at = sexpr.values(node, "at")
            for name, raw in fields.items():
                value = str(raw)
                prop = next(
                    (p for p in sexpr.children(node, "property") if len(p) >= 3 and p[1] == name),
                    None,
                )
                if prop is None:
                    prop = _property(name, value, float(at[0]), float(at[1]), hide=True)
                    # Keep properties together, ahead of pins/instances, as KiCad writes them.
                    last = list(sexpr.children(node, "property"))[-1]
                    node.insert(next(i for i, n in enumerate(node) if n is last) + 1, prop)
                    if name not in added:
                        added.append(name)
                    continue
                if prop[2] != value:
                    changed.setdefault(name, {"old": str(prop[2]), "new": value})
                    prop[2] = value
            if new_ref is not None:
                for ref_node in sexpr.find_all(sexpr.child(node, "instances") or [], "reference"):
                    ref_node[1] = new_ref

        warnings = []
        if footprints is not None:
            warning = footprint_warning(footprints, fields.get("Footprint", ""))
            if warning:
                warnings.append(warning)
        return {
            "reference": new_ref or reference,
            "units": len(nodes),
            "changed": changed,
            "added": added,
            "warnings": warnings,
        }

    def placements(self, index: SymbolIndex, reference: str) -> list[PlacedSymbol]:
        """Every placed unit of `reference`, with its orientation and own pins."""
        found = []
        for symbol in sexpr.children(self.tree, "symbol"):
            if _reference(symbol) != reference:
                continue
            lib_id = str(sexpr.value(symbol, "lib_id", ""))
            at = sexpr.values(symbol, "at")
            if not lib_id or not at:
                continue
            unit = int(sexpr.value(symbol, "unit", 1))
            mirror = sexpr.value(symbol, "mirror")
            found.append(PlacedSymbol(
                reference, "", lib_id,
                x=float(at[0]), y=float(at[1]),
                angle=float(at[2]) if len(at) > 2 else 0.0,
                unit=unit, uuid="", mirror=str(mirror) if mirror else None,
                pins=[p for p in index.pins(lib_id) if p.unit in (0, unit)],
            ))
        if not found:
            raise ToolInputError(
                f"No symbol with reference {reference!r} in {self.path.name}.",
                remedy="Call list_schematic_symbols to see what's placed.",
            )
        return found

    def _occupied_points(self, index: SymbolIndex, exclude: str) -> set[tuple[float, float]]:
        """Canvas points where something already makes a connection.

        Pins of `exclude` itself are left out: they are what we are labelling,
        and a symbol's own stacked pins (several GND pins on one point) are
        already the same net by design.
        """
        points: set[tuple[float, float]] = set()
        for wire in sexpr.children(self.tree, "wire"):
            pts = sexpr.child(wire, "pts")
            for xy in sexpr.children(pts, "xy") if pts is not None else []:
                points.add(_key((float(xy[1]), float(xy[2]))))
        for kind in ("label", "global_label", "hierarchical_label", "no_connect"):
            for node in sexpr.children(self.tree, kind):
                at = sexpr.values(node, "at")
                if at:
                    points.add(_key((float(at[0]), float(at[1]))))

        others = {
            ref for symbol in sexpr.children(self.tree, "symbol")
            if (ref := _reference(symbol)) and ref != exclude
        }
        for ref in others:
            try:
                for sym in self.placements(index, ref):
                    points.update(_key(sym.pin_point(p)) for p in sym.pins)
            except ToolInputError:
                continue  # a symbol whose library is gone still can't be labelled over
        return points

    def swap_power_symbol(self, index: SymbolIndex, old_lib_id: str, new_lib_id: str) -> int:
        """Repoint power symbols from one rail to another.

        Safe only when both symbols place their pin identically -- otherwise the
        wire would no longer meet the pin. Verified before swapping rather than
        assumed.
        """
        old_pins = index.pins(old_lib_id)
        new_pins = index.pins(new_lib_id)
        if not old_pins or not new_pins:
            raise ToolInputError(f"{old_lib_id} or {new_lib_id} has no pins.")
        a, b = old_pins[0], new_pins[0]
        if (a.x, a.y, a.angle) != (b.x, b.y, b.angle):
            raise ToolInputError(
                f"Cannot swap {old_lib_id} for {new_lib_id}: their pins sit at "
                f"different positions ({a.x},{a.y},{a.angle}) vs ({b.x},{b.y},{b.angle}), "
                "so existing wires would no longer connect.",
                remedy="Delete and re-place the symbol instead.",
            )

        new_name = new_lib_id.split(":")[-1]
        changed = 0
        for symbol in sexpr.children(self.tree, "symbol"):
            lib_node = sexpr.child(symbol, "lib_id")
            if lib_node is None or lib_node[1] != old_lib_id:
                continue
            lib_node[1] = new_lib_id
            for prop in sexpr.children(symbol, "property"):
                if len(prop) >= 3 and prop[1] == "Value":
                    prop[2] = new_name
            changed += 1

        if changed:
            self._ensure_lib_symbol(index, new_lib_id)
            self._prune_unused_lib_symbols()
        return changed

    def add_two_pin_component(
        self,
        index: SymbolIndex,
        lib_id: str,
        value: str,
        top_rail: str,
        bottom_rail: str,
        reference: str | None = None,
        at: tuple[float, float] | None = None,
        footprint: str = "",
    ) -> dict:
        """Place a two-pin part vertically between two power rails.

        Both ends terminate in power symbols rather than labels: for decoupling
        this is the conventional drawing, and it guarantees the net connection
        without depending on label text matching.
        """
        pins = index.pins(lib_id)
        if len(pins) < 2:
            raise ToolInputError(
                f"{lib_id} has {len(pins)} pin(s); need exactly two.",
                remedy="Use a two-terminal symbol such as Device:C or Device:R.",
            )

        prefix = "".join(c for c in lib_id.split(":")[-1] if c.isalpha())[:1] or "U"
        reference = reference or self.next_reference(prefix)
        if at:
            x, y = _snap(at[0]), _snap(at[1])
        else:
            # Clear the wire stubs and rail symbols above and below, too.
            x, y = self.free_spot(index, lib_id, inflate=STUB + 7.62)

        builder = self._builder()
        placed = builder.add_symbol(
            index, lib_id, reference, value, x=x, y=y, footprint=footprint
        )

        # Order pins by canvas y so "top" and "bottom" mean what they look like.
        ordered = sorted(pins[:2], key=lambda p: placed.pin_point(p)[1])
        for pin, rail in ((ordered[0], top_rail), (ordered[1], bottom_rail)):
            point = placed.pin_point(pin)
            dx, dy = placed.pin_outward(pin)
            end = (_snap(point[0] + dx * STUB), _snap(point[1] + dy * STUB))
            builder.add_wire(point, end)
            builder.add_symbol_pinned_at(
                index, rail, self.next_power_reference(builder),
                rail.split(":")[-1], target=end, hide_reference=True,
            )

        self._merge(builder)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "position": {"x": x, "y": y},
            "top_rail": top_rail,
            "bottom_rail": bottom_rail,
        }

    def next_power_reference(self, builder: SchematicBuilder) -> str:
        """Power symbols use #PWRnnn; keep them unique across edits."""
        used = {r for r in self.references() if r.startswith("#PWR")}
        used |= {
            str(prop[2])
            for node in builder.parts()["symbols"]
            for prop in sexpr.children(node, "property")
            if len(prop) >= 3 and prop[1] == "Reference" and str(prop[2]).startswith("#PWR")
        }
        n = 1
        while f"#PWR{n:03d}" in used:
            n += 1
        return f"#PWR{n:03d}"

    # --- internals ------------------------------------------------------

    def _ensure_lib_symbol(self, index: SymbolIndex, lib_id: str) -> None:
        block = sexpr.child(self.tree, "lib_symbols")
        if block is None:
            block = [Sym("lib_symbols")]
            self.tree.insert(_insert_index(self.tree), block)
        for existing in sexpr.children(block, "symbol"):
            if len(existing) > 1 and existing[1] == lib_id:
                return
        block.append(index.definition(lib_id))

    def _prune_unused_lib_symbols(self) -> None:
        """Drop embedded definitions nothing references any more.

        A stale definition is harmless to KiCad but shows up in the library
        table as a phantom part, which is confusing when reviewing a diff.
        """
        used = {
            str(sexpr.value(symbol, "lib_id", ""))
            for symbol in sexpr.children(self.tree, "symbol")
        }
        block = sexpr.child(self.tree, "lib_symbols")
        if block is None:
            return
        keep = [block[0]] + [
            node
            for node in block[1:]
            if not (isinstance(node, list) and len(node) > 1 and node[1] not in used)
        ]
        block[:] = keep

    def _merge(self, builder: SchematicBuilder) -> None:
        parts = builder.parts()

        block = sexpr.child(self.tree, "lib_symbols")
        if block is None:
            block = [Sym("lib_symbols")]
            self.tree.insert(_insert_index(self.tree), block)
        present = {n[1] for n in sexpr.children(block, "symbol") if len(n) > 1}
        for lib_id, node in sorted(parts["lib_symbols"].items()):
            if lib_id not in present:
                block.append(node)

        # Order within the document does not matter to KiCad, but keeping
        # symbols last matches what KiCad itself writes.
        for key in ("junctions", "no_connects", "wires", "texts", "labels", "symbols"):
            self.tree.extend(parts[key])
        self._move_trailing_blocks_last()

    def _move_trailing_blocks_last(self) -> None:
        """`sheet_instances` and `embedded_fonts` must stay at the end."""
        trailing = []
        for name in ("sheet_instances", "embedded_fonts"):
            node = sexpr.child(self.tree, name)
            if node is not None:
                self.tree.remove(node)
                trailing.append(node)
        self.tree.extend(trailing)

    def save(self) -> Path:
        self.path.write_text(sexpr.dumps(self.tree), encoding="utf-8")
        return self.path


def _local_points(node: sexpr.SExpr, unit: int | None = None,
                  include_pins: bool = True) -> list[tuple[float, float]]:
    """Every drawn point of a symbol definition, in its library (Y-up) frame.

    Body graphics and pin connection points; a pin's `at` is its outer end, so
    pins need nothing extra; `include_pins=False` gives the body alone. With
    `unit`, only that unit's graphics and the shared (unit 0) ones -- a
    multi-unit part's units are placed separately.
    """
    pts: list[tuple[float, float]] = []
    for sub in sexpr.children(node, "symbol"):
        m = re.search(r"_(\d+)_\d+$", str(sub[1]) if len(sub) > 1 else "")
        if unit is not None and m and int(m.group(1)) not in (0, unit):
            continue
        for rect in sexpr.children(sub, "rectangle"):
            for key in ("start", "end"):
                v = sexpr.values(rect, key)
                if v:
                    pts.append((float(v[0]), float(v[1])))
        for poly in list(sexpr.children(sub, "polyline")) + list(sexpr.children(sub, "bezier")):
            for xy in sexpr.children(sexpr.child(poly, "pts") or [], "xy"):
                pts.append((float(xy[1]), float(xy[2])))
        for circle in sexpr.children(sub, "circle"):
            c, r = sexpr.values(circle, "center"), sexpr.value(circle, "radius", 0)
            if c:
                cx, cy, r = float(c[0]), float(c[1]), float(r)
                pts += [(cx - r, cy - r), (cx + r, cy + r)]
        for arc in sexpr.children(sub, "arc"):
            for key in ("start", "mid", "end"):
                v = sexpr.values(arc, key)
                if v:
                    pts.append((float(v[0]), float(v[1])))
        for pin in sexpr.children(sub, "pin") if include_pins else ():
            v = sexpr.values(pin, "at")
            if v:
                pts.append((float(v[0]), float(v[1])))
    return pts or [(0.0, 0.0)]


def _relative_box(index: SymbolIndex, lib_id: str) -> Box:
    """Canvas box of `lib_id` placed unrotated at the origin, fields included.

    Reference and Value go where `SchematicBuilder.add_symbol` puts them:
    centred, one field-offset beyond the outermost pin above and below.
    """
    local = _local_points(index.definition(lib_id), unit=1)
    pins = [p for p in index.pins(lib_id) if p.unit in (0, 1)]
    extent = max((abs(p.y) for p in pins), default=10.0) + 5.08
    pts = [(px, -py) for px, py in local] + [(0.0, -extent), (0.0, extent)]
    return _grow(_bounds(pts), 1.27)  # field text has height of its own


def _bounds(pts: list[tuple[float, float]]) -> Box:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _grow(box: Box, by: float) -> Box:
    return (box[0] - by, box[1] - by, box[2] + by, box[3] + by)


def _inside(box: Box, area: Box) -> bool:
    return area[0] <= box[0] and area[1] <= box[1] and box[2] <= area[2] and box[3] <= area[3]


def _intersects(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _text_box(x: float, y: float, angle: float, text: str, size: float = 1.27) -> Box:
    """Rough extent of a label or text run from its anchor, along its angle."""
    length = len(text) * size + size
    direction = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}.get(int(angle) % 360, (1, 0))
    end = (x + direction[0] * length, y + direction[1] * length)
    return _grow(_bounds([(x, y), end]), size)


def _snap_up(v: float) -> float:
    """Snap to the grid without moving closer to what we are clearing."""
    return round(math.ceil(round(v / GRID, 6)) * GRID, 4)


def _reference(symbol: sexpr.SExpr) -> str | None:
    for prop in sexpr.children(symbol, "property"):
        if len(prop) >= 3 and prop[1] == "Reference":
            return str(prop[2])
    return None


def _key(point: tuple[float, float]) -> tuple[float, float]:
    """Coordinates comparable across our float maths and KiCad's written values."""
    return (round(point[0], 2), round(point[1], 2))


def _match_pins(
    placed: list[PlacedSymbol], assignments: dict[str, str]
) -> tuple[list[tuple[PlacedSymbol, PinInfo, str]], list[str]]:
    """Resolve assignment keys to pins by name or number.

    Case-insensitive and blind to underscores and hyphens, and `GPIO4` also
    finds a pin named `IO4` -- firmware and symbol authors disagree on both.
    """
    by_key: dict[str, list[tuple[PlacedSymbol, PinInfo]]] = {}
    for sym in placed:
        for pin in sym.pins:
            for key in (_normalise_key(pin.name), _normalise_key(pin.number)):
                if key:
                    by_key.setdefault(key, []).append((sym, pin))

    matched, unmatched = [], []
    for raw_key, net in assignments.items():
        key = _normalise_key(str(raw_key))
        candidates = by_key.get(key) or by_key.get(key.replace("GPIO", "IO"))
        if not candidates:
            unmatched.append(str(raw_key))
            continue
        sym, pin = candidates[0]
        matched.append((sym, pin, str(net)))
    return matched, unmatched


def _insert_index(tree: sexpr.SExpr) -> int:
    """Position for a new lib_symbols block: after the header scalars."""
    for i, node in enumerate(tree):
        if isinstance(node, list):
            return i
    return len(tree)


def add_decoupling_capacitors(
    index: SymbolIndex,
    schematic: str | Path,
    rails: list[str],
    value: str = "100nF",
    ground: str = "power:GND",
    cap_lib_id: str = "Device:C",
    footprint: str = "",
) -> dict:
    """Add one decoupling capacitor per named rail, each tied to ground.

    `rails` are power-symbol lib_ids such as `power:+3V3`. Each capacitor is
    placed clear of existing content and wired rail-to-ground.
    """
    editor = SchematicEditor.load(schematic)
    added = []
    # One at a time: each placement sees the capacitors placed before it, so
    # a row that fills up wraps instead of running off the sheet.
    for rail in rails:
        added.append(
            editor.add_two_pin_component(
                index, cap_lib_id, value, top_rail=rail, bottom_rail=ground,
                footprint=footprint,
            )
        )
    editor.save()
    return {"schematic": str(editor.path), "added": added, "count": len(added)}
