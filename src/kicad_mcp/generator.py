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
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from . import cli as kcli
from . import sexpr
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

    # --- content -------------------------------------------------------

    def add_symbol(self, index: SymbolIndex, lib_id: str, reference: str, value: str,
                   x: float, y: float, angle: float = 0, unit: int = 1,
                   footprint: str = "", hide_value: bool = False,
                   hide_reference: bool = False) -> PlacedSymbol:
        """Place a symbol, embedding its library definition on first use.

        A `.kicad_sch` does not resolve libraries at load time -- it carries its
        own copy of each symbol -- so the definition must be embedded or KiCad
        shows a rescue dialog.
        """
        if lib_id not in self._lib_symbols:
            self._lib_symbols[lib_id] = index.definition(lib_id)
        # The embedded definition's Footprint is only a library default; the
        # instance's own field is what the netlist and PCB actually read. Left
        # empty, the part silently arrives on the board with no footprint.
        footprint = footprint or _property_value(self._lib_symbols[lib_id], "Footprint")

        pins = [p for p in index.pins(lib_id) if p.unit in (0, unit)]
        placed = PlacedSymbol(
            reference=reference, value=value, lib_id=lib_id,
            x=_snap(x), y=_snap(y), angle=angle, unit=unit,
            uuid=_uuid(), footprint=footprint, pins=pins,
        )
        self.placed.append(placed)

        extent = max((abs(p.y) for p in pins), default=10.0) + 5.08
        node: sexpr.SExpr = [
            Sym("symbol"),
            make("lib_id", lib_id),
            make("at", placed.x, placed.y, angle),
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
                    [Sym("path"), f"/{self.uuid}", make("reference", reference), make("unit", unit)],
                ],
            ],
        ]
        self._symbols.append(node)
        return placed

    def add_symbol_pinned_at(self, index: SymbolIndex, lib_id: str, reference: str,
                             value: str, target: tuple[float, float],
                             pin_number: str | None = None, **kwargs) -> PlacedSymbol:
        """Place a symbol so that one of its pins lands exactly on `target`.

        Power symbols are placed this way: what matters is that the pin meets the
        wire, not where the graphic sits.
        """
        pins = index.pins(lib_id)
        if not pins:
            raise ToolInputError(f"Symbol {lib_id!r} has no pins to align.")
        pin = pins[0] if pin_number is None else next(
            (p for p in pins if p.number == pin_number), pins[0]
        )
        # Invert the pin transform: place = target - (px, -py)
        return self.add_symbol(
            index, lib_id, reference, value,
            x=target[0] - pin.x, y=target[1] + pin.y, **kwargs
        )

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
                  stub: float = LABEL_STUB) -> tuple[float, float]:
        """Wire a pin outward by `stub` and put a net label at the end.

        Labels rather than drawn nets: for a pinout-driven schematic this keeps
        the drawing readable and expresses connectivity exactly, which is what
        ERC and the netlist consume.
        """
        start = placed.pin_point(pin)
        dx, dy = placed.pin_outward(pin)
        end = (_snap(start[0] + dx * stub), _snap(start[1] + dy * stub))
        self.add_wire(start, end)
        # Rotate label text to run along the wire, and justify away from the pin.
        if abs(dx) > abs(dy):
            angle = 0 if dx > 0 else 180
            justify = ["left", "bottom"] if dx > 0 else ["right", "bottom"]
        else:
            angle = 270 if dy > 0 else 90
            justify = ["left", "bottom"]
        self.add_label(net, end, angle, justify)
        return end

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
) -> dict:
    """Build a schematic for an MCU with named nets on assigned pins.

    `assignments` maps a pin name or number (e.g. "IO4", "4") to a net name.
    Matching is case-insensitive and ignores underscores and hyphens, because
    firmware pinouts are written as `GPIO_4`, `IO4`, and `gpio4` interchangeably.

    Unassigned signal pins are left floating rather than marked no-connect: the
    user is likely to keep designing, and wrong no-connects are worse than none.
    """
    out_path = Path(output)
    project_name = project_name or out_path.stem
    builder = SchematicBuilder(
        project_name=project_name, paper=paper,
        title=title or f"{mcu_lib_id.split(':')[-1]} pinout",
    )
    # Match what the installed KiCad writes, or it asks to re-save on open.
    builder.version = kcli.schematic_format_version(index.install)

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

    for raw_key, net in assignments.items():
        key = _normalise_key(str(raw_key))
        candidates = by_key.get(key) or by_key.get(key.replace("GPIO", "IO"))
        if not candidates:
            unmatched.append(str(raw_key))
            continue
        pin = candidates[0]
        builder.label_pin(mcu, pin, str(net))
        used_pins.add(pin.number)
        connected.append({"pin": pin.number, "pin_name": pin.name, "net": str(net)})

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
            ref = f"#PWR{len(power_symbols) + 1:03d}"
            try:
                builder.add_symbol_pinned_at(
                    index, lib, ref, lib.split(":")[-1], target=end,
                    hide_reference=True,
                )
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
                    builder.add_symbol_pinned_at(
                        index, "power:PWR_FLAG", f"#FLG{len(net_flagged) + 1:03d}",
                        "PWR_FLAG", target=flag_at, hide_reference=True, hide_value=True,
                    )
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
        "available_pin_names": [p.name for p in mcu.pins][:200],
    }
