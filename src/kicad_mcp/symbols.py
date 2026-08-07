"""Symbol library resolution and lookup.

Two jobs: find which `.kicad_sym` files exist and what they are called, and pull
a symbol definition out of one in a form that can be embedded into a schematic.

The second job is what makes schematic generation possible. A `.kicad_sch` does
not reference libraries at load time -- it embeds a copy of every symbol it uses
in a `lib_symbols` block. Generating a schematic therefore means physically
copying symbol definitions out of the library and renaming them to the
`Library:Symbol` id the instances refer to.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import sexpr
from .errors import ToolInputError
from .sexpr import Sym


@dataclass(frozen=True)
class LibraryEntry:
    name: str
    uri: str
    path: Path | None

    def exists(self) -> bool:
        return self.path is not None and self.path.is_file()


@dataclass(frozen=True)
class PinInfo:
    number: str
    name: str
    electrical_type: str
    x: float
    y: float
    angle: float
    length: float
    unit: int

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "name": self.name,
            "electrical_type": self.electrical_type,
            "x": self.x,
            "y": self.y,
            "angle": self.angle,
            "length": self.length,
            "unit": self.unit,
        }


def _config_dir(major: int) -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    base = Path(appdata) / "kicad"
    if not base.is_dir():
        return None
    # Config dirs are named by major.minor; pick the one matching this install.
    candidates = sorted(
        (d for d in base.iterdir() if d.is_dir() and re.match(r"^\d+\.\d+$", d.name)),
        key=lambda d: [int(x) for x in d.name.split(".")],
        reverse=True,
    )
    for d in candidates:
        if d.name.startswith(f"{major}."):
            return d
    return candidates[0] if candidates else None


def _substitution_vars(install, project_dir: Path | None) -> dict[str, str]:
    """KiCad's path variables, enough of them to resolve stock library URIs."""
    share = install.share_dir
    major = install.major
    variables = {
        f"KICAD{major}_SYMBOL_DIR": str(share / "symbols"),
        f"KICAD{major}_FOOTPRINT_DIR": str(share / "footprints"),
        f"KICAD{major}_3DMODEL_DIR": str(share / "3dmodels"),
        f"KICAD{major}_TEMPLATE_DIR": str(share / "template"),
        f"KICAD{major}_3RD_PARTY": str(
            Path(os.environ.get("USERPROFILE", "")) / "Documents" / "KiCad"
            / f"{major}.0" / "3rdparty"
        ),
    }
    if project_dir:
        variables["KIPRJMOD"] = str(project_dir)
    # Real environment overrides our defaults, matching KiCad's own precedence.
    for key in list(variables):
        if os.environ.get(key):
            variables[key] = os.environ[key]
    return variables


def _expand(uri: str, variables: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        return variables.get(m.group(1), m.group(0))

    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, uri)


def _read_table(path: Path, variables: dict[str, str], seen: set[Path]) -> list[LibraryEntry]:
    """Read a sym-lib-table, following nested tables.

    KiCad 10 ships a global table whose single entry is of type "Table" pointing
    at the stock library table. Without following that indirection the stock
    libraries look like they do not exist.
    """
    path = path.resolve()
    if path in seen or not path.is_file():
        return []
    seen.add(path)

    try:
        tree = sexpr.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    entries: list[LibraryEntry] = []
    for lib in sexpr.children(tree, "lib"):
        name = sexpr.value(lib, "name")
        uri = sexpr.value(lib, "uri")
        kind = str(sexpr.value(lib, "type", "KiCad"))
        if not name or not uri:
            continue
        resolved = Path(_expand(str(uri), variables))
        if kind.lower() == "table":
            entries.extend(_read_table(resolved, variables, seen))
            continue
        entries.append(LibraryEntry(name=str(name), uri=str(uri), path=resolved))
    return entries


