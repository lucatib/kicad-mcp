"""The design behind a set of Altium sheets: root, sheet instances, KiCad paths.

KiCad links a footprint to its symbol through the path of sheet UUIDs from
the root down to the symbol. Altium's board stores that same path as
UniqueIds (`\\SHEETSYMBOL\\PART`), and KiCad's board importer turns each
UniqueId into a UUID with a fixed name-based hash. Giving every sheet symbol
and part the UUID of its UniqueId therefore makes KiCad's footprint paths
line up with the schematic, channel by channel, without consulting a single
designator.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ToolInputError
from .model import Part, Sheet, SheetSymbol
from .pcbdoc import BoardPart

#: KiCad's AltiumUniqueIdToKiid namespace. Changing it breaks every link.
KICAD_ALTIUM_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


def altium_kiid(unique_id: str) -> str:
    """The UUID KiCad's board importer gives an Altium UniqueId."""
    return str(uuid.uuid5(KICAD_ALTIUM_NS, unique_id.lstrip("\\")))


def flat_sheet_uuid(stem: str) -> str:
    """A flat design's sheets have no sheet symbol, so no UniqueId to hash."""
    return str(uuid.uuid5(KICAD_ALTIUM_NS, "flat-sheet:" + stem))


def root_sheet_uuid(project_name: str) -> str:
    return str(uuid.uuid5(KICAD_ALTIUM_NS, "root:" + project_name))


#: PrjPcb HierarchyMode, in the order Altium's options dialog lists them.
_MODES = {0: None, 1: "flat", 2: "hierarchical", 3: "hierarchical", 4: "global"}


@dataclass
class SheetNode:
    sheet: Sheet | None
    file_name: str
    sheet_symbol: SheetSymbol | None
    kicad_uuid: str
    kicad_path: str
    id_path: tuple[str, ...]
    footprint_prefix: str
    children: list["SheetNode"] = field(default_factory=list)
    #: Sheet name, unique among siblings, and the "/a/b/" path KiCad's board shows.
    name: str = ""
    human_path: str = "/"

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Design:
    project_name: str
    root: SheetNode
    sheets: dict[str, Sheet]
    mode: str
    flat: bool
    root_uuid: str
    channel_format: str = "$Component$ChannelAlpha"
    issues: list[dict] = field(default_factory=list)
    inferred: list[dict] = field(default_factory=list)
    unreferenced: list[str] = field(default_factory=list)
    #: The id path a board uses for a part -> (sheet instance, part).
    part_paths: dict[tuple[str, ...], tuple[SheetNode, Part]] = field(default_factory=dict)

    def nodes(self):
        return [n for n in self.root.walk() if n.sheet is not None]


