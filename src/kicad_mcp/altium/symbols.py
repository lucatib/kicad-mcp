"""Altium parts as KiCad symbols, rebuilt from the graphics placed on the sheet.

A .SchDoc stores each placed part's pins and drawing already rotated and
mirrored into sheet coordinates. The library symbol is that geometry relative
to the part's anchor, and the instance sits at the anchor unrotated: every pin
lands exactly where Altium drew it, with no rotation or mirror convention to
get wrong between the two programs. KiCad's library frame is Y-up like
Altium's, so library coordinates need no flip; only the instance's position
does.
"""

from __future__ import annotations

import hashlib
import math
import re

from .. import sexpr
from ..sexpr import Sym, make
from .model import Font, Graphic, Part, Pin, location, vertices

#: Altium stores font sizes in 10-mil units; KiCad's importer halves them.
PT_TO_MM = 0.127
GRID = 1.27
DEFAULT_TEXT = 1.27

#: Altium's default symbol body colour (light yellow), which KiCad draws as "background".
ALTIUM_BODY_FILL = 11599871

_DIRS = {0: (1.0, 0.0), 1: (0.0, 1.0), 2: (-1.0, 0.0), 3: (0.0, -1.0)}
_PIN_ANGLE = {0: 180, 1: 270, 2: 0, 3: 90}
PIN_TYPES = {0: "input", 1: "bidirectional", 2: "output", 3: "open_collector", 4: "passive",
             5: "tri_state", 6: "open_emitter", 7: "power_in"}


class Flip:
    """Altium Y-up sheet millimetres to KiCad Y-down, about a grid-aligned height.

    The height is the page height truncated to KiCad's 1.27 mm connection
    grid, so pins on Altium's 100 mil grid stay on KiCad's.
    """

    def __init__(self, page_height_mm: float):
        self.height = math.floor(page_height_mm / GRID + 1e-9) * GRID

    def y(self, y: float) -> float:
        return round(self.height - y, 6)

    def pt(self, x: float, y: float) -> tuple[float, float]:
        return round(x, 6), self.y(y)


def r6(v: float) -> float:
    return round(v + 0.0, 6)


def color_rgb(bgr: int) -> tuple[int, int, int]:
    return bgr & 0xFF, (bgr >> 8) & 0xFF, (bgr >> 16) & 0xFF


def text_size(fonts: dict[int, Font], font_id: int) -> float:
    font = fonts.get(font_id)
    if font is None:
        return DEFAULT_TEXT
    return max(0.5, r6(font.size_pt * PT_TO_MM))


def effects(fonts: dict[int, Font], font_id: int, justification: int | None = None,
            hide: bool = False, size: float | None = None) -> sexpr.SExpr:
    """KiCad (effects ...) for an Altium font id and 0-8 justification."""
    s = size if size is not None else text_size(fonts, font_id)
    font = make("font", make("size", s, s))
    f = fonts.get(font_id)
    if f is not None and f.bold:
        font.append(make("bold", True))
    if f is not None and f.italic:
        font.append(make("italic", True))
    out = make("effects", font)
    if justification is not None:
        h, v = justification % 3, justification // 3
        tokens = [Sym(t) for t in ({0: "left", 2: "right"}.get(h), {0: "bottom", 2: "top"}.get(v)) if t]
        if tokens:
            out.append(make("justify", *tokens))
    if hide:
        out.append(make("hide", True))
    return out


def upright(orientation: int, justification: int) -> tuple[int, int]:
    """KiCad text reads at 0 or 90 degrees; turn 180/270 into those, flipping the anchor."""
    orientation %= 4
    if orientation >= 2:
        h, v = justification % 3, justification // 3
        return orientation - 2, (2 - v) * 3 + (2 - h)
    return orientation, justification


def stroke(rec, width_key: str = "LINEWIDTH") -> sexpr.SExpr:
    return make("stroke", make("width", r6(rec.int(width_key) * 0.254)), make("type", Sym("default")))


def fill(rec, closed: bool = True) -> sexpr.SExpr:
    if not closed or not rec.bool("ISSOLID") or rec.bool("TRANSPARENT"):
        return make("fill", make("type", Sym("none")))
    area = rec.int("AREACOLOR")
    if area == ALTIUM_BODY_FILL:
        return make("fill", make("type", Sym("background")))
    r, g, b = color_rgb(area)
    return make("fill", make("type", Sym("color")), make("color", r, g, b, 1))