class SymbolIndex:
    """Resolved view of the symbol libraries available to a project."""

    def __init__(self, install, project_dir: Path | None = None) -> None:
        self.install = install
        self.project_dir = Path(project_dir) if project_dir else None
        self._entries: dict[str, LibraryEntry] | None = None

    @property
    def entries(self) -> dict[str, LibraryEntry]:
        if self._entries is None:
            variables = _substitution_vars(self.install, self.project_dir)
            seen: set[Path] = set()
            found: list[LibraryEntry] = []

            cfg = _config_dir(self.install.major)
            if cfg:
                found.extend(_read_table(cfg / "sym-lib-table", variables, seen))
            # Fall back to the stock table directly, so a machine whose user
            # config was never initialised still sees the shipped libraries.
            found.extend(
                _read_table(
                    self.install.share_dir / "template" / "sym-lib-table", variables, seen
                )
            )
            if self.project_dir:
                found.extend(
                    _read_table(self.project_dir / "sym-lib-table", variables, seen)
                )
            self._entries = {e.name: e for e in found}
        return self._entries

    def list_libraries(self, present_only: bool = True) -> list[dict]:
        out = []
        for e in sorted(self.entries.values(), key=lambda x: x.name.lower()):
            if present_only and not e.exists():
                continue
            out.append({"name": e.name, "path": str(e.path) if e.path else None})
        return out

    def library(self, name: str) -> LibraryEntry:
        entry = self.entries.get(name)
        if entry is None:
            close = [n for n in self.entries if n.lower() == name.lower()]
            if close:
                entry = self.entries[close[0]]
            else:
                raise ToolInputError(
                    f"No symbol library named {name!r}.",
                    remedy="Call list_symbol_libraries to see available names.",
                )
        if not entry.exists():
            raise ToolInputError(
                f"Symbol library {entry.name!r} is registered but its file is missing: {entry.path}",
                remedy="Check the sym-lib-table entry or reinstall the libraries.",
            )
        return entry

    def search(self, query: str, limit: int = 50, library: str | None = None) -> list[dict]:
        """Case-insensitive substring search over symbol names.

        Searches library files directly rather than building a global index:
        223 stock libraries is far too much to parse eagerly for one lookup.
        """
        q = query.lower()
        results: list[dict] = []
        names = [library] if library else list(self.entries)
        for lib_name in names:
            entry = self.entries.get(lib_name) if library is None else self.library(lib_name)
            if entry is None or not entry.exists():
                continue
            for sym_name in _symbol_names(entry.path):
                if q in sym_name.lower():
                    results.append(
                        {
                            "lib_id": f"{entry.name}:{sym_name}",
                            "library": entry.name,
                            "symbol": sym_name,
                        }
                    )
                    if len(results) >= limit:
                        flush_name_cache()
                        return results
        flush_name_cache()
        return results

    def definition(self, lib_id: str) -> sexpr.SExpr:
        """The symbol node from the library, renamed to `lib_id`, self-contained.

        Returned ready to drop into a schematic's `lib_symbols` block.

        Many stock symbols (most regulator and transistor variants, several
        logic families) are declared with `(extends "Base")` and carry no pins
        of their own -- the pin and graphic sub-symbols live only on the base.
        That is fine inside the library, where the base is always alongside it,
        but a schematic embeds one symbol at a time. So `extends` is resolved
        here: the base's pin/graphic sub-symbols are merged in and the
        `extends` property is dropped, leaving one node with no external
        dependency.
        """
        library, _, symbol = lib_id.partition(":")
        if not symbol:
            raise ToolInputError(
                f"Malformed lib_id {lib_id!r}.",
                remedy="Use 'Library:Symbol', e.g. 'RF_Module:ESP32-C3-MINI-1'.",
            )
        entry = self.library(library)
        node = _find_symbol(entry.path, symbol)
        if node is None:
            raise ToolInputError(
                f"Symbol {symbol!r} not found in library {entry.name!r}.",
                remedy="Call search_symbols to find the exact name.",
            )
        node = self._resolve_extends(entry, node, seen={symbol})
        renamed = list(node)
        renamed[1] = f"{entry.name}:{symbol}"
        return renamed

    #: Leading flag nodes that KiCad expects before any `property`. A derived
    #: symbol declares none of these itself -- they exist only on the base --
    #: so they must be copied across, not just the pins.
    _LEADING_FLAGS = (
        "pin_numbers", "pin_names", "exclude_from_sim", "in_bom", "on_board",
        "in_pos_files", "duplicate_pin_numbers_are_jumpers",
    )

    def _resolve_extends(self, entry: LibraryEntry, node: sexpr.SExpr, seen: set[str]) -> sexpr.SExpr:
        base_name = sexpr.value(node, "extends")
        if base_name is None:
            return node
        base_name = str(base_name)
        if base_name in seen:  # pragma: no cover - defensive against a cycle
            raise ToolInputError(f"Circular 'extends' chain involving {base_name!r}.")
        base = _find_symbol(entry.path, base_name)
        if base is None:
            raise ToolInputError(
                f"{node[1]!r} extends {base_name!r}, which was not found in {entry.name!r}.",
            )
        base = self._resolve_extends(entry, base, seen | {base_name})

        own_names = {sexpr._name_of(n) for n in node if isinstance(n, list)}
        flags = [
            n for n in base
            if isinstance(n, list) and sexpr._name_of(n) in self._LEADING_FLAGS
            and sexpr._name_of(n) not in own_names
        ]
        properties = [n for n in node if isinstance(n, list) and sexpr._name_of(n) == "property"]

        # KiCad expects each unit sub-symbol's name to be prefixed with its
        # parent's own (un-namespaced) name -- "AMS1117-3.3_1_1", not the
        # base's "AP1117-15_1_1" -- and rejects the whole file if it isn't.
        derived_name = str(node[1])
        own_sub_names = {s[1] for s in sexpr.children(node, "symbol") if len(s) > 1}
        sub_symbols = []
        for sub in sexpr.children(base, "symbol"):
            if len(sub) > 1 and sub[1] in own_sub_names:
                continue
            sub = list(sub)
            suffix = re.search(r"(_\d+_\d+)$", str(sub[1])) if len(sub) > 1 else None
            if suffix:
                sub[1] = f"{derived_name}{suffix.group(1)}"
            sub_symbols.append(sub)
        # Graphic primitives that sit directly on the base (rare, but valid)
        # rather than inside a unit sub-symbol.
        graphics = [
            n for kind in ("rectangle", "circle", "arc", "polyline", "text")
            for n in sexpr.children(base, kind)
        ]

        embedded_fonts = sexpr.child(node, "embedded_fonts") or sexpr.child(base, "embedded_fonts")

        # KiCad's own field order: flags, properties, sub-symbols/graphics,
        # embedded_fonts last. Getting this wrong produces a file KiCad
        # silently refuses to load rather than one it reports an error on.
        merged: sexpr.SExpr = [node[0], node[1], *flags, *properties, *graphics, *sub_symbols]
        if embedded_fonts is not None:
            merged.append(embedded_fonts)
        return merged

    def pins(self, lib_id: str) -> list[PinInfo]:
        """Pins of a symbol, gathered from its unit sub-symbols.

        KiCad stores pins on child symbols named `<Name>_<unit>_<bodystyle>`,
        not on the parent, so the parent alone always looks pinless.
        """
        node = self.definition(lib_id)
        out: list[PinInfo] = []
        for sub in sexpr.children(node, "symbol"):
            sub_name = sub[1] if len(sub) > 1 and isinstance(sub[1], str) else ""
            m = re.search(r"_(\d+)_(\d+)$", str(sub_name))
            unit = int(m.group(1)) if m else 1
            for pin in sexpr.children(sub, "pin"):
                out.append(_pin_info(pin, unit))
        out.sort(key=lambda p: (p.unit, _natural_key(p.number)))
        return out


