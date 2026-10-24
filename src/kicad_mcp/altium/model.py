"""An Altium sheet as typed objects, resolved from its record owner tree.

Coordinates stay in Altium's frame (millimetres, Y up); the writers flip them.
Geometry-heavy objects keep their record, since each writer reads only the
handful of keys it needs, and copying every field into a dataclass would only
add places for the two to disagree.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .records import Record, read_schdoc_records, read_storage_images

# Record types (Altium's numbering).
COMPONENT, PIN, IEEE_SYMBOL, LABEL, BEZIER, POLYLINE, POLYGON, ELLIPSE = 1, 2, 3, 4, 5, 6, 7, 8
PIECHART, ROUND_RECTANGLE, ELLIPTICAL_ARC, ARC, LINE, RECTANGLE = 9, 10, 11, 12, 13, 14
SHEET_SYMBOL, SHEET_ENTRY, POWER_PORT, PORT, NO_ERC, NET_LABEL, BUS, WIRE = 15, 16, 17, 18, 22, 25, 26, 27
TEXT_FRAME, JUNCTION, IMAGE, SHEET, SHEET_NAME, FILE_NAME, DESIGNATOR = 28, 29, 30, 31, 32, 33, 34
BUS_ENTRY, TEMPLATE, PARAMETER, PARAMETER_SET = 37, 39, 41, 43
IMPLEMENTATION_LIST, IMPLEMENTATION, MAP_DEFINER_LIST, MAP_DEFINER, IMPL_PARAMS = 44, 45, 46, 47, 48
NOTE, COMPILE_MASK, BLANKET, HYPERLINK = 209, 211, 225, 226

GRAPHIC_KINDS = {
    LABEL: "label", BEZIER: "bezier", POLYLINE: "polyline", POLYGON: "polygon",
    ELLIPSE: "ellipse", ROUND_RECTANGLE: "roundrect", ELLIPTICAL_ARC: "ellipticalarc",
    ARC: "arc", LINE: "line", RECTANGLE: "rect", TEXT_FRAME: "textframe", NOTE: "note",
    IMAGE: "image",
}

UNSUPPORTED = {
    IEEE_SYMBOL: "IEEE_SYMBOL", PIECHART: "PIECHART", PARAMETER_SET: "PARAMETER_SET",
    COMPILE_MASK: "COMPILE_MASK", 215: "HARNESS_CONNECTOR", 216: "HARNESS_ENTRY",
    217: "HARNESS_TYPE", 218: "SIGNAL_HARNESS", BLANKET: "BLANKET", HYPERLINK: "HYPERLINK",
}

#: Records that only carry bookkeeping for others; reading them is enough.
_BOOKKEEPING = {SHEET, SHEET_NAME, FILE_NAME, DESIGNATOR, PARAMETER, TEMPLATE,
                IMPLEMENTATION_LIST, IMPLEMENTATION, MAP_DEFINER_LIST, MAP_DEFINER, IMPL_PARAMS}

#: Altium SHEETSTYLE -> (KiCad paper name, width mm, height mm), landscape.
PAPER = {
    0: ("A4", 297, 210), 1: ("A3", 420, 297), 2: ("A2", 594, 420), 3: ("A1", 841, 594),
    4: ("A0", 1189, 841), 5: ("A", 279.4, 215.9), 6: ("B", 431.8, 279.4),
    7: ("C", 558.8, 431.8), 8: ("D", 863.6, 558.8), 9: ("E", 1117.6, 863.6),
    10: ("USLetter", 279.4, 215.9), 11: ("USLegal", 355.6, 215.9), 12: ("A3", 420, 297),
    13: ("A", 279.4, 215.9), 14: ("B", 431.8, 279.4), 15: ("C", 558.8, 431.8),
    16: ("D", 863.6, 558.8), 17: ("E", 1117.6, 863.6),
}

TITLE_FIELDS = ("Title", "Revision", "Date", "DocumentNumber", "CompanyName",
                "SheetNumber", "SheetTotal")


@dataclass
class Font:
    size_pt: float
    bold: bool = False
    italic: bool = False
    rotation: int = 0
    name: str = ""


@dataclass
class Pin:
    name: str
    number: str
    electrical: int
    orientation: int
    x: float
    y: float
    length: float
    hidden: bool
    show_name: bool
    show_number: bool
    part: int
    mode: int
    rec: Record


@dataclass
class Graphic:
    kind: str
    rec: Record
    part: int = -1
    mode: int = 0


@dataclass
class Param:
    name: str
    text: str
    x: float
    y: float
    hidden: bool
    font_id: int
    orientation: int
    justification: int
    rec: Record


@dataclass
class Part:
    uid: str
    designator: str
    libref: str
    part_id: int
    part_count: int
    mode_count: int
    x: float
    y: float
    orientation: int
    mirrored: bool
    rec: Record
    pins: list[Pin] = field(default_factory=list)
    graphics: list[Graphic] = field(default_factory=list)
    params: dict[str, Param] = field(default_factory=dict)
    designator_param: Param | None = None
    footprints: list[str] = field(default_factory=list)

    @property
    def value(self) -> str:
        comment = self.params.get("Comment")
        if comment is None:
            return ""
        text = comment.text
        # "=Value" means "show the parameter called Value".
        if text.startswith("="):
            ref = self.param(text[1:])
            return ref.text if ref else ""
        return "" if text == "*" else text

    def param(self, name: str) -> Param | None:
        for key, p in self.params.items():
            if key.lower() == name.lower():
                return p
        return None


@dataclass
class SheetEntry:
    uid: str
    name: str
    side: int
    distance_mm: float
    io_type: int
    rec: Record


@dataclass
class SheetSymbol:
    uid: str
    x: float
    y: float
    width: float
    height: float
    rec: Record
    name: str = ""
    file_name: str = ""
    entries: list[SheetEntry] = field(default_factory=list)
    name_param: Record | None = None
    file_param: Record | None = None


@dataclass
class Sheet:
    path: Path
    stem: str
    fonts: dict[int, Font]
    paper: str
    width_mm: float
    height_mm: float
    portrait: bool
    title: dict[str, str]
    parts: list[Part] = field(default_factory=list)
    wires: list[list[tuple[float, float]]] = field(default_factory=list)
    buses: list[list[tuple[float, float]]] = field(default_factory=list)
    bus_entries: list[Record] = field(default_factory=list)
    junctions: list[tuple[float, float]] = field(default_factory=list)
    net_labels: list[Record] = field(default_factory=list)
    power_ports: list[Record] = field(default_factory=list)
    ports: list[Record] = field(default_factory=list)
    no_ercs: list[tuple[float, float]] = field(default_factory=list)
    sheet_symbols: list[SheetSymbol] = field(default_factory=list)
    graphics: list[Graphic] = field(default_factory=list)
    images: list[tuple[str, bytes]] = field(default_factory=list)
    unsupported: Counter = field(default_factory=Counter)
    binary_records: int = 0


def location(rec: Record, prefix: str = "LOCATION") -> tuple[float, float]:
    return rec.mm(prefix + ".X"), rec.mm(prefix + ".Y")


def vertices(rec: Record) -> list[tuple[float, float]]:
    """The X1/Y1..Xn/Yn points of a wire, bus, polyline, polygon or bezier."""
    return [(rec.mm(f"X{i}"), rec.mm(f"Y{i}")) for i in range(1, rec.int("LOCATIONCOUNT") + 1)]


def _fonts(sheet_rec: Record | None) -> dict[int, Font]:
    fonts: dict[int, Font] = {}
    if sheet_rec is None:
        return {1: Font(10)}
    for i in range(1, sheet_rec.int("FONTIDCOUNT") + 1):
        fonts[i] = Font(size_pt=float(sheet_rec.int(f"SIZE{i}", 10)),
                        bold=sheet_rec.bool(f"BOLD{i}"), italic=sheet_rec.bool(f"ITALIC{i}"),
                        rotation=sheet_rec.int(f"ROTATION{i}"),
                        name=sheet_rec.text(f"FONTNAME{i}"))
    return fonts or {1: Font(10)}


def _paper(sheet_rec: Record | None) -> tuple[str, float, float, bool]:
    if sheet_rec is None:
        return "A4", 297.0, 210.0, False
    portrait = sheet_rec.int("WORKSPACEORIENTATION") == 1
    if sheet_rec.bool("USECUSTOMSHEET"):
        w, h = sheet_rec.mm("CUSTOMX"), sheet_rec.mm("CUSTOMY")
        return "User", w, h, False
    name, w, h = PAPER.get(sheet_rec.int("SHEETSTYLE"), PAPER[0])
    if portrait:
        w, h = h, w
    return name, float(w), float(h), portrait


def _param(rec: Record) -> Param:
    x, y = location(rec)
    return Param(name=rec.text("NAME"), text=rec.text("TEXT"), x=x, y=y,
                 hidden=rec.bool("ISHIDDEN"), font_id=rec.int("FONTID", 1),
                 orientation=rec.int("ORIENTATION"), justification=rec.int("JUSTIFICATION"),
                 rec=rec)


def _pin(rec: Record) -> Pin:
    conglomerate = rec.int("PINCONGLOMERATE")
    x, y = location(rec)
    return Pin(name=rec.text("NAME"), number=rec.text("DESIGNATOR"),
               electrical=rec.int("ELECTRICAL"), orientation=conglomerate & 0x03,
               x=x, y=y, length=rec.mm("PINLENGTH"), hidden=bool(conglomerate & 0x04),
               show_name=bool(conglomerate & 0x08), show_number=bool(conglomerate & 0x10),
               part=rec.int("OWNERPARTID", -1), mode=rec.int("OWNERPARTDISPLAYMODE"), rec=rec)


def load_sheet(path: str | Path) -> Sheet:
    p = Path(path)
    _header, recs, binary = read_schdoc_records(p)
    sheet_rec = next((r for r in recs if r.type == SHEET), None)
    paper, w, h, portrait = _paper(sheet_rec)
    sheet = Sheet(path=p, stem=p.stem, fonts=_fonts(sheet_rec), paper=paper, width_mm=w,
                  height_mm=h, portrait=portrait, title={}, binary_records=binary)
    by_index = {r.index: r for r in recs}

    parts: dict[int, Part] = {}
    symbols: dict[int, SheetSymbol] = {}
    impl_lists: dict[int, int] = {}  # implementation list index -> component index
    templates = {r.index for r in recs if r.type == TEMPLATE}

    for r in recs:
        if r.type == COMPONENT:
            x, y = location(r)
            parts[r.index] = Part(
                uid=r.text("UNIQUEID"), designator="", libref=r.text("LIBREFERENCE"),
                part_id=r.int("CURRENTPARTID", 1), part_count=max(1, r.int("PARTCOUNT", 2) - 1),
                mode_count=max(1, r.int("DISPLAYMODECOUNT", 1)), x=x, y=y,
                orientation=r.int("ORIENTATION"), mirrored=r.bool("ISMIRRORED"), rec=r)
        elif r.type == SHEET_SYMBOL:
            x, y = location(r)
            symbols[r.index] = SheetSymbol(uid=r.text("UNIQUEID"), x=x, y=y,
                                           width=r.mm("XSIZE"), height=r.mm("YSIZE"), rec=r)

    def owner_part(r: Record) -> Part | None:
        return parts.get(r.owner) if r.owner is not None else None

    def belongs(part: Part, r: Record) -> bool:
        """Altium keeps every part's primitives on each placed part; show the current one."""
        owner_part_id = r.int("OWNERPARTID", -1)
        if owner_part_id not in (-1, 0, part.part_id):
            return False
        return r.int("OWNERPARTDISPLAYMODE") == part.rec.int("DISPLAYMODE")

    for r in recs:
        t = r.type
        part = owner_part(r)
        if t == -1 and not r:
            continue  # a binary record, counted already
        if t == PIN:
            if part is not None and belongs(part, r):
                part.pins.append(_pin(r))
        elif t == DESIGNATOR and part is not None:
            part.designator = r.text("TEXT")
            part.designator_param = _param(r)
        elif t == PARAMETER:
            if part is not None:
                part.params[r.text("NAME")] = _param(r)
            elif r.owner is None or r.owner in templates:
                name = r.text("NAME")
                for title_name in TITLE_FIELDS:
                    if name.lower() == title_name.lower() and r.text("TEXT") not in ("", "*"):
                        sheet.title[title_name] = r.text("TEXT")
        elif t == IMPLEMENTATION_LIST and part is not None:
            impl_lists[r.index] = r.owner
        elif t == IMPLEMENTATION:
            comp = parts.get(impl_lists.get(r.owner, -1))
            if comp is not None and r.text("MODELTYPE").upper() == "PCBLIB":
                name = r.text("MODELNAME")
                if r.bool("ISCURRENT"):
                    comp.footprints.insert(0, name)
                else:
                    comp.footprints.append(name)
        elif t in GRAPHIC_KINDS:
            graphic = Graphic(GRAPHIC_KINDS[t], r, r.int("OWNERPARTID", -1),
                              r.int("OWNERPARTDISPLAYMODE"))
            if part is not None:
                if belongs(part, r):
                    part.graphics.append(graphic)
            elif r.owner is None or r.owner in templates or r.owner not in by_index:
                sheet.graphics.append(graphic)
        elif t == SHEET_ENTRY and r.owner in symbols:
            symbols[r.owner].entries.append(SheetEntry(
                uid=r.text("UNIQUEID"), name=r.text("NAME"), side=r.int("SIDE"),
                distance_mm=r.mm1("DISTANCEFROMTOP"), io_type=r.int("IOTYPE"), rec=r))
        elif t == SHEET_NAME and r.owner in symbols:
            symbols[r.owner].name = r.text("TEXT")
            symbols[r.owner].name_param = r
        elif t == FILE_NAME and r.owner in symbols:
            symbols[r.owner].file_name = r.text("TEXT")
            symbols[r.owner].file_param = r
        elif t == WIRE:
            sheet.wires.append(vertices(r))
        elif t == BUS:
            sheet.buses.append(vertices(r))
        elif t == BUS_ENTRY:
            sheet.bus_entries.append(r)
        elif t == JUNCTION:
            sheet.junctions.append(location(r))
        elif t == NET_LABEL:
            sheet.net_labels.append(r)
        elif t == POWER_PORT:
            sheet.power_ports.append(r)
        elif t == PORT:
            sheet.ports.append(r)
            if r.text("HARNESSTYPE"):
                sheet.unsupported["HARNESS_PORT"] += 1  # converted as a plain port
        elif t == NO_ERC:
            sheet.no_ercs.append(location(r))
        elif t in UNSUPPORTED:
            sheet.unsupported[UNSUPPORTED[t]] += 1
        elif t in _BOOKKEEPING or t in (COMPONENT, SHEET_SYMBOL):
            pass
        else:
            sheet.unsupported[f"RECORD_{t}"] += 1

    sheet.parts = list(parts.values())
    sheet.sheet_symbols = list(symbols.values())
    sheet.images = read_storage_images(p)
    return sheet
