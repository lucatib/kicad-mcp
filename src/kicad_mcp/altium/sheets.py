"""Altium sheets written as KiCad schematics.

One .kicad_sch per distinct .SchDoc: a sheet used by several sheet symbols
(a channel) is written once, and each of its symbols lists one instance per
use, each with the reference that use shows. Connectivity comes first: every
wire, junction, label, port and power port lands on the exact point Altium
put it, flipped once into KiCad's Y-down frame.
"""

from __future__ import annotations

import base64
import io
import math
import re
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from .. import sexpr
from ..sexpr import Sym, make
from . import symbols as sy
from .design import KICAD_ALTIUM_NS, Design, SheetNode, altium_kiid
from .model import Part, Sheet, location, vertices

#: Altium port/sheet-entry IOTYPE -> KiCad label shape.
IO_SHAPES = {0: "passive", 1: "output", 2: "input", 3: "bidirectional"}


def _uid(*parts: object) -> str:
    return str(uuid.uuid5(KICAD_ALTIUM_NS, ":".join(str(p) for p in parts)))


def overbar(text: str) -> str:
    """Altium marks an overbar with a backslash after each letter (R\\E\\S\\E\\T\\)."""
    if "\\" not in text:
        return text
    out, run = [], []
    i = 0
    while i < len(text):
        if i + 1 < len(text) and text[i + 1] == "\\":
            run.append(text[i])
            i += 2
            continue
        if run:
            out.append("~{" + "".join(run) + "}")
            run = []
        out.append(text[i])
        i += 1
    if run:
        out.append("~{" + "".join(run) + "}")
    return "".join(out)


def _stroke0():
    return make("stroke", make("width", 0), make("type", Sym("default")))


def _paper(sheet: Sheet | None) -> sexpr.SExpr:
    if sheet is None:
        return make("paper", "A4")
    if sheet.paper == "User":
        return make("paper", "User", sy.r6(sheet.width_mm), sy.r6(sheet.height_mm))
    if sheet.portrait:
        return make("paper", sheet.paper, Sym("portrait"))
    return make("paper", sheet.paper)


def _title_block(sheet: Sheet | None) -> sexpr.SExpr | None:
    if sheet is None or not sheet.title:
        return None
    node = make("title_block")
    t = sheet.title
    for key, field in (("Title", "title"), ("Date", "date"), ("Revision", "rev"),
                       ("CompanyName", "company")):
        if t.get(key):
            node.append(make(field, t[key]))
    if t.get("DocumentNumber"):
        node.append(make("comment", 1, "Document " + t["DocumentNumber"]))
    return node


class _Points:
    """Connection points of a sheet, to tell which end of a port is wired."""

    def __init__(self, sheet: Sheet):
        self.segments = [(a, b) for w in sheet.wires for a, b in zip(w, w[1:])]
        self.spots = [sy.pin_endpoint(p) for part in sheet.parts for p in part.pins]
        self.spots += list(sheet.junctions)

    def touches(self, pt) -> bool:
        x, y = pt
        for (ax, ay), (bx, by) in self.segments:
            if abs((bx - ax) * (y - ay) - (by - ay) * (x - ax)) < 1e-6 and \
                    min(ax, bx) - 1e-6 <= x <= max(ax, bx) + 1e-6 and \
                    min(ay, by) - 1e-6 <= y <= max(ay, by) + 1e-6:
                return True
        return any(math.dist(pt, s) < 1e-6 for s in self.spots)


def _port_anchor(rec, points: _Points) -> tuple[tuple[float, float], int]:
    """(connection point in Altium mm, KiCad label angle) of a port."""
    x, y = location(rec)
    width = rec.mm("WIDTH")
    vertical = rec.int("STYLE") >= 4
    far = (x, y + width) if vertical else (x + width, y)
    if points.touches(far) and not points.touches((x, y)):
        return far, (270 if vertical else 180)
    return (x, y), (90 if vertical else 0)


def _label_justify(angle: int) -> sexpr.SExpr:
    return make("justify", Sym("left" if angle in (0, 90) else "right"))


def _label(kind: str, name: str, at, angle: int, size: float, shape: str | None, uid: str):
    node = make(kind, name)
    if shape:
        node.append(make("shape", Sym(shape)))
    node.append(make("at", at[0], at[1], angle))
    eff = make("effects", make("font", make("size", size, size)))
    if kind == "label":
        eff.append(make("justify", Sym("left" if angle in (0, 90) else "right"), Sym("bottom")))
    else:
        eff.append(_label_justify(angle))
    node.append(eff)
    node.append(make("uuid", uid))
    return node