def _natural_key(text: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def _pin_info(pin: sexpr.SExpr, unit: int) -> PinInfo:
    bare = [a.name for a in pin[1:] if isinstance(a, Sym)]
    etype = bare[0] if bare else "unspecified"
    at = sexpr.values(pin, "at")
    name_node = sexpr.child(pin, "name")
    number_node = sexpr.child(pin, "number")
    return PinInfo(
        number=str(number_node[1]) if number_node and len(number_node) > 1 else "",
        name=str(name_node[1]) if name_node and len(name_node) > 1 else "",
        electrical_type=etype,
        x=float(at[0]) if len(at) > 0 else 0.0,
        y=float(at[1]) if len(at) > 1 else 0.0,
        angle=float(at[2]) if len(at) > 2 else 0.0,
        length=float(sexpr.value(pin, "length", 0) or 0),
        unit=unit,
    )


@lru_cache(maxsize=64)
def _parse_library(path_str: str) -> sexpr.SExpr:
    return sexpr.parse(Path(path_str).read_text(encoding="utf-8"))


#: Top-level symbols sit at exactly one tab; nested unit variants are deeper.
_TOP_LEVEL_SYMBOL = re.compile(r'[\r\n]\t\(symbol "([^"]+)"')


def _cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    d = Path(base) / "kicad-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_name_cache() -> dict:
    import json

    f = _cache_dir() / "symbol-names.json"
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_name_cache(cache: dict) -> None:
    import json

    try:
        (_cache_dir() / "symbol-names.json").write_text(
            json.dumps(cache), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - cache is an optimisation, never fatal
        pass


_NAME_CACHE: dict | None = None


def _symbol_names(path: Path | None) -> list[str]:
    """Top-level symbol names in a library.

    Uses a regex scan rather than a full parse. The stock libraries total ~234 MB
    and parsing them to answer one search took ~37 s; scanning takes under a
    second, and a disk cache keyed on size+mtime makes repeat calls instant.

    Sub-symbols (`Name_1_1`) are unit/body-style variants of their parent and are
    excluded by the indentation anchor -- they are not separately placeable.
    """
    global _NAME_CACHE
    if path is None or not path.is_file():
        return []

    if _NAME_CACHE is None:
        _NAME_CACHE = _load_name_cache()

    try:
        stat = path.stat()
    except OSError:
        return []
    key = str(path)
    stamp = [int(stat.st_mtime), stat.st_size]
    hit = _NAME_CACHE.get(key)
    if hit and hit.get("stamp") == stamp:
        return hit["names"]

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    names = _TOP_LEVEL_SYMBOL.findall(text)
    _NAME_CACHE[key] = {"stamp": stamp, "names": names}
    global _CACHE_DIRTY
    _CACHE_DIRTY = True
    return names


_CACHE_DIRTY = False


def flush_name_cache() -> None:
    """Persist accumulated scans once, rather than after every library."""
    global _CACHE_DIRTY
    if _CACHE_DIRTY and _NAME_CACHE is not None:
        _save_name_cache(_NAME_CACHE)
        _CACHE_DIRTY = False


def _find_symbol(path: Path | None, name: str) -> sexpr.SExpr | None:
    if path is None or not path.is_file():
        return None
    tree = _parse_library(str(path))
    for sym_node in sexpr.children(tree, "symbol"):
        if len(sym_node) > 1 and sym_node[1] == name:
            return sym_node
    for sym_node in sexpr.children(tree, "symbol"):
        if len(sym_node) > 1 and str(sym_node[1]).lower() == name.lower():
            return sym_node
    return None
