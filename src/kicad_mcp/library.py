"""Draw symbols into a project's own symbol library.

The other tools read libraries. A part none of them has -- a module bought as
a board, a header wired to match its silkscreen -- needs a symbol first, and
writing one by hand means getting KiCad's pin geometry, unit naming and fields
right, then registering the library in the project's sym-lib-table. This does
both for the usual shape: one unit, a rectangle with pins on its sides, every
pin end on the 2.54 mm grid.

Nothing already written is rewritten. A symbol goes in just before the
library's closing parenthesis, a table entry just before the table's, with the
line endings and indentation the file already uses, so what KiCad or the user
wrote stays byte for byte.
"""

from __future__ import annotations

import math
from pathlib import Path

from . import hierarchy, sexpr
from .errors import ToolInputError
from .generator import GENERATOR, _property
from .sexpr import Sym, make
from .symbols import SymbolIndex, resolve_tables

PITCH = 2.54
PIN_LENGTH = 2.54
#: Gap between a pin's name and the body edge, KiCad's default.
NAME_OFFSET = 1.016
FONT = 1.27
#: Width budgeted per character of a pin name. KiCad's stroke font at 1.27 mm
#: stays inside it, so names on facing sides never meet.
CHAR = 1.27
#: The `.kicad_sym` format KiCad 10.0 writes. A newer KiCad reads it as is.
LIBRARY_VERSION = 20251024

ELECTRICAL_TYPES = (
    "input", "output", "bidirectional", "tri_state", "passive", "free", "unspecified",
    "power_in", "power_out", "open_collector", "open_emitter", "no_connect",
)
SIDES = ("left", "right", "top", "bottom")
PIN_KEYS = ("number", "name", "type", "side")


def create_symbol(
    install,
    project: str | Path,
    library: str,
    name: str,
    pins: list[dict],
    reference: str = "U",
    value: str | None = None,
    footprint: str = "",
    datasheet: str = "",
    description: str = "",
    keywords: str = "",
) -> dict:
    """Draw a one-unit rectangular symbol into `library` of a project.

    `project` is the project's folder or any file in it. `library` is a
    nickname: one in the project's sym-lib-table gets the symbol added to
    its file; a new one becomes `<project>/<library>.kicad_sym`, registered
    in the project's table, which is made if missing. Each pin is a dict of
    PIN_KEYS; see `_layout` for where they go. Refused before anything is
    written: no project, a nickname a global library has (a project library
    would hide it), a name the library already has, and bad pins.
    """
    folder = hierarchy.find_project_dir(project)
    if folder is None:
        raise ToolInputError(
            f"No KiCad project (.kicad_pro) in {Path(project)} or the folders above it.",
            remedy="Pass the folder holding the project's .kicad_pro, or a schematic in it.",
        )
    name, library = _usable(name, "Symbol name"), _usable(library, "Library nickname")
    if not str(reference).isalpha():
        raise ToolInputError(
            f"The reference prefix {reference!r} must be letters only; KiCad adds the number.",
            remedy='Pass a prefix such as "U", "J" or "MOD".',
        )
    placed, half_w, up, down = _layout(pins)

    table_path = folder / "sym-lib-table"
    table = _read(table_path)
    own = {str(sexpr.value(n, "name")) for n in sexpr.children(_parse(table, table_path), "lib")} if table else set()
    register = library not in own
    if register:
        taken = {n.lower(): e for n, e in resolve_tables(install, None, "sym-lib-table").items()}
        if library.lower() in taken:
            raise ToolInputError(
                f"{library} is a global library ({taken[library.lower()].path}); a project library "
                "of that name would hide it in this project.",
                remedy=f"Pick another nickname, such as the project's name, {folder.name}.",
            )
        lib_path = folder / f"{library}.kicad_sym"
    else:
        lib_path = SymbolIndex(install, folder).entries[library].path
        if lib_path is None or lib_path.suffix != ".kicad_sym":
            raise ToolInputError(
                f"The project's {library} library is not a .kicad_sym file: {lib_path}.",
                remedy="Pick a nickname for a KiCad symbol library, or a new one.",
            )

    text = _read(lib_path)
    if text is not None:
        have = {str(n[1]).lower() for n in sexpr.children(_parse(text, lib_path), "symbol") if len(n) > 1}
        if name.lower() in have:
            raise ToolInputError(
                f"{library} already has a symbol {name}; nothing was written.",
                remedy="Pick another name, or place the existing one with add_symbol_to_schematic.",
            )

    node = _symbol(name, placed, half_w, up, down, reference, value if value is not None else name,
                   footprint, datasheet, description, keywords)
    # The library first: a table entry pointing at nothing is worse than an
    # unregistered library.
    lib_path.parent.mkdir(parents=True, exist_ok=True)
    lib_path.write_bytes(_with_symbol(text, node).encode("utf-8"))
    if register:
        entry = [make("name", library), make("type", "KiCad"),
                 make("uri", f"${{KIPRJMOD}}/{library}.kicad_sym"), make("options", ""), make("descr", "")]
        table_path.write_bytes(_with_entry(table, entry).encode("utf-8"))
    return {
        "lib_id": f"{library}:{name}",
        "library_file": str(lib_path),
        "created_library": text is None,
        "registered": register,
        "pins": len(placed),
        "body": {"width": round(2 * half_w, 4), "height": round(up + down, 4)},
    }