# --- power symbols -------------------------------------------------------

_STEM = 2.54


def _power_shape(style: int) -> list[list[tuple[float, float]]]:
    """Polylines of a power port pointing +x (Y-up), stem from the origin."""
    L = _STEM
    stem = [[(0, 0), (L, 0)]]
    if style in (2, 10):  # bar
        return stem + [[(L, -1.27), (L, 1.27)]]
    if style in (1, 7):  # arrow
        return stem + [[(L, -0.89), (L + 1.52, 0), (L, 0.89), (L, -0.89)]]
    if style == 3:  # wave
        return stem + [[(L + 0.4 * math.sin(i * math.pi / 4), -1.27 + i * 2.54 / 8) for i in range(9)]]
    if style in (4, 8):  # power ground
        return stem + [[(L, -1.27), (L, 1.27)], [(L + 0.6, -0.8), (L + 0.6, 0.8)],
                       [(L + 1.2, -0.3), (L + 1.2, 0.3)]]
    if style == 5:  # signal ground
        return stem + [[(L, -1.27), (L, 1.27), (L + 1.52, 0), (L, -1.27)]]
    if style in (6, 9):  # earth
        return stem + [[(L, -1.27), (L, 1.27)]] + [[(L, y), (L + 0.8, y - 0.6)] for y in (-1.27, 0, 1.27)]
    if style == 0:  # circle: drawn by the caller
        return stem
    return stem + [[(L, -1.27), (L, 1.27)]]


def _rotate(pts, orientation: int):
    a = math.radians(90 * orientation)
    c, s = round(math.cos(a)), round(math.sin(a))
    return [(sy.r6(x * c - y * s), sy.r6(x * s + y * c)) for x, y in pts]


def power_symbol(net: str, style: int, orientation: int) -> tuple[str, sexpr.SExpr]:
    """(lib id, lib symbol) of a power port pointing in Altium `orientation`."""
    base = re.sub(r"[^A-Za-z0-9_.+-]", "_", net) or "net"
    name = f"{base}_{style}_{orientation}"
    lib_id = "altium_power:" + name
    eff = sy.effects({}, 0, size=sy.DEFAULT_TEXT)
    node = make("symbol", lib_id, make("power"), make("pin_numbers", make("hide", True)),
                make("pin_names", make("offset", 0), make("hide", True)),
                make("exclude_from_sim", False), make("in_bom", True), make("on_board", True),
                make("property", "Reference", "#PWR", make("at", 0, 0, 0),
                     sy.effects({}, 0, size=sy.DEFAULT_TEXT, hide=True)),
                make("property", "Value", net, make("at", 0, 0, 0), eff))
    body = make("symbol", f"{name}_0_1")
    for line in _power_shape(style):
        body.append(make("polyline", sy.pts(_rotate(line, orientation)),
                         make("stroke", make("width", 0.254), make("type", Sym("default"))),
                         make("fill", make("type", Sym("none")))))
    if style == 0:
        (cx, cy), = _rotate([(_STEM + 0.635, 0)], orientation)
        body.append(make("circle", make("center", cx, cy), make("radius", 0.635),
                         make("stroke", make("width", 0.254), make("type", Sym("default"))),
                         make("fill", make("type", Sym("none")))))
    node.append(body)
    pins = make("symbol", f"{name}_1_1",
                make("pin", Sym("power_in"), Sym("line"), make("at", 0, 0, 0), make("length", 0),
                     make("hide", True), make("name", net, eff), make("number", "1", eff)))
    node.append(pins)
    node.append(make("embedded_fonts", False))
    return lib_id, node


# --- images ---------------------------------------------------------------

def _png(data: bytes) -> tuple[bytes, int, int] | None:
    """PNG bytes plus pixel size, at 300 dpi so KiCad's scale 1 is that size."""
    try:
        from PIL import Image  # noqa: PLC0415 - optional
    except ImportError:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            return data, w, h
        return None
    try:
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.save(buf, format="PNG", dpi=(300, 300))
        return buf.getvalue(), img.width, img.height
    except Exception:  # noqa: BLE001 - any undecodable image is reported, not fatal
        return None


# --- the writer -----------------------------------------------------------