def _prjpcb(path: Path | None) -> dict[str, list[str]]:
    if path is None:
        return {}
    out: dict[str, list[str]] = {}
    for line in Path(path).read_text(encoding="latin-1", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(key.strip().upper(), []).append(value.strip())
    return out


def _key(name: str) -> str:
    return Path(name.replace("\\", "/")).name.lower()


def _order_key(node_symbol: SheetSymbol) -> tuple[float, float]:
    # Altium numbers channels top to bottom, then left to right.
    return (-node_symbol.y, node_symbol.x)


def _mode(sheets: list[Sheet], prj: dict[str, list[str]]) -> str:
    for raw in prj.get("HIERARCHYMODE", []):
        try:
            mode = _MODES.get(int(raw))
        except ValueError:
            mode = None
        if mode:
            return mode
    if any(s.entries for sheet in sheets for s in sheet.sheet_symbols):
        return "hierarchical"
    if any(sheet.ports for sheet in sheets):
        return "flat"
    return "global"


def build_design(sheets: list[Sheet], project_name: str,
                 board_parts: list[BoardPart] | None, prjpcb: Path | None) -> Design:
    prj = _prjpcb(prjpcb)
    by_file = {}
    for s in sheets:
        by_file.setdefault(_key(s.path.name), s)
    root_uuid = root_sheet_uuid(project_name)
    hierarchical = any(s.sheet_symbols for s in sheets)
    issues: list[dict] = []
    inferred: list[dict] = []
    doc_order = [_key(p) for p in prj.get("DOCUMENTPATH", [])]

    if hierarchical:
        referenced = {_key(sym.file_name) for s in sheets for sym in s.sheet_symbols}
        candidates = [s for s in sheets if _key(s.path.name) not in referenced]
        root_sheet = _pick_root(candidates, board_parts, doc_order)
        root = SheetNode(root_sheet, root_sheet.path.name, None, root_uuid, "/" + root_uuid,
                         (), "")
        _expand(root, by_file, issues, stack=[_key(root_sheet.path.name)])
        flat = False
    else:
        ordered = _flat_order(sheets, board_parts, doc_order)
        if len(ordered) == 1:
            only = ordered[0]
            root = SheetNode(only, only.path.name, None, root_uuid, "/" + root_uuid, (), "")
        else:
            root = SheetNode(None, project_name + ".kicad_sch", None, root_uuid,
                             "/" + root_uuid, (), "")
            for s in ordered:
                sid = flat_sheet_uuid(s.stem)
                root.children.append(SheetNode(s, s.path.name, None, sid,
                                               root.kicad_path + "/" + sid, (), "/" + sid))
            inferred.append({"what": "flat design: root sheet created with one sheet per .SchDoc",
                             "order": [s.stem for s in ordered]})
        flat = True

    _name_sheets(root, inferred)
    in_design = {id(n.sheet) for n in root.walk() if n.sheet is not None}
    unreferenced = [s.stem for s in sheets if id(s) not in in_design]
    channel_format = (prj.get("CHANNELDESIGNATORFORMATSTRING") or ["$Component$ChannelAlpha"])[0]
    design = Design(project_name=project_name, root=root, sheets={s.stem: s for s in sheets},
                    mode=_mode(sheets, prj), flat=flat, root_uuid=root_uuid,
                    channel_format=channel_format, issues=issues, inferred=inferred,
                    unreferenced=unreferenced)
    for node in design.nodes():
        for part in node.sheet.parts:
            key = (part.uid,) if flat else node.id_path + (part.uid,)
            if part.uid and key not in design.part_paths:
                design.part_paths[key] = (node, part)
    return design


def _pick_root(candidates: list[Sheet], board_parts, doc_order) -> Sheet:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ToolInputError("Every sheet is used by another sheet's sheet symbol; there is no root.",
                             remedy="Check the sheet symbols for a loop, or pass the root .SchDoc.")
    if board_parts:
        roots = {p.sheet_path.split("\\")[0].lower() for p in board_parts if p.sheet_path}
        named = [s for s in candidates if s.stem.lower() in roots]
        if len(named) == 1:
            return named[0]
    for doc in doc_order:
        for s in candidates:
            if _key(s.path.name) == doc:
                return s
    names = ", ".join(sorted(s.stem for s in candidates))
    raise ToolInputError(f"More than one sheet could be the root: {names}.",
                         remedy="Pass the .PcbDoc or .PrjPcb as well, or leave out sheets "
                                "that are not part of this design.")


def _flat_order(sheets: list[Sheet], board_parts, doc_order) -> list[Sheet]:
    rank: dict[str, int] = {}
    for i, doc in enumerate(doc_order):
        rank.setdefault(doc, i)
    if not rank and board_parts:
        for p in board_parts:
            if p.sheet_path:
                rank.setdefault((p.sheet_path + ".schdoc").lower(), len(rank))
    return sorted(sheets, key=lambda s: (rank.get(_key(s.path.name), len(rank) + 1), s.stem.lower()))


def _expand(node: SheetNode, by_file, issues, stack: list[str]) -> None:
    for sym in node.sheet.sheet_symbols:
        if sym.name.strip().lower().startswith("repeat("):
            issues.append({"kind": "repeat_channel", "sheet_symbol": sym.name,
                           "note": "Repeat() channels are converted as a single instance"})
        sid = altium_kiid(sym.uid) if sym.uid else str(uuid.uuid4())
        child_sheet = by_file.get(_key(sym.file_name))
        child = SheetNode(child_sheet, sym.file_name, sym, sid, node.kicad_path + "/" + sid,
                          node.id_path + (sym.uid,), node.footprint_prefix + "/" + sid)
        node.children.append(child)
        if child_sheet is None:
            issues.append({"kind": "missing_sheet", "file": sym.file_name})
            continue
        key = _key(child_sheet.path.name)
        if key in stack:
            raise ToolInputError(f"Sheet {child_sheet.path.name} contains itself through "
                                 f"{' -> '.join(stack)}.",
                                 remedy="Fix the sheet symbol loop in Altium first.")
        _expand(child, by_file, issues, stack + [key])


def _name_sheets(node: SheetNode, inferred: list[dict]) -> None:
    """Give children unique names: KiCad rejects two sibling sheets with one name."""
    def base(child: SheetNode) -> str:
        if child.sheet_symbol is not None and child.sheet_symbol.name:
            return child.sheet_symbol.name
        return child.sheet.stem if child.sheet is not None else Path(child.file_name).stem

    groups: dict[str, list[SheetNode]] = {}
    for child in node.children:
        groups.setdefault(base(child), []).append(child)
    for name, group in groups.items():
        if len(group) == 1:
            group[0].name = name
            continue
        ordered = sorted(group, key=lambda c: _order_key(c.sheet_symbol) if c.sheet_symbol else (0, 0))
        for i, child in enumerate(ordered):
            child.name = f"{name}_{_alpha(i)}"
        inferred.append({"what": f"sheet names {name}_A..{name}_{_alpha(len(group) - 1)}",
                         "reason": f"{len(group)} sibling sheets are all called {name}; "
                                   "KiCad needs unique names (designators unchanged)"})
    for child in node.children:
        child.human_path = node.human_path + child.name + "/"
        _name_sheets(child, inferred)


def footprint_kicad_path(design: Design, id_path: tuple[str, ...]) -> str | None:
    """The KiCad footprint path for a board part's SOURCEUNIQUEID, if that part exists."""
    found = design.part_paths.get(tuple(id_path))
    if found is None:
        return None
    node, part = found
    return node.footprint_prefix + "/" + altium_kiid(part.uid)


def _alpha(n: int) -> str:
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(ord("A") + r) + out
    return out


def _channel_name(fmt: str, designator: str, index: int, room: str) -> str:
    out = fmt
    for token, value in (("$ComponentPrefix", re.sub(r"\d.*$", "", designator)),
                         ("$Component", designator), ("$ChannelAlpha", _alpha(index)),
                         ("$ChannelIndex", str(index + 1)), ("$RoomName", room)):
        out = out.replace(token, value)
    return out


def instance_references(design: Design,
                        board_refs: dict[tuple[str, ...], str]) -> dict[tuple[str, str], str]:
    """(sheet instance path, part uid) -> the reference that instance shows.

    The board's reference wins. Without one, the .SchDoc designator, with
    Altium's channel suffix added when the sheet is used more than once.
    """
    uses: dict[int, list[SheetNode]] = {}
    for node in design.nodes():
        uses.setdefault(id(node.sheet), []).append(node)
    channel_index: dict[str, int] = {}
    for nodes in uses.values():
        if len(nodes) > 1:
            ordered = sorted(nodes, key=lambda n: _order_key(n.sheet_symbol) if n.sheet_symbol else (0, 0))
            for i, n in enumerate(ordered):
                channel_index[n.kicad_path] = i

    refs: dict[tuple[str, str], str] = {}
    inferred_for: set[str] = set()
    for node in design.nodes():
        for part in node.sheet.parts:
            key = (part.uid,) if design.flat else node.id_path + (part.uid,)
            ref = board_refs.get(key)
            if ref is None:
                ref = part.designator
                if node.kicad_path in channel_index and ref:
                    room = node.sheet_symbol.name if node.sheet_symbol else ""
                    ref = _channel_name(design.channel_format, ref,
                                        channel_index[node.kicad_path], room)
                    inferred_for.add(node.sheet.stem)
            refs[(node.kicad_path, part.uid)] = ref
    for stem in sorted(inferred_for):
        design.inferred.append({"what": f"channel designators for {stem}",
                                "format": design.channel_format,
                                "reason": "no board reference for these instances"})
    return refs