def pts(points) -> sexpr.SExpr:
    return make("pts", *[make("xy", r6(x), r6(y)) for x, y in points])


def ellipse_points(cx, cy, rx, ry, a0=0.0, a1=360.0, n=32) -> list[tuple[float, float]]:
    steps = max(4, int(n * abs(a1 - a0) / 360))
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
             cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)]


def arc_node(cx, cy, r, a0, a1, st, fl) -> sexpr.SExpr:
    if a1 <= a0:
        a1 += 360
    if abs(a1 - a0 - 360) < 1e-6 or abs(a1 - a0) < 1e-6:
        return make("circle", make("center", r6(cx), r6(cy)), make("radius", r6(r)), st, fl)
    am = (a0 + a1) / 2

    def at(a):
        return r6(cx + r * math.cos(math.radians(a))), r6(cy + r * math.sin(math.radians(a)))

    return make("arc", make("start", *at(a0)), make("mid", *at(am)), make("end", *at(a1)), st, fl)


def graphic_nodes(g: Graphic, dx: float, dy: float, fonts, notes: list[str],
                  yflip=None) -> list[sexpr.SExpr]:
    """KiCad nodes for one Altium graphic, offset by (-dx, -dy).

    `yflip` maps a Y value into the target frame: identity for library
    symbols (Y-up), the sheet flip for schematic graphics (Y-down).
    """
    rec = g.rec
    sign = -1 if yflip else 1  # Y-down frames reverse angles

    def p(x, y):
        return r6(x - dx), (r6(y - dy) if yflip is None else yflip(y - dy))

    k = g.kind
    st = stroke(rec)
    if k == "line":
        return [make("polyline", pts([p(*location(rec)), p(*location(rec, "CORNER"))]), st, fill(rec, False))]
    if k in ("polyline", "polygon"):
        vs = [p(x, y) for x, y in vertices(rec)]
        if len(vs) < 2:
            return []
        closed = k == "polygon"
        if closed:
            vs.append(vs[0])
        return [make("polyline", pts(vs), st, fill(rec, closed))]
    if k in ("rect", "roundrect"):
        if k == "roundrect":
            notes.append("rounded rectangle drawn with square corners")
        (x0, y0), (x1, y1) = p(*location(rec)), p(*location(rec, "CORNER"))
        return [make("rectangle", make("start", x0, y0), make("end", x1, y1), st, fill(rec))]
    if k in ("arc", "ellipticalarc", "ellipse"):
        cx, cy = p(*location(rec))
        rx = rec.mm("RADIUS")
        ry = rec.mm("SECONDARYRADIUS") if k != "arc" else rx
        if k == "ellipse":
            a0, a1 = 0.0, 360.0
        else:
            a0, a1 = rec.float("STARTANGLE"), rec.float("ENDANGLE")
            if sign < 0:
                a0, a1 = -a1, -a0
        closed = k == "ellipse"
        if abs(rx - ry) < 1e-6:
            return [arc_node(cx, cy, rx, a0, a1, st, fill(rec, closed))]
        if a1 <= a0:
            a1 += 360
        notes.append("ellipse drawn as a polyline")
        poly = ellipse_points(cx, cy, rx, ry, a0, a1)
        return [make("polyline", pts(poly), st, fill(rec, closed))]
    if k == "bezier":
        vs = [p(x, y) for x, y in vertices(rec)]
        return [make("bezier", pts(vs[i:i + 4]), st, fill(rec, False))
                for i in range(0, len(vs) - 3, 3)]
    if k == "label":
        orient, just = upright(rec.int("ORIENTATION"), rec.int("JUSTIFICATION"))
        x, y = p(*location(rec))
        angle = orient * 90 * (10 if not yflip else 1)
        return [make("text", rec.text("TEXT").replace("~1", "\n"), make("at", x, y, angle),
                     effects(fonts, rec.int("FONTID", 1), just))]
    if k in ("textframe", "note"):
        (x0, y0), (x1, y1) = p(*location(rec)), p(*location(rec, "CORNER"))
        left, right = min(x0, x1), max(x0, x1)
        top = max(y0, y1) if not yflip else min(y0, y1)
        h = abs(y1 - y0)
        size_y = -h if not yflip else h
        margin = r6(rec.mm("TEXTMARGIN")) if "TEXTMARGIN" in rec else 0.5
        border = rec.bool("SHOWBORDER") or k == "note"
        bstroke = make("stroke", make("width", 0 if border else -1),
                       make("type", Sym("solid" if border else "default")))
        alignment = rec.int("ALIGNMENT", 1)  # 1 left, 2 right, 3 centre in Altium
        just = {1: 6, 2: 8, 3: 7}.get(alignment, 6)
        text = rec.text("TEXT").replace("~1", "\n")
        return [make("text_box", text, make("at", left, top, 0), make("size", r6(right - left), size_y),
                     make("margins", margin, margin, margin, margin), bstroke, fill(rec),
                     effects(fonts, rec.int("FONTID", 1), just))]
    if k == "image":
        notes.append("image inside a symbol not converted")
        return []
    return []