def _layout(pins: list[dict]) -> tuple[list[tuple[dict, float, float, int]], float, float, float]:
    """Validated pins with their (x, y, angle), and the body's half width and
    how far its edges sit above and below the origin.

    Pins run down the left and right sides from the top, and along the top
    and bottom from the left, in list order, one pitch apart; without any
    `side`, the first half go left and the rest right. The body reaches one
    pitch past the outer pins and clears every name: facing names keep a
    pitch between them, and top and bottom pins sit inward of the side
    pins' names.
    """
    if not pins:
        raise ToolInputError("A symbol needs at least one pin.",
                             remedy='Pass pins such as [{"number": "1", "name": "VCC"}].')
    clean = []
    for i, raw in enumerate(pins, 1):
        if not isinstance(raw, dict):
            raise ToolInputError(f"Pin {i} is not an object.", remedy=f"Each pin takes {', '.join(PIN_KEYS)}.")
        unknown = sorted(set(raw) - set(PIN_KEYS))
        if unknown:
            raise ToolInputError(f"Pin {i}: unknown key {', '.join(unknown)}.",
                                 remedy=f"Each pin takes {', '.join(PIN_KEYS)}.")
        number = str(raw.get("number", "")).strip()
        if not number:
            raise ToolInputError(f"Pin {i} has no number.", remedy="Give every pin its number on the part.")
        kind, side = str(raw.get("type") or "passive"), raw.get("side")
        if kind not in ELECTRICAL_TYPES:
            raise ToolInputError(
                f"Pin {number}: {kind!r} is not a KiCad electrical type; use one of {', '.join(ELECTRICAL_TYPES)}.",
                remedy="Leave type out for passive.",
            )
        if side is not None and side not in SIDES:
            raise ToolInputError(f"Pin {number}: side {side!r} is not one of {', '.join(SIDES)}.",
                                 remedy="Leave side out of every pin to split them left and right.")
        clean.append({"number": number, "name": str(raw.get("name") or "~"), "type": kind, "side": side})

    seen: dict[str, int] = {}
    for p in clean:
        seen[p["number"]] = seen.get(p["number"], 0) + 1
    if dups := [n for n, k in seen.items() if k > 1]:
        raise ToolInputError(f"Pin number {', '.join(dups)} is given more than once.",
                             remedy="Give each pin its own number.")
    sideless = [p["number"] for p in clean if p["side"] is None]
    if sideless and len(sideless) < len(clean):
        raise ToolInputError(f"Pin {', '.join(sideless)} has no side while others do.",
                             remedy="Give every pin a side, or none to split them left and right.")
    if sideless:
        half = math.ceil(len(clean) / 2)
        for i, p in enumerate(clean):
            p["side"] = "left" if i < half else "right"

    by_side = {s: [p for p in clean if p["side"] == s] for s in SIDES}
    lw, rw, tw, bw = (max((len(p["name"]) for p in by_side[s] if p["name"] != "~"), default=0) * CHAR
                      for s in SIDES)
    rows = max(len(by_side["left"]), len(by_side["right"]), 1)
    cols = max(len(by_side["top"]), len(by_side["bottom"]))
    top_row = math.ceil((rows - 1) / 2) * PITCH
    first_col = -math.floor((cols - 1) / 2) * PITCH if cols else 0.0
    col_extent = max(-first_col, first_col + (cols - 1) * PITCH) if cols else 0.0
    half_w = _snap_up(max(
        2 * PITCH,
        (lw + rw + 2 * NAME_OFFSET + PITCH) / 2,
        (max(lw, rw) + NAME_OFFSET + FONT + col_extent) if cols else 0.0,
        col_extent + PITCH,
    ))
    up, down = top_row + PITCH, (rows - 1) * PITCH - top_row + PITCH
    short = (tw + bw + 2 * NAME_OFFSET + PITCH) - (up + down) if cols else 0.0
    if short > 0:
        up, down = up + _snap_up(short / 2), down + _snap_up(short / 2)

    where: dict[int, tuple[float, float, int]] = {}
    for i, p in enumerate(by_side["left"]):
        where[id(p)] = (-half_w - PIN_LENGTH, top_row - i * PITCH, 0)
    for i, p in enumerate(by_side["right"]):
        where[id(p)] = (half_w + PIN_LENGTH, top_row - i * PITCH, 180)
    for i, p in enumerate(by_side["top"]):
        where[id(p)] = (first_col + i * PITCH, up + PIN_LENGTH, 270)
    for i, p in enumerate(by_side["bottom"]):
        where[id(p)] = (first_col + i * PITCH, -down - PIN_LENGTH, 90)
    placed = [(p, round(where[id(p)][0], 4), round(where[id(p)][1], 4), where[id(p)][2]) for p in clean]
    return placed, half_w, up, down


