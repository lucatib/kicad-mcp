"""Read `.kicad_sch` files directly.

KiCad 10 exposes no schematic IPC API -- the 10.0 release branch defines zero
schematic commands -- so schematic access is file-based. That turns out to be an
advantage for generation: we can produce schematics without KiCad running at all.

Everything here is read-only. Generation lives in `generator.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .errors import ToolInputError
from .sexpr import Sym

LABEL_KINDS = ("label", "global_label", "hierarchical_label")


@dataclass
class SymbolInstance:
    reference: str
    value: str
    lib_id: str
    x: float
    y: float
    angle: float
    unit: int
    uuid: str
    footprint: str = ""
    dnp: bool = False
    properties: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "reference": self.reference,
            "value": self.value,
            "lib_id": self.lib_id,
            "position": {"x": self.x, "y": self.y},
            "rotation": self.angle,
            "unit": self.unit,
            "uuid": self.uuid,
            "footprint": self.footprint,
            "dnp": self.dnp,
            "properties": self.properties,
        }


def _prop_map(node: sexpr.SExpr) -> dict:
    out: dict = {}
    for prop in sexpr.children(node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str):
            out[prop[1]] = prop[2] if isinstance(prop[2], str) else str(prop[2])
    return out


def _bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    text = v.name if isinstance(v, Sym) else str(v)
    return text.lower() in ("yes", "true", "1")


class Schematic:
    """A parsed `.kicad_sch`."""

    def __init__(self, path: Path, tree: sexpr.SExpr) -> None:
        self.path = path
        self.tree = tree

    @classmethod
    def load(cls, path: str | Path) -> "Schematic":
        p = Path(path)
        if not p.is_file():
            raise ToolInputError(
                f"Schematic not found: {p}",
                remedy="Pass the full path to a .kicad_sch file.",
            )
        if p.suffix != ".kicad_sch":
            raise ToolInputError(
                f"Not a schematic file: {p.name}",
                remedy="Schematic files have the .kicad_sch extension.",
            )
        try:
            tree = sexpr.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise ToolInputError(
                f"Could not parse {p.name}: {exc}",
                remedy="The file may be corrupt or from an incompatible KiCad version.",
            ) from exc
        return cls(p, tree)

    # --- header ---------------------------------------------------------

    @property
    def version(self) -> int | None:
        v = sexpr.value(self.tree, "version")
        return int(v) if isinstance(v, (int, float)) else None

    @property
    def uuid(self) -> str:
        return str(sexpr.value(self.tree, "uuid", ""))

    @property
    def paper(self) -> str:
        return str(sexpr.value(self.tree, "paper", ""))

    def title_block(self) -> dict:
        tb = sexpr.child(self.tree, "title_block")
        if tb is None:
            return {}
        out = {}
        for key in ("title", "date", "rev", "company"):
            val = sexpr.value(tb, key)
            if val is not None:
                out[key] = str(val)
        for comment in sexpr.children(tb, "comment"):
            if len(comment) >= 3:
                out[f"comment{comment[1]}"] = comment[2]
        return out

    # --- contents -------------------------------------------------------

    def symbols(self) -> list[SymbolInstance]:
        """Placed symbol instances, excluding power symbols' hidden nature.

        Power symbols are included: they are real instances and matter for
        netlist reasoning, even though they carry no footprint.
        """
        out: list[SymbolInstance] = []
        for node in sexpr.children(self.tree, "symbol"):
            props = _prop_map(node)
            at = sexpr.values(node, "at")
            out.append(
                SymbolInstance(
                    reference=props.get("Reference", ""),
                    value=props.get("Value", ""),
                    lib_id=str(sexpr.value(node, "lib_id", "")),
                    x=float(at[0]) if len(at) > 0 else 0.0,
                    y=float(at[1]) if len(at) > 1 else 0.0,
                    angle=float(at[2]) if len(at) > 2 else 0.0,
                    unit=int(sexpr.value(node, "unit", 1) or 1),
                    uuid=str(sexpr.value(node, "uuid", "")),
                    footprint=props.get("Footprint", ""),
                    dnp=_bool(sexpr.value(node, "dnp")),
                    properties=props,
                )
            )
        out.sort(key=lambda s: s.reference)
        return out

    def labels(self) -> list[dict]:
        out = []
        for kind in LABEL_KINDS:
            for node in sexpr.children(self.tree, kind):
                at = sexpr.values(node, "at")
                out.append(
                    {
                        "kind": kind,
                        "text": node[1] if len(node) > 1 and isinstance(node[1], str) else "",
                        "position": {
                            "x": float(at[0]) if len(at) > 0 else 0.0,
                            "y": float(at[1]) if len(at) > 1 else 0.0,
                        },
                        "rotation": float(at[2]) if len(at) > 2 else 0.0,
                    }
                )
        return out

    def wires(self) -> list[dict]:
        out = []
        for node in sexpr.children(self.tree, "wire"):
            pts = sexpr.child(node, "pts")
            coords = []
            if pts:
                for xy in sexpr.children(pts, "xy"):
                    coords.append({"x": float(xy[1]), "y": float(xy[2])})
            out.append({"points": coords})
        return out

    def junctions(self) -> list[dict]:
        out = []
        for node in sexpr.children(self.tree, "junction"):
            at = sexpr.values(node, "at")
            out.append({"x": float(at[0]), "y": float(at[1])} if len(at) >= 2 else {})
        return out

    def sheets(self) -> list[dict]:
        """Child sheets, with the file each one points at.

        This is how hierarchy is traversed: a sheet's `Sheetfile` property names
        a sibling `.kicad_sch` on disk.
        """
        out = []
        for node in sexpr.children(self.tree, "sheet"):
            props = _prop_map(node)
            at = sexpr.values(node, "at")
            filename = props.get("Sheetfile") or props.get("Sheet file", "")
            out.append(
                {
                    "name": props.get("Sheetname") or props.get("Sheet name", ""),
                    "file": filename,
                    "path": str((self.path.parent / filename).resolve()) if filename else "",
                    "position": {
                        "x": float(at[0]) if len(at) > 0 else 0.0,
                        "y": float(at[1]) if len(at) > 1 else 0.0,
                    },
                    "uuid": str(sexpr.value(node, "uuid", "")),
                }
            )
        return out

    def lib_symbol_ids(self) -> list[str]:
        block = sexpr.child(self.tree, "lib_symbols")
        if block is None:
            return []
        return [s[1] for s in sexpr.children(block, "symbol") if len(s) > 1 and isinstance(s[1], str)]

    def summary(self) -> dict:
        syms = self.symbols()
        powers = [s for s in syms if s.lib_id.startswith("power:")]
        return {
            "path": str(self.path),
            "version": self.version,
            "uuid": self.uuid,
            "paper": self.paper,
            "title_block": self.title_block(),
            "counts": {
                "symbols": len(syms),
                "power_symbols": len(powers),
                "components": len(syms) - len(powers),
                "labels": len(self.labels()),
                "wires": len(self.wires()),
                "junctions": len(self.junctions()),
                "sheets": len(self.sheets()),
                "embedded_lib_symbols": len(self.lib_symbol_ids()),
            },
            "sheets": self.sheets(),
        }


def find_project_schematics(project_dir: str | Path) -> list[str]:
    d = Path(project_dir)
    if not d.is_dir():
        raise ToolInputError(
            f"Not a directory: {d}", remedy="Pass a KiCad project folder."
        )
    return sorted(str(p) for p in d.glob("*.kicad_sch"))
