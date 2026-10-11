"""Generate `.kicad_sch` files by importing symbols from KiCad libraries.

Because KiCad 10 has no schematic IPC API, generation is pure file authoring --
which means it works with KiCad closed, and the result is a normal schematic the
user can open and edit by hand.

Coordinate systems are the thing to get right. Symbol libraries use Y-up; the
schematic canvas uses Y-down. A pin at `(at px py angle)` in a `.kicad_sym`
records its *connection point*, with `angle` pointing from that point toward the
symbol body. So for a symbol placed at `(sx, sy)` with no rotation:

    connection point on canvas = (sx + px, sy - py)
    direction away from the body = (-cos(angle), +sin(angle))

Everything here follows from those two lines.
"""

from __future__ import annotations

import math
import re
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import cli as kcli
from . import hierarchy, sexpr
from .errors import ToolInputError
from .sexpr import Sym, make
from .symbols import PinInfo, SymbolIndex

GENERATOR = "kicad-mcp"
GRID = 1.27
LABEL_STUB = 5.08  # wire length from pin to label; two grid steps reads cleanly


def _uuid() -> str:
    return str(uuidlib.uuid4())


def _snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def _effects(size: float = 1.27, justify: list[str] | None = None, hide: bool = False) -> sexpr.SExpr:
    node = make("effects", make("font", make("size", size, size)))
    if justify:
        node.append(make("justify", *[Sym(j) for j in justify]))
    if hide:
        node.append(make("hide", Sym("yes")))
    return node


def _property(name: str, value: str, x: float, y: float, angle: float = 0,
              hide: bool = False, justify: list[str] | None = None) -> sexpr.SExpr:
    return [
        Sym("property"), name, value,
        make("at", x, y, angle),
        _effects(justify=justify, hide=hide),
    ]


def _property_value(node: sexpr.SExpr, name: str) -> str:
    for prop in sexpr.children(node, "property"):
        if len(prop) >= 3 and prop[1] == name:
            return str(prop[2])
    return ""


@dataclass
class PlacedSymbol:
    reference: str
    value: str
    lib_id: str
    x: float
    y: float
    angle: float
    unit: int
    uuid: str
    footprint: str = ""
    pins: list[PinInfo] = field(default_factory=list)
    #: KiCad's `(mirror x)` flips about the horizontal axis (canvas y negated),
    #: `(mirror y)` about the vertical one (canvas x negated); both apply after
    #: rotation. Symbols we place are never mirrored, but hand-placed ones are.
    mirror: str | None = None
    #: (sheet path, reference) for each use of the sheet the symbol is on.
    instances: list[tuple[str, str]] = field(default_factory=list)

    def _mirrored(self, dx: float, dy: float) -> tuple[float, float]:
        if self.mirror == "x":
            dy = -dy
        elif self.mirror == "y":
            dx = -dx
        return dx, dy

    def to_canvas(self, px: float, py: float) -> tuple[float, float]:
        """Canvas coordinates of a point given in the symbol's library frame."""
        if self.angle:
            a = math.radians(self.angle)
            cos_a, sin_a = math.cos(a), math.sin(a)
            px, py = px * cos_a - py * sin_a, px * sin_a + py * cos_a
        dx, dy = self._mirrored(px, -py)
        return (round(self.x + dx, 4), round(self.y + dy, 4))

    def pin_point(self, pin: PinInfo) -> tuple[float, float]:
        """Canvas coordinates of a pin's connection point."""
        return self.to_canvas(pin.x, pin.y)

    def pin_outward(self, pin: PinInfo) -> tuple[float, float]:
        """Unit vector pointing away from the symbol body, in canvas space."""
        a = math.radians(pin.angle + self.angle)
        dx, dy = self._mirrored(-math.cos(a), math.sin(a))
        return (round(dx, 6), round(dy, 6))


