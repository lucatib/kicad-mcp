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
from .generator import GRID, SchematicBuilder, _snap
from .schematic import Schematic
from .sexpr import Sym
from .symbols import SymbolIndex

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

    # --- edits ----------------------------------------------------------

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