def pin_endpoint(pin: Pin) -> tuple[float, float]:
    """Where wires connect: Altium's pin location is the body end."""
    dx, dy = _DIRS[pin.orientation]
    return pin.x + dx * pin.length, pin.y + dy * pin.length


def pin_angle(pin: Pin) -> int:
    return _PIN_ANGLE[pin.orientation]


def _pin_shape(pin: Pin) -> str:
    rec = pin.rec
    if rec is None:
        return "line"
    outer_edge, inner_edge = rec.int("SYMBOL_OUTEREDGE"), rec.int("SYMBOL_INNEREDGE")
    outer = rec.int("SYMBOL_OUTER")
    clock = inner_edge == 3
    if outer_edge == 1:
        return "inverted_clock" if clock else "inverted"
    if outer == 4 or outer_edge == 4:
        return "clock_low" if clock else "input_low"
    if outer in (17, 22) or outer_edge == 22:
        return "output_low"
    return "clock" if clock else "line"


def pin_node(pin: Pin, ax: float, ay: float) -> sexpr.SExpr:
    ex, ey = pin_endpoint(pin)
    kind = PIN_TYPES.get(pin.electrical, "unspecified")
    name = pin.name
    if pin.electrical == 7 and pin.hidden:
        # A hidden power pin joins the net Altium names for it; KiCad joins a
        # hidden power input to the net of its name. Without a name it must
        # not join anything, so it becomes passive.
        hidden_net = pin.rec.text("HIDDENNETNAME") if pin.rec is not None else ""
        if hidden_net:
            name = hidden_net
        else:
            kind = "passive"
    node = make("pin", Sym(kind), Sym(_pin_shape(pin)), make("at", r6(ex - ax), r6(ey - ay), pin_angle(pin)),
                make("length", r6(pin.length)))
    if pin.hidden:
        node.append(make("hide", True))
    node.append(make("name", name.replace("\\", ""), effects({}, 0, size=DEFAULT_TEXT)))
    node.append(make("number", pin.number, effects({}, 0, size=DEFAULT_TEXT)))
    return node


def _geometry(part: Part) -> tuple:
    def q(v):
        return round(v - 0.0, 4)

    pins = tuple(sorted((p.number, p.name, p.electrical, p.orientation, q(p.x - part.x), q(p.y - part.y),
                         q(p.length), p.hidden) for p in part.pins))
    graphics = tuple(sorted((g.kind, tuple(sorted((k, v) for k, v in g.rec.items()
                                                  if k not in ("OWNERINDEX", "INDEXINSHEET", "UNIQUEID")
                                                  and not k.startswith(("LOCATION", "CORNER", "X", "Y")))),
                             tuple(q(c) for c in _graphic_coords(g, part))) for g in part.graphics))
    return pins, graphics


def _graphic_coords(g: Graphic, part: Part) -> list[float]:
    rec = g.rec
    out: list[float] = []
    for prefix in ("LOCATION", "CORNER"):
        if prefix + ".X" in rec:
            x, y = location(rec, prefix)
            out += [x - part.x, y - part.y]
    for x, y in vertices(rec):
        out += [x - part.x, y - part.y]
    return out