class SchematicBuilder:
    """Accumulates schematic content, then serialises a valid `.kicad_sch`."""

    def __init__(self, project_name: str, paper: str = "A3", title: str = "",
                 rev: str = "", company: str = "") -> None:
        self.project_name = project_name
        self.paper = paper
        self.title = title
        self.rev = rev
        self.company = company
        #: File format version; callers with an install set the one it writes.
        self.version = kcli.FALLBACK_SCH_VERSION
        self.uuid = _uuid()
        self._lib_symbols: dict[str, sexpr.SExpr] = {}
        self._symbols: list[sexpr.SExpr] = []
        self._wires: list[sexpr.SExpr] = []
        self._labels: list[sexpr.SExpr] = []
        self._junctions: list[sexpr.SExpr] = []
        self._no_connects: list[sexpr.SExpr] = []
        self._texts: list[sexpr.SExpr] = []
        self.placed: list[PlacedSymbol] = []
        #: The sheet paths this file is used under (see `hierarchy`); empty
        #: when it is its own root, annotated under `/<its uuid>`.
        self.instance_paths: list[str] = []
        #: References in use anywhere in the project. New ones never collide,
        #: and every reference this builder hands out is added.
        self.taken: set[str] = set()

    def next_reference(self, prefix: str, width: int = 0) -> str:
        """First free `prefix<N>`, zero-padded to `width` digits (#PWR001)."""
        n = 1
        while f"{prefix}{n:0{width}d}" in self.taken:
            n += 1
        return f"{prefix}{n:0{width}d}"

    # --- content -------------------------------------------------------

    def add_symbol(self, index: SymbolIndex, lib_id: str, reference: str, value: str,
                   x: float, y: float, angle: float = 0, unit: int = 1,
                   footprint: str = "", hide_value: bool = False,
                   hide_reference: bool = False, mirror: str | None = None) -> PlacedSymbol:
        """Place a symbol, embedding its library definition on first use.

        `mirror` is KiCad's "x" (flip top/bottom) or "y" (flip left/right).

        A `.kicad_sch` does not resolve libraries at load time -- it carries its
        own copy of each symbol -- so the definition must be embedded or KiCad
        shows a rescue dialog.

        `reference` annotates the first use of the sheet; a sheet used more
        than once gets the next free reference of the same prefix per extra
        use, since each use is a separate part on the board.
        """
        if lib_id not in self._lib_symbols:
            self._lib_symbols[lib_id] = index.definition(lib_id)
        # The embedded definition's Footprint is only a library default; the
        # instance's own field is what the netlist and PCB actually read. Left
        # empty, the part silently arrives on the board with no footprint.
        footprint = footprint or _property_value(self._lib_symbols[lib_id], "Footprint")

        pins = [p for p in index.pins(lib_id) if p.unit in (0, unit)]
        paths = self.instance_paths or [f"/{self.uuid}"]
        references = [reference]
        self.taken.add(reference)
        prefix = re.sub(r"[\d?]+$", "", reference)
        for _ in paths[1:]:
            references.append(self.next_reference(prefix, 3 if prefix.startswith("#") else 0))
            self.taken.add(references[-1])
        placed = PlacedSymbol(
            reference=reference, value=value, lib_id=lib_id,
            x=_snap(x), y=_snap(y), angle=angle, unit=unit,
            uuid=_uuid(), footprint=footprint, pins=pins, mirror=mirror,
            instances=list(zip(paths, references)),
        )
        self.placed.append(placed)

        extent = max((abs(p.y) for p in pins), default=10.0) + 5.08
        node: sexpr.SExpr = [
            Sym("symbol"),
            make("lib_id", lib_id),
            make("at", placed.x, placed.y, angle),
            *([make("mirror", Sym(mirror))] if mirror else []),
            make("unit", unit),
            make("exclude_from_sim", Sym("no")),
            make("in_bom", Sym("yes")),
            make("on_board", Sym("yes")),
            make("dnp", Sym("no")),
            make("uuid", placed.uuid),
            _property("Reference", reference, placed.x, _snap(placed.y - extent),
                      hide=hide_reference),
            _property("Value", value, placed.x, _snap(placed.y + extent),
                      hide=hide_value),
            _property("Footprint", footprint, placed.x, placed.y, hide=True),
            _property("Datasheet", "", placed.x, placed.y, hide=True),
            _property("Description", "", placed.x, placed.y, hide=True),
            [
                Sym("instances"),
                [
                    Sym("project"), self.project_name,
                    *[[Sym("path"), path, make("reference", ref), make("unit", unit)]
                      for path, ref in placed.instances],
                ],
            ],
        ]
        self._symbols.append(node)
        return placed

    def use_library_fields(self, placed: PlacedSymbol) -> None:
        """Put Reference and Value where the library draws them.

        `add_symbol` centres them above and below the outermost pins, which
        suits an IC. On a two-pin part standing between rails that is exactly
        where the rail wires run; the library's own spots sit beside it.
        """
        definition = self._lib_symbols[placed.lib_id]
        node = next(n for n in self._symbols if sexpr.value(n, "uuid") == placed.uuid)
        for prop in sexpr.children(node, "property"):
            lib = next((p for p in sexpr.children(definition, "property") if p[1] == prop[1]), None)
            if prop[1] not in ("Reference", "Value") or lib is None or len(sexpr.values(lib, "at")) < 3:
                continue
            lx, ly, langle = sexpr.values(lib, "at")[:3]
            x, y = placed.to_canvas(float(lx), float(ly))
            justify = [sexpr.as_text(j) for j in sexpr.values(sexpr.child(lib, "effects") or [], "justify")]
            hidden = sexpr.child(sexpr.child(prop, "effects") or [], "hide") is not None
            prop[3] = make("at", x, y, langle)
            prop[4] = _effects(justify=justify or None, hide=hidden)

    def add_power_symbol(self, index: SymbolIndex, lib_id: str, target: tuple[float, float],
                         direction: tuple[float, float], value: str | None = None,
                         hide_value: bool = False) -> PlacedSymbol:
        """A power symbol with its pin on `target`, pointing along `direction`.

        Rotated so the body lies beyond `target` -- a GND on an upward-facing
        pin points up rather than back over the wire and the part -- and the
        Value placed past the tip, reading along `direction` like a label
        does, so the rails of neighbouring pins 2.54 mm apart never overlap.
        The reference comes from the library's own prefix (#PWR, #FLG) and
        is free across the project.
        """
        pins = index.pins(lib_id)
        if not pins:
            raise ToolInputError(f"Symbol {lib_id!r} has no pins to align.")
        pin = pins[0]
        dx, dy = round(direction[0]), round(direction[1])
        # The library pin's angle points from its end into the body; turn it onto `direction`.
        angle = int(round((math.degrees(math.atan2(-dy, dx)) - pin.angle) / 90) * 90) % 360
        probe = PlacedSymbol("", "", lib_id, x=0.0, y=0.0, angle=angle, unit=1, uuid="")
        ox, oy = probe.pin_point(pin)
        definition = index.definition(lib_id)
        prefix = _property_value(definition, "Reference") or "#PWR"
        placed = self.add_symbol(
            index, lib_id, self.next_reference(prefix, 3), value or lib_id.split(":")[-1],
            x=target[0] - ox, y=target[1] - oy, angle=angle,
            hide_reference=True, hide_value=hide_value,
        )

        tip = max(
            (cx - target[0]) * dx + (cy - target[1]) * dy
            for cx, cy in (placed.to_canvas(px, py) for px, py in _local_points(definition, 1, False))
        )
        text = placed.value
        # Centred past the tip. Justified text would flip sides: KiCad mirrors
        # a field's justification on a symbol turned 90 degrees but not 270.
        reach = max(tip, 0.0) + 1.27 + len(text) * 0.5  # ~1 mm per character at 1.27 mm
        # A field's stored angle is relative to its symbol's: on one turned
        # 90 or 270 degrees, a stored 90 reads horizontally and 0 vertically.
        text_angle = 90 if (dx == 0) != (angle % 180 == 90) else 0
        for prop in sexpr.children(self._symbols[-1], "property"):
            if prop[1] == "Value":
                prop[3] = make("at", round(target[0] + dx * reach, 4), round(target[1] + dy * reach, 4), text_angle)
                prop[4] = _effects(hide=hide_value)
        return placed

    def add_wire(self, a: tuple[float, float], b: tuple[float, float]) -> None:
        if a == b:
            return
        self._wires.append([
            Sym("wire"),
            [Sym("pts"), make("xy", a[0], a[1]), make("xy", b[0], b[1])],
            [Sym("stroke"), make("width", 0), make("type", Sym("default"))],
            make("uuid", _uuid()),
        ])

    def add_label(self, text: str, at: tuple[float, float], angle: float = 0,
                  justify: list[str] | None = None) -> None:
        self._labels.append([
            Sym("label"), text,
            make("at", at[0], at[1], angle),
            make("fields_autoplaced", Sym("yes")),
            _effects(justify=justify or ["left", "bottom"]),
            make("uuid", _uuid()),
        ])

    def add_global_label(self, text: str, at: tuple[float, float], angle: float = 0,
                         justify: list[str] | None = None, shape: str = "passive") -> None:
        """A label that joins every same-named global label in the project."""
        justify = justify or ["left"]
        self._labels.append([
            Sym("global_label"), text,
            make("shape", Sym(shape)),
            make("at", at[0], at[1], angle),
            make("fields_autoplaced", Sym("yes")),
            _effects(justify=justify),
            make("uuid", _uuid()),
            _property("Intersheetrefs", "${INTERSHEET_REFS}", at[0], at[1], angle,
                      hide=True, justify=justify),
        ])

    def add_junction(self, at: tuple[float, float]) -> None:
        self._junctions.append([
            Sym("junction"), make("at", at[0], at[1]),
            make("diameter", 0), [Sym("color"), 0, 0, 0, 0],
            make("uuid", _uuid()),
        ])

    def add_no_connect(self, at: tuple[float, float]) -> None:
        self._no_connects.append([
            Sym("no_connect"), make("at", at[0], at[1]), make("uuid", _uuid())
        ])

    def add_text(self, text: str, at: tuple[float, float], size: float = 2.0) -> None:
        self._texts.append([
            Sym("text"), text,
            make("at", at[0], at[1], 0),
            make("effects", make("font", make("size", size, size)), make("justify", Sym("left"))),
            make("uuid", _uuid()),
        ])

    def label_pin(self, placed: PlacedSymbol, pin: PinInfo, net: str,
                  stub: float = LABEL_STUB, kind: str = "label") -> tuple[float, float]:
        """Wire a pin outward by `stub` and put a net label at the end.

        Labels rather than drawn nets: for a pinout-driven schematic this keeps
        the drawing readable and expresses connectivity exactly, which is what
        ERC and the netlist consume. `kind` is "label" (this sheet only) or
        "global_label" (every sheet of the project).
        """
        start = placed.pin_point(pin)
        dx, dy = placed.pin_outward(pin)
        end = (_snap(start[0] + dx * stub), _snap(start[1] + dy * stub))
        self.add_wire(start, end)
        # Run the text along the wire, away from the pin. KiCad keeps label
        # text upright (270 loads as 90, 180 as 0), so which way it reads is
        # set by the justification: left reads right or up, right left or down.
        if abs(dx) > abs(dy):
            angle, side = (0, "left") if dx > 0 else (180, "right")
        else:
            angle, side = (270, "right") if dy > 0 else (90, "left")
        if kind == "global_label":
            self.add_global_label(net, end, angle, [side], shape=_label_shape(pin))
        else:
            self.add_label(net, end, angle, [side, "bottom"])
        return end

    def power_pin(self, index: SymbolIndex, placed: PlacedSymbol, pin: PinInfo, lib_id: str,
                  net: str | None = None, stub: float = LABEL_STUB) -> PlacedSymbol:
        """Wire a pin outward by `stub` to a power symbol pointing away from it."""
        start = placed.pin_point(pin)
        dx, dy = placed.pin_outward(pin)
        end = (_snap(start[0] + dx * stub), _snap(start[1] + dy * stub))
        self.add_wire(start, end)
        return self.add_power_symbol(index, lib_id, end, (dx, dy), value=net)

    def connect_pin(self, index: SymbolIndex, placed: PlacedSymbol, pin: PinInfo,
                    join: "Join") -> None:
        """Join a pin to a net the way `classify_net` decided."""
        if join.via == "power_symbol":
            self.power_pin(index, placed, pin, join.lib_id, join.net)
        else:
            self.label_pin(placed, pin, join.net, kind=join.via)

    # --- output --------------------------------------------------------

    def parts(self) -> dict:
        """The accumulated content, for callers merging into an existing tree.

        `build()` produces a whole new document; an editor modifying a schematic
        that already exists needs the pieces instead.
        """
        return {
            "lib_symbols": dict(self._lib_symbols),
            "symbols": list(self._symbols),
            "wires": list(self._wires),
            "labels": list(self._labels),
            "junctions": list(self._junctions),
            "no_connects": list(self._no_connects),
            "texts": list(self._texts),
        }

    def build(self) -> sexpr.SExpr:
        title_block: sexpr.SExpr = [Sym("title_block")]
        if self.title:
            title_block.append(make("title", self.title))
        if self.rev:
            title_block.append(make("rev", self.rev))
        if self.company:
            title_block.append(make("company", self.company))

        lib_block: sexpr.SExpr = [Sym("lib_symbols")]
        for _, node in sorted(self._lib_symbols.items()):
            lib_block.append(node)

        tree: sexpr.SExpr = [
            Sym("kicad_sch"),
            make("version", self.version),
            make("generator", GENERATOR),
            make("generator_version", "10.0"),
            make("uuid", self.uuid),
            make("paper", self.paper),
        ]
        if len(title_block) > 1:
            tree.append(title_block)
        tree.append(lib_block)
        tree.extend(self._junctions)
        tree.extend(self._no_connects)
        tree.extend(self._wires)
        tree.extend(self._texts)
        tree.extend(self._labels)
        tree.extend(self._symbols)
        tree.append([
            Sym("sheet_instances"),
            [Sym("path"), "/", make("page", "1")],
        ])
        tree.append(make("embedded_fonts", Sym("no")))
        return tree

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        if p.suffix != ".kicad_sch":
            raise ToolInputError(
                f"Output must be a .kicad_sch file, got {p.name}",
                remedy="Use a path ending in .kicad_sch",
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sexpr.dumps(self.build()), encoding="utf-8")
        return p


