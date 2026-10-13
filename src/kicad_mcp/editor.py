"""Modify schematics that already exist.

`generator.py` authors a document from nothing. This module changes one in
place, which is a different problem: existing content must survive untouched,
and new symbols must join the same project and sheet instance as the old ones or
KiCad treats them as unannotated strays.

Edits append rather than rewrite. Anything the user drew by hand between our
edits is preserved, because we touch only the nodes an edit is about and never
regenerate the document.
"""

from __future__ import annotations

import functools
import math
from collections import defaultdict
from pathlib import Path

from . import hierarchy, sexpr
from .errors import KicadMcpError, ToolInputError
from .footprints import FootprintIndex, footprint_warning
from .generator import (
    GRID, LABEL_STUB, Join, PlacedSymbol, SchematicBuilder, _local_points, _normalise_key,
    _property, _snap, classify_net,
)
from .schematic import LABEL_KINDS, Schematic
from .sexpr import Sym
from .symbols import PinInfo, SymbolIndex, definition_pins

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

    @functools.cached_property
    def place(self) -> hierarchy.SheetPlace:
        """This file's project and the sheet paths it is used under."""
        return hierarchy.locate(self.path, self.tree)

    @property
    def project_name(self) -> str:
        """The project KiCad annotates this sheet's symbols under.

        New symbols must use it; a mismatch makes KiCad treat them as
        belonging to a different project and drop their annotation.
        """
        return self.place.project

    def _builder(self) -> SchematicBuilder:
        builder = SchematicBuilder(project_name=self.place.project)
        builder.uuid = self.root_uuid
        builder.instance_paths = list(self.place.paths)
        builder.taken = self.taken_references()
        return builder

    # --- queries --------------------------------------------------------

    def references(self) -> set[str]:
        """References shown on this sheet's symbols."""
        refs = set()
        for symbol in sexpr.children(self.tree, "symbol"):
            for prop in sexpr.children(symbol, "property"):
                if len(prop) >= 3 and prop[1] == "Reference":
                    refs.add(str(prop[2]))
        return refs

    def taken_references(self) -> set[str]:
        """Every reference in use in the project, on this sheet's every use included."""
        return hierarchy.symbol_references(self.tree, self.place.project) | self.place.taken_references()

    def next_reference(self, prefix: str) -> str:
        """First `prefix<N>` free across the project, so edits never collide."""
        existing = self.taken_references()
        n = 1
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"

    def net_joins(self, index: SymbolIndex, nets, global_nets: list[str] | None = None) -> dict[str, Join]:
        """How each net joins its pins; see `generator.classify_net`."""
        power = {**self.place.power_nets(), **hierarchy.power_nets(self.tree)}
        globals_ = self.place.global_labels() | hierarchy.label_names(self.tree, "global_label")
        wanted = set(global_nets or ())
        return {str(n): classify_net(index, str(n), wanted, power, globals_) for n in nets}

    def _join_warnings(self, joins: dict[str, Join]) -> list[str]:
        """Labels that look connected across sheets but are not.

        A local label never reaches another sheet, and a global label does not
        join a local one of the same name on another sheet either.
        """
        out = []
        for join in joins.values():
            if join.via == "power_symbol":
                continue  # KiCad joins same-named local labels to a power net
            sheets = self.place.sheets_with_local_label(join.net)
            if not sheets:
                continue
            here = "a local label here" if join.via == "label" else "a global label here"
            out.append(
                f"{join.net} is also a local label on {', '.join(sheets)}; as {here} it is "
                "a different net. To connect them, pass it in global_nets here and turn "
                "those labels global (in KiCad: right-click > Change To > Global Label)."
            )
        return out

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
        rel, fits, anchors = self._fitter(index, lib_id, inflate)
        for _, y, x in sorted(self._candidates(rel, anchors)):
            if fits(x, y):
                return x, y
        raise self._no_room(lib_id)

    def connected_spot(
        self,
        index: SymbolIndex,
        lib_id: str,
        links: list[tuple[PinInfo, list[tuple[tuple[float, float], tuple[int, int] | None]]]],
        inflate: float = 0.0,
    ) -> tuple[float, float, str | None]:
        """Origin and mirroring for `lib_id` that keep its wires short.

        `links` pairs each of the new part's pins with the points it will
        connect to already on the sheet, and the direction each of those pins
        faces. Both orientations -- as drawn, and flipped left/right -- are
        tried, at the ordinary free spots plus spots beside the partners
        lined up with them. The winner is the one with the least estimated
        wire, where a pin facing away from its partner counts as a detour
        around the part. The same page rules as `free_spot` apply.
        """
        best: tuple[float, int, float, float, str | None] | None = None
        partners = [pt for _, ps in links for pt, _ in ps]
        pxs, pys = [p[0] for p in partners], [p[1] for p in partners]
        for mirror in (None, "y"):
            rel, fits, anchors = self._fitter(index, lib_id, inflate, mirror)
            probe = PlacedSymbol("", "", lib_id, x=0.0, y=0.0, angle=0.0, unit=1, uuid="", mirror=mirror)
            pins = [(probe.pin_point(p), probe.pin_outward(p), ps) for p, ps in links]
            y0 = sum(pys) / len(pys) - sum(pt[1] for pt, _, _ in pins) / len(pins)
            candidates = self._candidates(rel, anchors)
            for extra in (0.0, 12.7, 25.4, 38.1, 50.8, 76.2):
                for k in range(-15, 16):
                    y = _snap_up(y0 + k * GRID * 2)
                    candidates.append((1, y, _snap_up(min(pxs) - PLACE_MARGIN - extra - rel[2])))
                    candidates.append((1, y, _snap_up(max(pxs) + PLACE_MARGIN + extra - rel[0])))
            for tier, y, x in candidates:
                if not fits(x, y):
                    continue
                key = (_wire_cost(pins, x, y), tier, y, x, mirror)
                if best is None or key[:4] < best[:4]:
                    best = key
        if best is None:
            raise self._no_room(lib_id)
        _, _, y, x, mirror = best
        return x, y, mirror

    def partner_pins(
        self, index: SymbolIndex, nets: set[str]
    ) -> dict[str, list[tuple[tuple[float, float], tuple[int, int] | None]]]:
        """Where each named net already touches a pin, and which way that pin faces.

        Found through its labels: a label on a pin, or at the far end of a
        stub wire from one. A label reaching no pin still counts, as a point
        with no direction -- being near it is still better than not.
        """
        pins: dict[tuple[float, float], tuple[int, int]] = {}
        for ref in self.references():
            try:
                units = self.placements(index, ref)
            except ToolInputError:
                continue
            for sym in units:
                for pin in sym.pins:
                    dx, dy = sym.pin_outward(pin)
                    pins[_key(sym.pin_point(pin))] = (round(dx), round(dy))
        ends: dict[tuple[float, float], list[tuple[float, float]]] = {}
        for wire in sexpr.children(self.tree, "wire"):
            pts = [(float(xy[1]), float(xy[2]))
                   for xy in sexpr.children(sexpr.child(wire, "pts") or [], "xy")]
            if len(pts) == 2:
                ends.setdefault(_key(pts[0]), []).append(pts[1])
                ends.setdefault(_key(pts[1]), []).append(pts[0])

        out: dict[str, list] = {}
        for kind in ("label", "global_label", "hierarchical_label"):
            for label in sexpr.children(self.tree, kind):
                name = str(label[1]) if len(label) > 1 else ""
                at = sexpr.values(label, "at")
                if name not in nets or not at:
                    continue
                point = (float(at[0]), float(at[1]))
                if _key(point) in pins:
                    hit = (point, pins[_key(point)])
                else:
                    hit = next(
                        ((far, pins[_key(far)]) for far in ends.get(_key(point), [])
                         if _key(far) in pins),
                        (point, None),
                    )
                out.setdefault(name, []).append(hit)
        return out

    def _fitter(self, index: SymbolIndex, lib_id: str, inflate: float, mirror: str | None = None):
        """(relative box, fits(x, y), anchors) for placing `lib_id` on this sheet."""
        rl, rt, rr, rb = _relative_box(index, lib_id)
        if mirror == "y":
            rl, rr = -rr, -rl
        rel = (rl - inflate, rt - inflate, rr + inflate, rb + inflate)
        area = self.drawing_area()
        anchors, blockers = self._obstacles(index)
        blockers.append(self.title_block())
        gap = PLACE_MARGIN / 2

        def fits(x: float, y: float) -> bool:
            box = (x + rel[0], y + rel[1], x + rel[2], y + rel[3])
            if not _inside(box, area):
                return False
            grown = (box[0] - gap, box[1] - gap, box[2] + gap, box[3] + gap)
            return not any(_intersects(grown, b) for b in blockers)

        return rel, fits, anchors

    def _candidates(self, rel: Box, anchors) -> list[tuple[int, float, float]]:
        """free_spot's spots as (tier, y, x): lower tiers are preferred."""
        rl, rt = rel[0], rel[1]
        area = self.drawing_area()
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
        return [(tier, _snap_up(y), _snap_up(x)) for tier, y, x in candidates]

    def _no_room(self, lib_id: str) -> ToolInputError:
        w, h = self.page_size()
        return ToolInputError(
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
        global_nets: list[str] | None = None,
    ) -> dict:
        """Place a new symbol into this (already-existing) schematic and wire
        named pins to nets.

        This is `generate_pinout_schematic`'s pin-matching and net-joining,
        aimed at a file that already has content instead of authoring a fresh
        one: power rails get power symbols, names in `global_nets` (or already
        global in the project) global labels, the rest local labels. Unlike
        that function there is no MCU-specific power auto-wiring -- a generic
        connector's pins 1/5/6 are not "VDD"/"GND" by convention, so guessing
        would be wrong more often than it helped. Call `mark_pins_unused`
        separately for pins that should be no-connected rather than left
        floating.
        """
        joins = self.net_joins(index, assignments.values(), global_nets)
        prefix = "".join(c for c in lib_id.split(":")[-1] if c.isalpha())[:1] or "U"
        warnings = []
        if reference and reference in self.taken_references():
            warnings.append(f"{reference} is already used in project {self.place.project}; "
                            "rename one of them with set_symbol_fields.")
        reference = reference or self.next_reference(prefix)
        value = value if value is not None else lib_id.split(":")[-1]
        mirror = None
        if at:
            x, y = _snap(at[0]), _snap(at[1])
            warnings += self.placement_warnings(index, lib_id, x, y)
        else:
            # Room for the labels about to hang off its pins, not just the body.
            longest = max((len(str(net)) for net in assignments.values()), default=0)
            inflate = LABEL_STUB + longest * 1.27 if assignments else 0.0
            # A rail is everywhere; sitting next to one of its pins says nothing.
            signals = {k: v for k, v in assignments.items() if joins[str(v)].via != "power_symbol"}
            links = self._links(index, lib_id, signals)
            if links:
                x, y, mirror = self.connected_spot(index, lib_id, links, inflate=inflate)
            else:
                x, y = self.free_spot(index, lib_id, inflate=inflate)

        builder = self._builder()
        placed = builder.add_symbol(index, lib_id, reference, value, x=x, y=y,
                                    footprint=footprint, mirror=mirror)

        matched, unmatched = _match_pins([placed], assignments)
        connected = []
        for sym, pin, net in matched:
            builder.connect_pin(index, sym, pin, joins[net])
            connected.append(joins[net].describe(pin))

        self._merge(builder)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "position": {"x": x, "y": y},
            "footprint": placed.footprint,
            "mirrored": mirror is not None,
            "instances": [{"path": p, "reference": r} for p, r in placed.instances],
            "warnings": warnings + self._join_warnings(joins),
            "connected": connected,
            "unmatched_assignments": unmatched,
            "available_pin_names": [p.name for p in placed.pins][:200],
        }

    def label_pins(
        self,
        index: SymbolIndex,
        reference: str,
        assignments: dict[str, str],
        global_nets: list[str] | None = None,
    ) -> dict:
        """Wire named pins of an already-placed symbol to nets.

        `place_symbol` can only wire pins at the moment it places a symbol;
        this is the same pin-matching and net-joining for one that is already
        on the sheet -- placed by an earlier call or by hand, rotated or
        mirrored. Multi-unit symbols are searched across every placed unit.

        A pin that already has something at its connection point (a wire, a
        label, a no-connect, another symbol's pin) is skipped and reported, not
        labelled: a second label there would silently short two nets together.
        """
        placed = self.placements(index, reference)
        matched, unmatched = _match_pins(placed, assignments)
        joins = self.net_joins(index, {net for _, _, net in matched}, global_nets)
        occupied = self._occupied_points(index, exclude=reference)

        builder = self._builder()
        connected, skipped = [], []
        for sym, pin, net in matched:
            if _key(sym.pin_point(pin)) in occupied:
                skipped.append({"pin": pin.number, "pin_name": pin.name, "net": net})
                continue
            builder.connect_pin(index, sym, pin, joins[net])
            connected.append(joins[net].describe(pin))

        self._merge(builder)
        return {
            "reference": reference,
            "connected": connected,
            "already_connected": skipped,
            "unmatched_assignments": unmatched,
            "warnings": self._join_warnings(joins),
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
        on the next annotation. On a sheet used more than once only the use
        that carries the old reference is renamed; the other uses are other
        parts with references of their own. With `footprints`, a Footprint that does not
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
        if new_ref is not None and new_ref != reference and new_ref in self.taken_references():
            raise ToolInputError(
                f"Reference {new_ref!r} is already used in project {self.place.project}.",
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
                ref_nodes = list(sexpr.find_all(sexpr.child(node, "instances") or [], "reference"))
                # A lone stale entry is this part's too; KiCad copies the field into it on load.
                matching = [n for n in ref_nodes if n[1] == reference]
                for ref_node in matching or (ref_nodes if len(ref_nodes) == 1 else []):
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

    def _links(self, index: SymbolIndex, lib_id: str, assignments: dict[str, str]):
        """The new part's assigned pins, each with where its net already is."""
        if not assignments:
            return []
        probe = PlacedSymbol("", "", lib_id, x=0.0, y=0.0, angle=0.0, unit=1, uuid="",
                             pins=[p for p in index.pins(lib_id) if p.unit in (0, 1)])
        matched, _ = _match_pins([probe], assignments)
        partners = self.partner_pins(index, {net for _, _, net in matched})
        return [(pin, partners[net]) for _, pin, net in matched if net in partners]

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
                remedy="Remove it with remove_symbols and place the new one instead.",
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

    def remove_symbols(self, index: SymbolIndex, references: list[str]) -> dict:
        """Delete placed symbols, and whatever was there only to connect them.

        Every unit of a part goes, and on a sheet used more than once it goes
        from every use -- there is only one drawing. Then what hung off its
        pins: wire runs that now end in nothing, and labels, power symbols,
        PWR_FLAGs, no-connects and junctions that no longer reach any pin.
        Anything still reaching another part's pin or a sheet pin stays. So
        do two things that reach no pin but are reported instead of removed:
        wiring that joins two net names, since removing it would split those
        nets for the parts still on them, and a hierarchical label, which the
        parent sheet's pin refers to. An unknown reference removes nothing.
        """
        wanted = list(dict.fromkeys(str(r) for r in references))
        nodes = [s for s in sexpr.children(self.tree, "symbol") if _all_references(s) & set(wanted)]
        found = set().union(*map(_all_references, nodes))
        missing = [r for r in wanted if r not in found]
        if missing:
            raise ToolInputError(
                f"No symbol with reference {', '.join(missing)} in {self.path.name}; nothing was removed.",
                remedy="Call list_schematic_symbols to see what's placed.",
            )

        embedded = _embedded_definitions(self.tree)
        seeds: list[tuple[float, float]] = []
        warnings: dict[str, str] = {}
        for node in nodes:
            ref = _reference(node) or "?"
            points = _pin_points(index, embedded, node)
            if points is None:
                warnings[f"pins:{ref}"] = (
                    f"Pins of {ref} ({sexpr.value(node, 'lib_id')}) are unknown, so what was "
                    "wired to it was left as drawn."
                )
            seeds += points or []
            uses = sorted(set(_instance_references(node)))
            if len(uses) > 1:
                warnings[f"uses:{ref}"] = (
                    f"{ref} is drawn once on a sheet used {len(uses)} times; removing it "
                    f"removed {', '.join(uses)}."
                )

        gone = {id(n) for n in nodes}
        self.tree[:] = [n for n in self.tree if id(n) not in gone]
        layout = _Layout(self.tree, index, embedded)
        cleaned = layout.clean_up([_key(p) for p in seeds])
        self.tree[:] = [n for n in self.tree if id(n) not in layout.dead]
        self._prune_unused_lib_symbols()
        return {
            "removed": wanted,
            "units": len(nodes),
            "cleaned_up": cleaned,
            "warnings": list(warnings.values()) + layout.warnings,
        }

    def rename_labels(self, renames: dict[str, str]) -> dict:
        """Rename nets by renaming their labels on this sheet, of every kind.

        All renames apply at once, so a series can shift onto itself (IN3 to
        IN1 while IN5 becomes IN3) and two names can swap. Refused, changing
        nothing: a name no label here has, and any rename that would join two
        nets -- onto a name another label on this sheet keeps, onto a power
        net, or two names onto one. Other sheets are not touched, so warnings
        say when a global label's old name stays on other sheets (that net no
        longer reaches this one) or its new name is already there (this sheet
        joins it), and when a hierarchical label leaves the parent's sheet pin
        on the old name.
        """
        renames = {str(old): str(new) for old, new in renames.items() if str(old) != str(new)}
        by_name: dict[str, list[sexpr.SExpr]] = {}
        for kind in LABEL_KINDS:
            for node in sexpr.children(self.tree, kind):
                by_name.setdefault(str(node[1]), []).append(node)
        missing = [old for old in renames if old not in by_name]
        if missing:
            raise ToolInputError(
                f"No label named {', '.join(missing)} in {self.path.name}; nothing was renamed.",
                remedy="Call schematic_netlist to see the sheet's nets (a local label X shows as /X).",
            )

        targets: dict[str, list[str]] = {}
        for old, new in renames.items():
            targets.setdefault(new, []).append(old)
        kept = set(by_name) - set(renames)
        power = set(hierarchy.power_nets(self.tree)) | set(self.place.power_nets())
        clashes = []
        for new, olds in targets.items():
            if not new.strip():
                clashes.append(f"{olds[0]} would get an empty name")
            elif len(olds) > 1:
                clashes.append(f"{' and '.join(olds)} would all become {new}")
            elif new in kept:
                clashes.append(f"{olds[0]} would join {new}, a net this sheet already has")
            elif new in power:
                clashes.append(f"{olds[0]} would join the power net {new}")
        if clashes:
            raise ToolInputError(
                f"Renaming would join nets in {self.path.name}: {'; '.join(clashes)}. Nothing was renamed.",
                remedy="Pick a name not used on this sheet, or rename the other net too in the same call.",
            )

        renamed, warnings = [], []
        for old, new in renames.items():
            nodes = by_name[old]
            for node in nodes:
                node[1] = new
            renamed.append({"from": old, "to": new, "labels": len(nodes)})
            kinds = {n[0].name for n in nodes}
            if "global_label" in kinds:
                if sheets := self.place.sheets_with_label(old, "global_label"):
                    warnings.append(
                        f"{old} stays a global label on {', '.join(sheets)}, so that net no longer "
                        f"reaches this sheet; rename it there too to keep them one net."
                    )
                if sheets := self.place.sheets_with_label(new, "global_label"):
                    warnings.append(f"This sheet's {old} net joins the global net {new} of {', '.join(sheets)}.")
            if "hierarchical_label" in kinds:
                warnings.append(
                    f"{old} is a hierarchical label: the parent sheet's sheet pin {old} keeps its "
                    f"name and no longer connects; rename that pin to {new} in KiCad."
                )
        return {"renamed": renamed, "warnings": warnings}

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
        builder.use_library_fields(placed)  # off the rail wires above and below

        # Order pins by canvas y so "top" and "bottom" mean what they look like.
        ordered = sorted(pins[:2], key=lambda p: placed.pin_point(p)[1])
        for pin, rail in ((ordered[0], top_rail), (ordered[1], bottom_rail)):
            point = placed.pin_point(pin)
            dx, dy = placed.pin_outward(pin)
            end = (_snap(point[0] + dx * STUB), _snap(point[1] + dy * STUB))
            builder.add_wire(point, end)
            builder.add_power_symbol(index, rail, end, (dx, dy))

        self._merge(builder)
        return {
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "position": {"x": x, "y": y},
            "top_rail": top_rail,
            "bottom_rail": bottom_rail,
        }

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


#: A pin facing away from what it connects to: the wire has to go around the part.
FACING_PENALTY = 100.0


def _wire_cost(pins, x: float, y: float) -> float:
    """Estimated wire for a candidate origin: Manhattan distance, plus detours.

    `pins` holds, per connected pin of the new part, its offset from the
    origin, the direction it faces, and its partners' (point, direction).
    """
    total = 0.0
    for (px, py), (ox, oy), partners in pins:
        ax, ay = x + px, y + py
        (bx, by), facing = min(partners, key=lambda p: abs(p[0][0] - ax) + abs(p[0][1] - ay))
        vx, vy = bx - ax, by - ay
        total += abs(vx) + abs(vy)
        if ox * vx + oy * vy < 0:
            total += FACING_PENALTY
        if facing is not None and facing[0] * -vx + facing[1] * -vy < 0:
            total += FACING_PENALTY
    return total


def _snap_up(v: float) -> float:
    """Snap to the grid without moving closer to what we are clearing."""
    return round(math.ceil(round(v / GRID, 6)) * GRID, 4)


def _reference(symbol: sexpr.SExpr) -> str | None:
    for prop in sexpr.children(symbol, "property"):
        if len(prop) >= 3 and prop[1] == "Reference":
            return str(prop[2])
    return None


def _instance_references(symbol: sexpr.SExpr) -> list[str]:
    """The reference of each use of the sheet, from the symbol's `instances` block."""
    return [str(n[1]) for n in sexpr.find_all(sexpr.child(symbol, "instances") or [], "reference")]


def _all_references(symbol: sexpr.SExpr) -> set[str]:
    """Every name the symbol goes by: its Reference field and each use's annotation."""
    shown = _reference(symbol)
    return set(_instance_references(symbol)) | ({shown} if shown else set())


def _embedded_definitions(tree: sexpr.SExpr) -> dict[str, sexpr.SExpr]:
    """lib_id -> the definition copy the file embeds, which is what KiCad draws."""
    return {
        str(n[1]): n
        for n in sexpr.children(sexpr.child(tree, "lib_symbols") or [], "symbol")
        if len(n) > 1
    }


def _pin_points(
    index: SymbolIndex, embedded: dict[str, sexpr.SExpr], symbol: sexpr.SExpr
) -> list[tuple[float, float]] | None:
    """Canvas connection points of one placed unit; None if its pins are unknown.

    From the embedded copy first, so a part whose library is gone still has pins.
    """
    lib_id = str(sexpr.value(symbol, "lib_id", ""))
    at = sexpr.values(symbol, "at")
    if not lib_id or not at:
        return None
    unit = int(sexpr.value(symbol, "unit", 1) or 1)
    try:
        definition = embedded.get(lib_id)
        pins = definition_pins(definition) if definition is not None else index.pins(lib_id)
    except KicadMcpError:
        return None
    mirror = sexpr.value(symbol, "mirror")
    placed = PlacedSymbol(
        "", "", lib_id, x=float(at[0]), y=float(at[1]),
        angle=float(at[2]) if len(at) > 2 else 0.0, unit=unit, uuid="",
        mirror=str(mirror) if mirror else None,
    )
    return [placed.pin_point(p) for p in pins if p.unit in (0, unit)]


def _wire_ends(wire: sexpr.SExpr) -> list[tuple[float, float]]:
    pts = sexpr.child(wire, "pts")
    return [(float(xy[1]), float(xy[2])) for xy in sexpr.children(pts or [], "xy")]


def _on_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Whether `p` lies on the segment a-b, short of both ends."""
    if p in (a, b):
        return False
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length == 0:
        return False
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    return (
        abs(cross) / length <= 0.01
        and min(a[0], b[0]) - 0.01 <= p[0] <= max(a[0], b[0]) + 0.01
        and min(a[1], b[1]) - 0.01 <= p[1] <= max(a[1], b[1]) + 0.01
    )


Key = tuple[float, float]


class _Layout:
    """How what is left on a sheet connects, to find what a removal left loose.

    Built from the tree once the removed symbols are gone. A wire joins its
    two ends and anything lying along it. Power symbols and PWR_FLAGs
    (references starting with "#"), labels, junctions and no-connects only
    name or decorate a net; a net that reaches no other pin is not there
    for anything. Nodes to delete collect in `dead`.
    """

    LABELS = LABEL_KINDS

    def __init__(self, tree: sexpr.SExpr, index: SymbolIndex, embedded: dict[str, sexpr.SExpr]) -> None:
        self.dead: set[int] = set()
        self.warnings: list[str] = []
        self.wires = list(sexpr.children(tree, "wire"))
        self.ends: dict[int, tuple[Key, Key]] = {}
        self.wires_at: dict[Key, list[sexpr.SExpr]] = defaultdict(list)
        for wire in self.wires:
            pts = [_key(p) for p in _wire_ends(wire)]
            if len(pts) == 2 and pts[0] != pts[1]:
                self.ends[id(wire)] = (pts[0], pts[1])
                for p in pts:
                    self.wires_at[p].append(wire)
        self.wires = [w for w in self.wires if id(w) in self.ends]

        self.pins: dict[Key, list[sexpr.SExpr]] = defaultdict(list)
        for symbol in sexpr.children(tree, "symbol"):
            for p in _pin_points(index, embedded, symbol) or []:
                self.pins[_key(p)].append(symbol)
        self.items: dict[str, dict[Key, list[sexpr.SExpr]]] = {}
        for kind in (*self.LABELS, "junction", "no_connect"):
            self.items[kind] = defaultdict(list)
            for node in sexpr.children(tree, kind):
                at = sexpr.values(node, "at")
                if at:
                    self.items[kind][_key((float(at[0]), float(at[1])))].append(node)
        # Sheet pins and bus entries: connections to elsewhere, never loose.
        self.fixed: set[Key] = set()
        for sheet in sexpr.children(tree, "sheet"):
            for pin in sexpr.children(sheet, "pin"):
                at = sexpr.values(pin, "at")
                if at:
                    self.fixed.add(_key((float(at[0]), float(at[1]))))
        for entry in sexpr.children(tree, "bus_entry"):
            at, size = sexpr.values(entry, "at"), sexpr.values(entry, "size")
            if at and size:
                x, y = float(at[0]), float(at[1])
                self.fixed |= {_key((x, y)), _key((x + float(size[0]), y + float(size[1])))}

        self.points: set[Key] = set(self.pins) | self.fixed
        for by_point in self.items.values():
            self.points |= set(by_point)
        self._rows: dict[float, list[Key]] = defaultdict(list)
        self._cols: dict[float, list[Key]] = defaultdict(list)
        for p in self.points:
            self._rows[p[1]].append(p)
            self._cols[p[0]].append(p)

    def clean_up(self, seeds: list[Key]) -> dict:
        """Remove what the pins at `seeds` leave loose; report what went."""
        touched = set(seeds)
        loose = self._prune_wires(seeds, touched)
        report: dict = {"wires": loose, "labels": [], "power_symbols": [], "no_connects": 0, "junctions": 0}

        for keys in self._components(touched):
            symbols = {id(s): s for k in keys for s in self.pins.get(k, [])}
            labels = {kind: [n for k in keys for n in self.items[kind].get(k, [])] for kind in self.LABELS}
            if keys & self.fixed or any(not (_reference(s) or "").startswith("#") for s in symbols.values()):
                continue
            if labels["hierarchical_label"]:
                names = ", ".join(sorted({str(n[1]) for n in labels["hierarchical_label"]}))
                self.warnings.append(
                    f"Hierarchical label {names} no longer reaches any pin on this sheet; kept, "
                    "since the parent sheet's pin of that name refers to it."
                )
                continue
            names = {str(n[1]) for kind in ("label", "global_label") for n in labels[kind]}
            names |= {v for s in symbols.values() if (v := _property_text(s, "Value")) != "PWR_FLAG"}
            if len(names) > 1:
                self.warnings.append(
                    f"{' and '.join(sorted(names))} stay joined by wiring that no longer reaches "
                    "any pin; kept, since removing it would split those nets. Delete it in KiCad "
                    "if they should not be one net."
                )
                continue

            for wire in self.wires:
                if id(wire) not in self.dead and self.ends[id(wire)][0] in keys:
                    self.dead.add(id(wire))
                    report["wires"] += 1
            for kind in self.LABELS:
                for node in labels[kind]:
                    self.dead.add(id(node))
                    report["labels"].append(str(node[1]))
            for symbol in symbols.values():
                self.dead.add(id(symbol))
                report["power_symbols"].append(_reference(symbol))
            for kind, field in (("no_connect", "no_connects"), ("junction", "junctions")):
                for node in (n for k in keys for n in self.items[kind].get(k, [])):
                    self.dead.add(id(node))
                    report[field] += 1

        for key in touched:
            for node in self.items["junction"].get(key, []):
                if id(node) not in self.dead and self._connections(key) < 3:
                    self.dead.add(id(node))
                    report["junctions"] += 1
        return report

    def _live_wires(self) -> list[sexpr.SExpr]:
        return [w for w in self.wires if id(w) not in self.dead]

    def _along(self, wire: sexpr.SExpr) -> list[Key]:
        """Points of interest lying on `wire`, short of its ends."""
        a, b = self.ends[id(wire)]
        if a[1] == b[1]:
            candidates = self._rows.get(a[1], [])
        elif a[0] == b[0]:
            candidates = self._cols.get(a[0], [])
        else:
            candidates = self.points
        return [p for p in candidates if _on_segment(p, a, b)]

    def _held(self, key: Key) -> bool:
        """Whether something other than wire ends connects at `key`."""
        if key in self.pins or key in self.fixed or any(key in self.items[k] for k in self.LABELS):
            return True
        return any(_on_segment(key, *self.ends[id(w)]) for w in self._live_wires())

    def _prune_wires(self, seeds: list[Key], touched: set[Key]) -> int:
        """Remove wires left with an end touching nothing, run by run."""
        count = 0
        queue = list(seeds)
        while queue:
            key = queue.pop()
            ends = [w for w in self.wires_at.get(key, []) if id(w) not in self.dead]
            if len(ends) != 1 or self._held(key):
                continue
            wire = ends[0]
            self.dead.add(id(wire))
            count += 1
            a, b = self.ends[id(wire)]
            far = b if a == key else a
            touched.add(far)
            touched.update(self._along(wire))
            queue.append(far)
        return count

    def _components(self, touched: set[Key]) -> list[set[Key]]:
        """The connected groups of points that include a touched point."""
        parent: dict[Key, Key] = {}

        def find(k: Key) -> Key:
            parent.setdefault(k, k)
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        for wire in self._live_wires():
            a, b = self.ends[id(wire)]
            for p in (b, *self._along(wire)):
                parent[find(p)] = find(a)
        groups: dict[Key, set[Key]] = defaultdict(set)
        for k in list(parent):
            groups[find(k)].add(k)
        roots = {find(k) for k in touched}
        return [groups[r] or {r} for r in roots]

    def _connections(self, key: Key) -> int:
        """How many wire ends, wires passing through and pins meet at `key`."""
        live = self._live_wires()
        ends = sum(key in self.ends[id(w)] for w in live)
        through = sum(_on_segment(key, *self.ends[id(w)]) for w in live)
        pins = len(self.pins.get(key, [])) + (key in self.fixed)
        return ends + 2 * through + pins


def _property_text(symbol: sexpr.SExpr, name: str) -> str:
    for prop in sexpr.children(symbol, "property"):
        if len(prop) >= 3 and prop[1] == name:
            return str(prop[2])
    return ""


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