def symbol_key(units: list[Part]) -> str:
    """Identical drawings, pins and unit numbers give the same key, wherever they sit."""
    data = repr(sorted((u.part_id, _geometry(u)) for u in units))
    return hashlib.sha1(data.encode()).hexdigest()[:12]


def lib_name(libref: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", libref) or "part"


def lib_symbol(name: str, units: list[Part], fonts: dict[int, Font],
               notes: list[str] | None = None) -> sexpr.SExpr:
    """`(symbol "altium:<name>" ...)` with one sub-symbol per placed unit."""
    notes = notes if notes is not None else []
    node = make("symbol", "altium:" + name)
    all_pins = [p for u in units for p in u.pins]
    if all_pins and not any(p.show_number for p in all_pins):
        node.append(make("pin_numbers", make("hide", True)))
    pn = make("pin_names", make("offset", 0.254))
    if all_pins and not any(p.show_name for p in all_pins):
        pn.append(make("hide", True))
    node.append(pn)
    if any(p.show_name for p in all_pins) and not all(p.show_name for p in all_pins):
        notes.append("pin names hidden on some pins only: all shown")
    node += [make("exclude_from_sim", False), make("in_bom", True), make("on_board", True)]
    for field, value, hide in (("Reference", "U", False), ("Value", name, False),
                               ("Footprint", "", True), ("Datasheet", "", True),
                               ("Description", "", True)):
        node.append(make("property", field, value, make("at", 0, 0, 0),
                         effects({}, 0, size=DEFAULT_TEXT, hide=hide)))
    for u in sorted(units, key=lambda u: u.part_id):
        body = make("symbol", f"{name}_{u.part_id}_1")
        for g in u.graphics:
            body += graphic_nodes(g, u.x, u.y, fonts, notes)
        for p in u.pins:
            body.append(pin_node(p, u.x, u.y))
        node.append(body)
    node.append(make("embedded_fonts", False))
    return node


def field_node(name: str, value: str, param, fonts, flip: Flip, default_at, hide: bool) -> sexpr.SExpr:
    if param is not None:
        x, y = flip.pt(param.x, param.y)
        orient, just = upright(param.orientation, param.justification)
        return make("property", name, value, make("at", x, y, orient * 90),
                    effects(fonts, param.font_id, just, hide=hide or param.hidden))
    return make("property", name, value, make("at", *default_at, 0),
                effects({}, 0, size=DEFAULT_TEXT, hide=True))


def placed_symbol(lib_id: str, part: Part, unit: int, uuid: str,
                  instances: list[tuple[str, str, str]], footprint: str, fonts, flip: Flip,
                  altium_footprint: str | None = None) -> sexpr.SExpr:
    """A symbol instance at the part's anchor, unrotated, with its fields."""
    at = flip.pt(part.x, part.y)
    node = make("symbol", make("lib_id", lib_id), make("at", at[0], at[1], 0), make("unit", unit),
                make("exclude_from_sim", False), make("in_bom", True), make("on_board", True),
                make("dnp", False), make("uuid", uuid))
    ref = instances[0][2] if instances else part.designator
    node.append(field_node("Reference", ref, part.designator_param, fonts, flip, at, False))
    value_param = part.param("Comment")
    node.append(field_node("Value", part.value or part.libref, value_param, fonts, flip, at, False))
    node.append(field_node("Footprint", footprint, None, fonts, flip, at, True))
    node.append(field_node("Datasheet", "", None, fonts, flip, at, True))
    skip = {"comment", "designator"}
    if altium_footprint is None:
        altium_footprint = part.footprints[0] if part.footprints else ""
    if altium_footprint:
        node.append(field_node("Altium_Footprint", altium_footprint, None, fonts, flip, at, True))
    for name, param in part.params.items():
        if name.lower() in skip or not name or param.text in ("", "*"):
            continue
        if name in ("Reference", "Value", "Footprint", "Datasheet"):
            name = "Altium_" + name
        node.append(field_node(name, param.text, param, fonts, flip, at, param.hidden))
    paths = []
    for project, path, reference in instances:
        paths.append(make("path", path, make("reference", reference), make("unit", unit)))
    if instances:
        node.append(make("instances", make("project", instances[0][0], *paths)))
    return node