# --- how a pin joins a net ------------------------------------------------


class Join(NamedTuple):
    """How a pin joins a net: by local label, global label or power symbol."""

    net: str
    via: str  # "label", "global_label" or "power_symbol"
    lib_id: str | None = None

    def describe(self, pin: PinInfo) -> dict:
        out = {"pin": pin.number, "pin_name": pin.name, "net": self.net, "via": self.via}
        if self.lib_id:
            out["symbol"] = self.lib_id
        return out


def classify_net(
    index: SymbolIndex,
    net: str,
    global_nets: set[str],
    power_nets: dict[str, str],
    global_labels: set[str],
) -> Join:
    """Decide how a pin joins `net`.

    Only power symbols and global labels connect across sheets; a local
    label is a different net on every sheet. In order:

    - a name in `global_nets` gets a global label, as asked;
    - a `Library:Symbol` id names a power symbol outright (`power:+5V`);
    - a name that a power symbol already carries in the project, or that a
      stock `power` symbol carries (GND, +3V3, VBUS...), gets that symbol --
      a rail is a power net, not a label that happens to share its name;
    - a name already used by a global label in the project stays global;
    - anything else is a local label.
    """
    if net in global_nets:
        return Join(net, "global_label")
    library, sep, name = net.partition(":")
    if sep and library in index.entries:
        definition = index.definition(net)  # raises with a remedy if it does not exist
        if not hierarchy.is_global_power(definition):
            raise ToolInputError(
                f"{net} is not a power symbol, so it cannot name a net.",
                remedy="Pass a net name, or a power symbol such as power:+3V3 "
                       "(search_symbols with library='power' lists them).",
            )
        return Join(name, "power_symbol", net)
    if net in power_nets:
        return Join(net, "power_symbol", power_nets[net])
    if net != "PWR_FLAG" and _stock_power(index, net):
        return Join(net, "power_symbol", f"power:{net}")
    if net in global_labels:
        return Join(net, "global_label")
    return Join(net, "label")


