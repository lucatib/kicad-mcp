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

from pathlib import Path

from . import sexpr
from .errors import ToolInputError
from .generator import GRID, PlacedSymbol, SchematicBuilder, _normalise_key, _snap
from .schematic import Schematic
from .sexpr import Sym
from .symbols import PinInfo, SymbolIndex

#: Where a two-pin part's pins sit relative to its centre, for Device:C and
#: friends: pin 1 above, pin 2 below, both on the 1.27 mm grid.
STUB = 2.54


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

    def half_width(self, index: SymbolIndex, lib_id: str) -> float:
        """Half-width of a symbol's body+pins, in millimetres from its origin.

        Measured from the body rectangle and every pin's local x, not assumed.
        Board-level symbols especially vary wildly in size -- ESP32-DevKitC
        spans +-33mm, Device:C spans about +-4mm -- so a fixed offset that
        works for one overlaps badly with the other.
        """
        try:
            node = index.definition(lib_id)
        except ToolInputError:
            return 12.7  # unknown part: a conservative guess, not a silent 0
        xs = [0.0]
        for sub in sexpr.children(node, "symbol"):
            for rect in sexpr.children(sub, "rectangle"):
                for pt in (sexpr.values(rect, "start"), sexpr.values(rect, "end")):
                    if pt:
                        xs.append(abs(float(pt[0])))
            for pin in sexpr.children(sub, "pin"):
                at = sexpr.values(pin, "at")
                if at:
                    xs.append(abs(float(at[0])))
        return max(xs)

    def free_x(self, index: SymbolIndex, lib_id: str, margin: float = 12.7,
               default: float = 190.5) -> float:
        """A centre x where placing `lib_id` will not overlap anything present.

        Takes the real half-width of every already-placed symbol (not just its
        centre coordinate) to find the current right-hand edge, then clears it
        by `margin` plus the new symbol's own half-width.
        """
        new_half = self.half_width(index, lib_id)
        edges = []
        for symbol in sexpr.children(self.tree, "symbol"):
            at = sexpr.values(symbol, "at")
            existing_lib = sexpr.value(symbol, "lib_id")
            if not at or not existing_lib:
                continue
            edges.append(float(at[0]) + self.half_width(index, str(existing_lib)))
        if not edges:
            return default
        return _snap(max(edges) + margin + new_half)

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
        x, y = at if at else (self.free_x(index, lib_id), 101.6)
        x, y = _snap(x), _snap(y)

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
        x, y = at if at else (self.free_x(index, lib_id), 101.6)
        x, y = _snap(x), _snap(y)

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
    x = editor.free_x(index, cap_lib_id)
    cap_span = editor.half_width(index, cap_lib_id) * 2 + 10.16
    for i, rail in enumerate(rails):
        added.append(
            editor.add_two_pin_component(
                index, cap_lib_id, value, top_rail=rail, bottom_rail=ground,
                at=(x + i * cap_span, 101.6), footprint=footprint,
            )
        )
    editor.save()
    return {"schematic": str(editor.path), "added": added, "count": len(added)}