class _SheetWriter:
    def __init__(self, design: Design, refs, footprint_fields, version: int):
        self.d = design
        self.refs = refs
        self.fp = footprint_fields
        self.version = version
        self.notes: Counter = Counter()
        self.not_converted: Counter = Counter()
        self.pwr = 0
        self.issues: list[dict] = []

    # Which nodes use a sheet file, and in which order pages are numbered.
    def plan(self):
        self.uses: dict[int, list[SheetNode]] = defaultdict(list)
        self.page: dict[str, int] = {}
        for i, node in enumerate(self.d.root.walk()):
            self.page[node.kicad_path] = i + 1
            if node.sheet is not None:
                self.uses[id(node.sheet)].append(node)

    def file_name(self, sheet: Sheet | None) -> str:
        if sheet is None or (self.d.root.sheet is sheet):
            return self.d.project_name + ".kicad_sch"
        return sheet.stem + ".kicad_sch"

    def write_all(self, out_dir: Path) -> list[str]:
        self.plan()
        files = []
        written: set[int] = set()
        for node in self.d.root.walk():
            if node is not self.d.root and (node.sheet is None or id(node.sheet) in written):
                continue
            if node.sheet is not None:
                written.add(id(node.sheet))
            path = out_dir / self.file_name(node.sheet)
            path.write_text(sexpr.dumps(self.sheet_doc(node)), encoding="utf-8")
            files.append(str(path))
        return files

    def sheet_doc(self, node: SheetNode) -> sexpr.SExpr:
        sheet = node.sheet
        is_root = node is self.d.root
        file_uuid = self.d.root_uuid if is_root else _uid("file", sheet.stem)
        doc = make("kicad_sch", make("version", self.version), make("generator", "kicad-mcp"),
                   make("generator_version", "10.0"), make("uuid", file_uuid), _paper(sheet))
        tb = _title_block(sheet)
        if tb:
            doc.append(tb)
        libs = make("lib_symbols")
        doc.append(libs)
        if sheet is not None:
            self.sheet_items(sheet, doc, libs)
        doc += self.sheet_symbols(node, sheet)
        if is_root:
            doc.append(make("sheet_instances", make("path", "/", make("page", "1"))))
        doc.append(make("embedded_fonts", False))
        return doc

    # Sheet symbols live in the parent file, with one instance per use of that file.
    def sheet_symbols(self, node: SheetNode, sheet: Sheet | None) -> list[sexpr.SExpr]:
        out = []
        parents = self.uses.get(id(sheet), [node]) if sheet is not None else [node]
        flip = sy.Flip(sheet.height_mm) if sheet is not None else sy.Flip(210)
        for i, child in enumerate(node.children):
            instances = []
            for parent in parents:
                twin = next((c for c in parent.children if c.kicad_uuid == child.kicad_uuid), None)
                if twin is not None:
                    instances.append(make("path", parent.kicad_path,
                                          make("page", str(self.page.get(twin.kicad_path, 2)))))
            out.append(self.sheet_node(child, flip, i, instances))
        return out

    def sheet_node(self, child: SheetNode, flip: sy.Flip, i: int, instances) -> sexpr.SExpr:
        sym = child.sheet_symbol
        target = (child.sheet.stem if child.sheet else Path(child.file_name).stem) + ".kicad_sch"
        name = sym.name if sym else child.sheet.stem
        if sym is not None:
            x, y = flip.pt(sym.x, sym.y)
            w, h = sy.r6(sym.width), sy.r6(sym.height)
        else:  # a flat design's synthesized sheet symbol
            x, y, w, h = 25.4 + (i % 4) * 63.5, 38.1 + (i // 4) * 50.8, 50.8, 25.4
        node = make("sheet", make("at", x, y), make("size", w, h), make("exclude_from_sim", False),
                    make("in_bom", True), make("on_board", True), make("dnp", False),
                    make("stroke", make("width", 0.1524), make("type", Sym("solid"))),
                    make("fill", make("color", 0, 0, 0, 0.0)), make("uuid", child.kicad_uuid))
        size = sy.DEFAULT_TEXT
        node.append(make("property", "Sheetname", name, make("at", x, sy.r6(y - 0.7116), 0),
                         make("effects", make("font", make("size", size, size)),
                              make("justify", Sym("left"), Sym("bottom")))))
        node.append(make("property", "Sheetfile", target, make("at", x, sy.r6(y + h + 0.5846), 0),
                         make("effects", make("font", make("size", size, size)),
                              make("justify", Sym("left"), Sym("top")), make("hide", True))))
        if sym is not None:
            for e in sym.entries:
                d = e.distance_mm
                px, py, angle = {0: (x, y + d, 180), 1: (x + w, y + d, 0),
                                 2: (x + d, y, 90), 3: (x + d, y + h, 270)}.get(e.side, (x, y + d, 180))
                shape = IO_SHAPES.get(e.io_type, "passive")
                node.append(make("pin", overbar(e.name), Sym(shape), make("at", sy.r6(px), sy.r6(py), angle),
                                 make("uuid", _uid("entry", child.kicad_uuid, e.uid or e.name)),
                                 make("effects", make("font", make("size", size, size)),
                                      make("justify", Sym("left" if angle in (180, 90) else "right")))))
        if instances:
            node.append(make("instances", make("project", self.d.project_name, *instances)))
        return node

    def sheet_items(self, sheet: Sheet, doc, libs) -> None:
        flip = sy.Flip(sheet.height_mm)
        stem = sheet.stem
        nodes = self.uses[id(sheet)]
        is_root_sheet = self.d.root.sheet is sheet
        mode = self.d.mode
        notes: list[str] = []

        # Symbols, grouped into multi-unit parts by designator and library reference.
        groups: dict[tuple, list[Part]] = defaultdict(list)
        for part in sheet.parts:
            key = (part.designator, part.libref) if part.designator and part.part_count > 1 else (id(part),)
            groups[key].append(part)
        lib_by_key: dict[str, str] = {}
        used_names: set[str] = set()
        seen_uids: set[str] = set()
        placed = []
        for units in groups.values():
            units = sorted(units, key=lambda u: u.part_id)
            key = sy.symbol_key(units)
            if key not in lib_by_key:
                name = sy.lib_name(units[0].libref)
                n, candidate = 1, name
                while candidate in used_names:
                    n += 1
                    candidate = f"{name}_{n}"
                used_names.add(candidate)
                lib_by_key[key] = candidate
                libs.append(sy.lib_symbol(candidate, units, sheet.fonts, notes))
            for u in units:
                if u.uid and u.uid not in seen_uids:
                    seen_uids.add(u.uid)
                    sid = altium_kiid(u.uid)
                else:
                    sid = _uid("part", stem, u.rec.index)
                    self.issues.append({"kind": "missing_or_duplicate_unique_id", "sheet": stem,
                                        "designator": u.designator})
                instances = [(self.d.project_name, n.kicad_path, self.refs.get((n.kicad_path, u.uid), u.designator))
                             for n in nodes]
                fp = next((self.fp[r] for _, _, r in instances if r in self.fp), "")
                placed.append(sy.placed_symbol("altium:" + lib_by_key[key], u, u.part_id, sid,
                                               instances, fp, sheet.fonts, flip))

        # Power ports.
        power_libs: dict[str, sexpr.SExpr] = {}
        for i, r in enumerate(sheet.power_ports):
            net = overbar(r.text("TEXT"))
            lib_id, lib = power_symbol(net, r.int("STYLE"), r.int("ORIENTATION"))
            power_libs.setdefault(lib_id, lib)
            x, y = flip.pt(*location(r))
            inst = []
            for n in nodes:
                self.pwr += 1
                inst.append(make("path", n.kicad_path, make("reference", f"#PWR{self.pwr:04d}"), make("unit", 1)))
            dx, dy = {0: (4.2, 0), 1: (0, -4.2), 2: (-4.2, 0), 3: (0, 4.2)}.get(r.int("ORIENTATION") % 4, (0, -4.2))
            placed.append(make(
                "symbol", make("lib_id", lib_id), make("at", x, y, 0), make("unit", 1),
                make("exclude_from_sim", False), make("in_bom", True), make("on_board", True),
                make("dnp", False), make("uuid", _uid("power", stem, r.index)),
                make("property", "Reference", "#PWR", make("at", x, y, 0),
                     sy.effects({}, 0, size=sy.DEFAULT_TEXT, hide=True)),
                make("property", "Value", net, make("at", sy.r6(x + dx), sy.r6(y + dy), 0),
                     sy.effects(sheet.fonts, r.int("FONTID", 1), 4, hide=not r.bool("SHOWNETNAME"))),
                make("instances", make("project", self.d.project_name, *inst))))
        for lib in power_libs.values():
            libs.append(lib)
        doc += placed

        # Wiring.
        for wi, wire in enumerate(sheet.wires):
            for si, (a, b) in enumerate(zip(wire, wire[1:])):
                doc.append(make("wire", sy.pts([flip.pt(*a), flip.pt(*b)]), _stroke0(),
                                make("uuid", _uid("wire", stem, wi, si))))
        for bi, bus in enumerate(sheet.buses):
            for si, (a, b) in enumerate(zip(bus, bus[1:])):
                doc.append(make("bus", sy.pts([flip.pt(*a), flip.pt(*b)]), _stroke0(),
                                make("uuid", _uid("bus", stem, bi, si))))
        for r in sheet.bus_entries:
            (x0, y0), (x1, y1) = flip.pt(*location(r)), flip.pt(*location(r, "CORNER"))
            doc.append(make("bus_entry", make("at", x0, y0), make("size", sy.r6(x1 - x0), sy.r6(y1 - y0)),
                            _stroke0(), make("uuid", _uid("busentry", stem, r.index))))
        for ji, j in enumerate(sheet.junctions):
            doc.append(make("junction", make("at", *flip.pt(*j)), make("diameter", 0),
                            make("color", 0, 0, 0, 0), make("uuid", _uid("junction", stem, ji))))
        for ni, p in enumerate(sheet.no_ercs):
            doc.append(make("no_connect", make("at", *flip.pt(*p)), make("uuid", _uid("noerc", stem, ni))))

        # Labels and ports, by Altium's net scope.
        for r in sheet.net_labels:
            x, y = flip.pt(*location(r))
            angle = (r.int("ORIENTATION") % 4) * 90
            kind = "global_label" if mode == "global" else "label"
            shape = "passive" if kind == "global_label" else None
            doc.append(_label(kind, overbar(r.text("TEXT")), (x, y), angle,
                              sy.text_size(sheet.fonts, r.int("FONTID", 1)), shape,
                              _uid("label", stem, r.index)))
        points = _Points(sheet)
        for r in sheet.ports:
            (ax, ay), angle = _port_anchor(r, points)
            x, y = flip.pt(ax, ay)
            if mode == "hierarchical" and not is_root_sheet:
                kind = "hierarchical_label"
            else:
                kind = "global_label"
            doc.append(_label(kind, overbar(r.text("NAME")), (x, y), angle,
                              sy.text_size(sheet.fonts, r.int("FONTID", 1)),
                              IO_SHAPES.get(r.int("IOTYPE"), "passive"), _uid("port", stem, r.index)))

        # Notes, drawings and images.
        images = {name.replace("\\", "/").split("/")[-1].lower(): data for name, data in sheet.images}
        for g in sheet.graphics:
            if g.kind == "image":
                doc += self.image(g, flip, images, stem)
                continue
            for item in sy.graphic_nodes(g, 0.0, 0.0, sheet.fonts, notes, yflip=flip.y):
                if item[0] == Sym("text") or item[0] == Sym("text_box"):
                    item.insert(2, make("exclude_from_sim", False))
                item.append(make("uuid", _uid("graphic", stem, g.rec.index, len(item))))
                doc.append(item)

        for n in notes:
            self.notes[n] += 1
        for kind, count in sheet.unsupported.items():
            self.not_converted[(kind, stem)] += count
        if sheet.binary_records:
            self.not_converted[("BINARY_RECORD", stem)] += sheet.binary_records

    def image(self, g, flip, images, stem) -> list[sexpr.SExpr]:
        rec = g.rec
        name = rec.text("FILENAME").replace("\\", "/").split("/")[-1].lower()
        data = images.get(name) if rec.bool("EMBEDIMAGE") else None
        if data is None:
            self.not_converted[("IMAGE (linked file " + rec.text("FILENAME") + ")", stem)] += 1
            return []
        png = _png(data)
        if png is None:
            self.not_converted[("IMAGE (format not readable: " + name + ")", stem)] += 1
            return []
        blob, w_px, _h_px = png
        (x0, y0), (x1, y1) = flip.pt(*location(rec)), flip.pt(*location(rec, "CORNER"))
        native_w = w_px / 300 * 25.4
        scale = sy.r6(abs(x1 - x0) / native_w) if native_w else 1
        encoded = base64.b64encode(blob).decode("ascii")
        chunks = [encoded[i:i + 76] for i in range(0, len(encoded), 76)]
        return [make("image", make("at", sy.r6((x0 + x1) / 2), sy.r6((y0 + y1) / 2)),
                     make("scale", scale), make("uuid", _uid("image", stem, rec.index)),
                     make("data", *chunks))]


def write_sheets(design: Design, out_dir: Path, refs: dict, footprint_fields: dict[str, str],
                 version: int) -> dict:
    """Write every sheet of `design` into `out_dir`; report what was approximated or left out."""
    out_dir.mkdir(parents=True, exist_ok=True)
    writer = _SheetWriter(design, refs, footprint_fields, version)
    files = writer.write_all(out_dir)
    return {
        "files": files,
        "approximations": [{"what": k, "count": v} for k, v in sorted(writer.notes.items())],
        "not_converted": [{"record": k, "sheet": s, "count": v}
                          for (k, s), v in sorted(writer.not_converted.items())],
        "issues": writer.issues,
    }