def _stock_power(index: SymbolIndex, name: str) -> bool:
    try:
        return hierarchy.is_global_power(index.definition(f"power:{name}"))
    except ToolInputError:
        return False


def _label_shape(pin: PinInfo) -> str:
    """A global label's shape, from the direction of the pin it serves."""
    return {
        "input": "input", "output": "output", "bidirectional": "bidirectional",
        "tri_state": "tri_state",
    }.get(pin.electrical_type, "passive")


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


# --- pinout-driven generation -------------------------------------------

POWER_NETS = {
    "3V3": "power:+3V3", "+3V3": "power:+3V3", "3.3V": "power:+3V3",
    "VCC": "power:VCC", "VDD": "power:VDD", "5V": "power:+5V", "+5V": "power:+5V",
    "GND": "power:GND", "VSS": "power:GND", "AGND": "power:GND",
}


def _is_power_pin(pin: PinInfo) -> bool:
    return pin.electrical_type in ("power_in", "power_out")


def _normalise_key(text: str) -> str:
    return text.strip().upper().replace("_", "").replace("-", "")


def generate_pinout_schematic(
    index: SymbolIndex,
    output: str | Path,
    mcu_lib_id: str,
    assignments: dict[str, str],
    project_name: str | None = None,
    title: str = "",
    mcu_reference: str = "U1",
    mcu_value: str | None = None,
    mcu_footprint: str = "",
    paper: str = "A3",
    connect_power: bool = True,
    origin: tuple[float, float] = (152.4, 101.6),
    global_nets: list[str] | None = None,
) -> dict:
    """Build a schematic for an MCU with named nets on assigned pins.

    `assignments` maps a pin name or number (e.g. "IO4", "4") to a net name.
    Matching is case-insensitive and ignores underscores and hyphens, because
    firmware pinouts are written as `GPIO_4`, `IO4`, and `gpio4` interchangeably.
    Each net is joined as `classify_net` decides: names in `global_nets` by
    global label, power rails by power symbol, the rest by local label.

    Unassigned signal pins are left floating rather than marked no-connect: the
    user is likely to keep designing, and wrong no-connects are worse than none.

    When `output` is a sheet of a KiCad project, its symbols are annotated
    under the project and the sheet's paths in it, with references free
    across the whole project.
    """
    out_path = Path(output)
    place = hierarchy.locate(out_path)
    builder = SchematicBuilder(
        project_name=project_name or place.project, paper=paper,
        title=title or f"{mcu_lib_id.split(':')[-1]} pinout",
    )
    # Match what the installed KiCad writes, or it asks to re-save on open.
    builder.version = kcli.schematic_format_version(index.install)
    builder.instance_paths = list(place.paths)
    if place.root is not None and place.paths == [f"/{_file_uuid(out_path)}"]:
        builder.uuid = place.paths[0][1:]  # regenerating the root: sub-sheet paths start with its uuid
    builder.taken = place.taken_references()
    warnings = []
    if mcu_reference in builder.taken:
        warnings.append(f"{mcu_reference} is already used on another sheet of {place.project}; "
                        "rename one of them with set_symbol_fields.")

    all_pins = index.pins(mcu_lib_id)
    if not all_pins:
        raise ToolInputError(
            f"Symbol {mcu_lib_id!r} exposes no pins.",
            remedy="Check the lib_id with search_symbols.",
        )

    mcu = builder.add_symbol(
        index, mcu_lib_id, mcu_reference,
        mcu_value if mcu_value is not None else mcu_lib_id.split(":")[-1],
        x=origin[0], y=origin[1], footprint=mcu_footprint,
    )

    # Index pins by both name and number for flexible matching.
    by_key: dict[str, list[PinInfo]] = {}
    for pin in mcu.pins:
        for key in (_normalise_key(pin.name), _normalise_key(pin.number)):
            if key:
                by_key.setdefault(key, []).append(pin)

    connected: list[dict] = []
    unmatched: list[str] = []
    used_pins: set[str] = set()
    power, globals_ = place.power_nets(), place.global_labels()
    joins = {
        str(net): classify_net(index, str(net), set(global_nets or ()), power, globals_)
        for net in assignments.values()
    }

    for raw_key, net in assignments.items():
        key = _normalise_key(str(raw_key))
        candidates = by_key.get(key) or by_key.get(key.replace("GPIO", "IO"))
        if not candidates:
            unmatched.append(str(raw_key))
            continue
        pin = candidates[0]
        builder.connect_pin(index, mcu, pin, joins[str(net)])
        used_pins.add(pin.number)
        connected.append(joins[str(net)].describe(pin))

    power_symbols: list[dict] = []
    net_flagged: set[str] | None = set() if connect_power else None
    if connect_power:
        for pin in mcu.pins:
            if not _is_power_pin(pin) or pin.number in used_pins:
                continue
            lib = POWER_NETS.get(_normalise_key(pin.name).replace(".", ""))
            if lib is None:
                lib = POWER_NETS.get(pin.name.strip().upper())
            if lib is None:
                continue
            point = mcu.pin_point(pin)
            dx, dy = mcu.pin_outward(pin)
            branch = (_snap(point[0] + dx * 2.54), _snap(point[1] + dy * 2.54))
            end = (_snap(point[0] + dx * 5.08), _snap(point[1] + dy * 5.08))
            builder.add_wire(point, branch)
            builder.add_wire(branch, end)
            try:
                builder.add_power_symbol(index, lib, end, (dx, dy))
            except ToolInputError:
                continue

            # A power symbol alone leaves ERC reporting "power pin not driven":
            # nothing in the sheet asserts the rail is actually sourced. PWR_FLAG
            # on a perpendicular branch is KiCad's idiom for saying that it is.
            if net_flagged is not None and lib not in net_flagged:
                perp = (-dy, dx)
                flag_at = (_snap(branch[0] + perp[0] * 5.08), _snap(branch[1] + perp[1] * 5.08))
                builder.add_wire(branch, flag_at)
                try:
                    builder.add_power_symbol(index, "power:PWR_FLAG", flag_at, perp, hide_value=True)
                    builder.add_junction(branch)
                    net_flagged.add(lib)
                except ToolInputError:
                    pass

            used_pins.add(pin.number)
            power_symbols.append({"pin": pin.number, "pin_name": pin.name, "symbol": lib})

    written = builder.write(out_path)
    return {
        "schematic": str(written),
        "mcu": {"lib_id": mcu_lib_id, "reference": mcu_reference, "pin_count": len(all_pins)},
        "connected": connected,
        "connected_count": len(connected),
        "power_symbols": power_symbols,
        "unmatched_assignments": unmatched,
        "warnings": warnings,
        "available_pin_names": [p.name for p in mcu.pins][:200],
    }


def _file_uuid(path: Path) -> str:
    """The `(uuid ...)` of an existing schematic, or "" if it has none."""
    try:
        return str(sexpr.value(sexpr.parse(path.read_text(encoding="utf-8")), "uuid", ""))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ""