def _symbol(name, placed, half_w, up, down, reference, value, footprint, datasheet, description, keywords):
    """The library node, laid out as KiCad 10's symbol editor writes one."""
    def font():
        return make("effects", make("font", make("size", FONT, FONT)))

    sides = {p["side"] for p, *_ in placed}
    above = up + (PIN_LENGTH if "top" in sides else 0) + FONT
    below = -(down + (PIN_LENGTH if "bottom" in sides else 0) + FONT)
    fields = [
        _property("Reference", reference, -half_w, round(above, 4), justify=["left"]),
        _property("Value", value, -half_w, round(below, 4), justify=["left"]),
        _property("Footprint", footprint, 0, 0, hide=True),
        _property("Datasheet", datasheet, 0, 0, hide=True),
        _property("Description", description, 0, 0, hide=True),
    ]
    if keywords:
        fields.append(_property("ki_keywords", keywords, 0, 0, hide=True))
    body = [Sym("symbol"), f"{name}_0_1", [
        Sym("rectangle"), make("start", -half_w, up), make("end", half_w, -down),
        [Sym("stroke"), make("width", 0.254), make("type", Sym("default"))],
        [Sym("fill"), make("type", Sym("background"))],
    ]]
    unit = [Sym("symbol"), f"{name}_1_1", *(
        [Sym("pin"), Sym(p["type"]), Sym("line"), make("at", x, y, angle), make("length", PIN_LENGTH),
         [Sym("name"), p["name"], font()], [Sym("number"), p["number"], font()]]
        for p, x, y, angle in placed
    )]
    return [
        Sym("symbol"), name,
        [Sym("pin_names"), make("offset", NAME_OFFSET)],
        make("exclude_from_sim", Sym("no")), make("in_bom", Sym("yes")), make("on_board", Sym("yes")),
        make("in_pos_files", Sym("yes")), make("duplicate_pin_numbers_are_jumpers", Sym("no")),
        *fields, body, unit,
        make("embedded_fonts", Sym("no")),
    ]


def _with_symbol(text: str | None, node: sexpr.SExpr) -> str:
    """A library's text with `node` added, or a new library holding just it."""
    if text is None:
        return sexpr.dumps([
            Sym("kicad_symbol_lib"), make("version", LIBRARY_VERSION),
            make("generator", GENERATOR), make("generator_version", "10.0"), node,
        ])
    newline = "\r\n" if "\r\n" in text else "\n"
    return _before_close(text, "".join(f"\t{line}{newline}" for line in sexpr.dumps(node).splitlines()))


def _with_entry(text: str | None, fields: list[sexpr.SExpr]) -> str:
    """A sym-lib-table's text with one more `(lib ...)` line, in the style its lines have."""
    if text is None:
        return f'(sym_lib_table\n\t(version 7)\n\t(lib {" ".join(_inline(f) for f in fields)})\n)\n'
    newline = "\r\n" if "\r\n" in text else "\n"
    sample = next((line for line in text.splitlines() if line.lstrip().startswith("(lib ")), "\t(lib ")
    indent = sample[: len(sample) - len(sample.lstrip())]
    joiner = "" if ")(" in sample else " "
    return _before_close(text, f"{indent}(lib {joiner.join(_inline(f) for f in fields)}){newline}")


def _before_close(text: str, lines: str) -> str:
    """`text` with whole `lines` inserted just before its last closing parenthesis."""
    close = text.rstrip().rfind(")")
    start = text.rfind("\n", 0, close) + 1
    if text[start:close].strip():  # the parenthesis closes a line with content on it
        newline = "\r\n" if "\r\n" in text else "\n"
        return text[:close] + newline + lines + text[close:]
    return text[:start] + lines + text[start:]


def _inline(node: sexpr.SExpr) -> str:
    return sexpr.dumps(node).strip()


def _read(path: Path) -> str | None:
    """A file's text with its line endings as they are; None if it is not there."""
    return path.read_bytes().decode("utf-8") if path.is_file() else None


def _parse(text: str, path: Path) -> sexpr.SExpr:
    try:
        return sexpr.parse(text)
    except SyntaxError as exc:
        raise ToolInputError(
            f"Could not parse {path}: {exc}. Nothing was written.",
            remedy="Open it in KiCad and save it, or fix the file, then try again.",
        ) from exc


def _usable(text: str, what: str) -> str:
    text = str(text)
    if not text.strip() or text != text.strip() or any(c in text for c in ':/\\"'):
        raise ToolInputError(
            f"{what} {text!r} cannot be used: it needs text, no spaces at either end, "
            'and none of : / \\ ".',
            remedy="Use letters, digits, - and _, e.g. LevelShifter_4ch.",
        )
    return text


def _snap_up(v: float) -> float:
    return round(math.ceil(round(v / PITCH, 6)) * PITCH, 4)
