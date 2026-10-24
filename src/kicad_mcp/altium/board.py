"""The board kicad-cli imports from a .PcbDoc, re-linked to the converted schematic.

KiCad's Altium importer keeps each footprint's designator and the UUID of its
part's UniqueId, but builds the sheet half of the path from the sheet *name*,
which is not a UUID, so every footprint gets a random one. This module joins
each footprint back to its Components6 row, rewrites path, sheetname and
sheetfile from SOURCEUNIQUEID, and renames nets to what the schematic calls
them. Edits are targeted text substitutions: the file stays exactly as
KiCad wrote it everywhere else.
"""

from __future__ import annotations

import json
import re
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import cli
from ..errors import CliError
from .design import Design, altium_kiid, footprint_kicad_path
from .pcbdoc import BoardPart

#: Footprints KiCad's importer moved by the board offset still sit this close
#: to where Altium put them.
JOIN_TOLERANCE_MM = 0.05

_FP_START = re.compile(r'^\t\(footprint "([^"]*)"', re.M)
_AT = re.compile(r'^\t\t\(at ([-\d.e]+) ([-\d.e]+)(?: ([-\d.e]+))?\)', re.M)
_REF = re.compile(r'^\t\t\(property "Reference" "((?:[^"\\]|\\.)*)"', re.M)
_VALUE = re.compile(r'^\t\t\(property "Value" "((?:[^"\\]|\\.)*)"', re.M)
_PATH = re.compile(r'^\t\t\(path "([^"]*)"\)', re.M)
_PAD = re.compile(r'^\t\t\(pad "((?:[^"\\]|\\.)*)"', re.M)
_NET = re.compile(r'\(net "((?:[^"\\]|\\.)*)"\)')


@dataclass
class Footprint:
    index: int
    start: int
    end: int
    lib_id: str
    ref: str
    x: float
    y: float
    path: str | None
    pads: dict[str, str] = field(default_factory=dict)
    value: str = ""

    @property
    def symbol_uuid(self) -> str | None:
        return self.path.rsplit("/", 1)[-1] if self.path else None


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def footprints(text: str) -> list[Footprint]:
    starts = [m.start() for m in _FP_START.finditer(text)]
    out = []
    for i, start in enumerate(starts):
        # A footprint ends where the next top-level item starts.
        nxt = re.compile(r"^\t\(", re.M).search(text, start + 1)
        end = nxt.start() if nxt else len(text)
        block = text[start:end]
        lib = _FP_START.match(block).group(1)
        at = _AT.search(block)
        ref = _REF.search(block)
        value = _VALUE.search(block)
        path = _PATH.search(block)
        pads: dict[str, str] = {}
        pad_starts = [m for m in _PAD.finditer(block)]
        for j, m in enumerate(pad_starts):
            pad_end = pad_starts[j + 1].start() if j + 1 < len(pad_starts) else len(block)
            net = _NET.search(block, m.end(), pad_end)
            if net and net.group(1):
                pads.setdefault(_unescape(m.group(1)), _unescape(net.group(1)))
        out.append(Footprint(index=i, start=start, end=end, lib_id=lib,
                             ref=_unescape(ref.group(1)) if ref else "",
                             x=float(at.group(1)) if at else 0.0, y=float(at.group(2)) if at else 0.0,
                             path=path.group(1) if path else None, pads=pads,
                             value=_unescape(value.group(1)) if value else ""))
    return out


def footprint_fields(text: str) -> dict[str, str]:
    """reference -> footprint lib id, for the symbols' Footprint field."""
    return {fp.ref: fp.lib_id for fp in footprints(text) if fp.ref}


def board_pad_nets(text: str) -> dict[str, set[tuple[str, str]]]:
    """net name -> {(reference, pad number)}; unnamed nets left out."""
    out: dict[str, set[tuple[str, str]]] = {}
    for fp in footprints(text):
        for pad, net in fp.pads.items():
            if fp.ref and net:
                out.setdefault(net, set()).add((fp.ref, pad))
    return out


def import_board(install, pcbdoc: Path, out_pcb: Path, timeout: int = 900) -> dict:
    """kicad-cli's Altium board import; returns its JSON report."""
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-") as tmp:
        report_file = Path(tmp) / "import.json"
        result = cli.run_cli(install, ["pcb", "import", "--format", "altium", "--report-format", "json",
                                       "--report-file", str(report_file), "-o", str(out_pcb), str(pcbdoc)],
                             timeout=timeout)
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"errors": [result["stderr"] or result["stdout"] or "no import report"]}
    if result["returncode"] != 0 or not out_pcb.is_file():
        raise CliError(f"kicad-cli could not import {Path(pcbdoc).name}: "
                       f"{result['stderr'] or result['stdout']}",
                       remedy="Check the .PcbDoc opens in Altium; KiCad imports binary .PcbDoc only.")
    return report


def join_footprints(text: str, parts: list[BoardPart]) -> list[tuple[Footprint, BoardPart | None]]:
    """Each footprint with its Components6 row, or None.

    KiCad's importer hashes the whole SOURCEUNIQUEID (`SHEET\\PART`, leading
    backslash dropped) into the last path element, so that alone identifies
    the row, channel instances included. Should two rows ever share it,
    position decides, once the fixed offset KiCad's importer applies is
    measured on the unambiguous parts.
    """
    fps = footprints(text)
    by_uuid: dict[str, list[BoardPart]] = {}
    for p in parts:
        if p.id_path:
            by_uuid.setdefault(altium_kiid("\\".join(p.id_path)), []).append(p)
    offsets = []
    for fp in fps:
        cands = by_uuid.get(fp.symbol_uuid or "", [])
        if len(cands) == 1:
            offsets.append((fp.x - cands[0].x_mm, fp.y + cands[0].y_mm))
    dx = statistics.median(o[0] for o in offsets) if offsets else 0.0
    dy = statistics.median(o[1] for o in offsets) if offsets else 0.0

    used: set[int] = set()
    out: list[tuple[Footprint, BoardPart | None]] = []
    for fp in fps:
        cands = [c for c in by_uuid.get(fp.symbol_uuid or "", []) if c.index not in used]
        best = None
        if cands:
            dist, best = min(((abs(fp.x - (c.x_mm + dx)) + abs(fp.y - (dy - c.y_mm)), c) for c in cands),
                             key=lambda t: t[0])
            if len(by_uuid[fp.symbol_uuid]) > 1 and dist > JOIN_TOLERANCE_MM:
                best = None
        if best is not None:
            used.add(best.index)
        out.append((fp, best))
    return out


def rewrite_board(text: str, joins: list[tuple[Footprint, BoardPart | None]],
                  design: Design) -> tuple[str, dict[tuple[str, ...], str], list[dict]]:
    """Board text with every joinable footprint's path pointing at its symbol.

    Also returns the board reference of each Altium id path (for the
    schematic's instance references) and what could not be linked.
    """
    refs: dict[tuple[str, ...], str] = {}
    issues: list[dict] = []
    edits: list[tuple[int, int, str]] = []
    for fp, part in joins:
        if part is None:
            if fp.ref:
                issues.append({"kind": "board_only", "ref": fp.ref})
            continue
        refs[part.id_path] = fp.ref
        found = design.part_paths.get(part.id_path)
        new_path = footprint_kicad_path(design, part.id_path)
        if found is None or new_path is None:
            issues.append({"kind": "no_schematic_part", "ref": fp.ref,
                           "id": "\\" + "\\".join(part.id_path), "board_sheet": part.sheet_path})
            continue
        node, _ = found
        sheetfile = (design.project_name if node is design.root else node.sheet.stem) + ".kicad_sch"
        block = text[fp.start:fp.end]
        block = _PATH.sub(lambda m: f'\t\t(path "{new_path}")', block, count=1)
        block = re.sub(r'^\t\t\(sheetname "(?:[^"\\]|\\.)*"\)', lambda m: f'\t\t(sheetname "{_escape(node.human_path)}")',
                       block, count=1, flags=re.M)
        block = re.sub(r'^\t\t\(sheetfile "(?:[^"\\]|\\.)*"\)', lambda m: f'\t\t(sheetfile "{_escape(sheetfile)}")',
                       block, count=1, flags=re.M)
        edits.append((fp.start, fp.end, block))
    out = []
    last = 0
    for start, end, block in edits:
        out.append(text[last:start])
        out.append(block)
        last = end
    out.append(text[last:])
    return "".join(out), refs, issues


def assign_pad_nets(text: str, nets: dict[tuple[str, str], str]) -> str:
    """Give pads that have no net the net the schematic gives them.

    The schematic names every unconnected pin's net (`unconnected-(U1-Pad3)`);
    the board leaves those pads netless, and KiCad's first update would fill
    them in. A pad that already has a net is never touched.
    """
    if not nets:
        return text
    inserts: list[tuple[int, str]] = []
    for fp in footprints(text):
        block = text[fp.start:fp.end]
        pad_starts = list(_PAD.finditer(block))
        for j, m in enumerate(pad_starts):
            name = nets.get((fp.ref, _unescape(m.group(1))))
            if name is None:
                continue
            pad_end = pad_starts[j + 1].start() if j + 1 < len(pad_starts) else len(block)
            if _NET.search(block, m.end(), pad_end):
                continue
            line_end = block.index("\n", m.end())
            inserts.append((fp.start + line_end, f'\n\t\t\t(net "{_escape(name)}")'))
    for pos, line in sorted(inserts, reverse=True):
        text = text[:pos] + line + text[pos:]
    return text


_PROP = re.compile(r'^\t\t\(property "((?:[^"\\]|\\.)*)"', re.M)
_FIELD = '''
\t\t(property "{name}" "{value}"
\t\t\t(at 0 0 0)
\t\t\t(layer "F.Fab")
\t\t\t(hide yes)
\t\t\t(uuid "{uuid}")
\t\t\t(effects
\t\t\t\t(font
\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t(thickness 0.15)
\t\t\t\t)
\t\t\t)
\t\t)'''


def add_fields(text: str, fields: dict[str, dict[str, str]]) -> str:
    """Add the symbol's fields a footprint lacks, hidden on F.Fab.

    KiCad's "Update PCB from Schematic" copies symbol fields onto footprints;
    adding them now keeps that first update from touching every part.
    Existing fields are never changed.
    """
    import uuid as _uuid  # noqa: PLC0415

    inserts: list[tuple[int, str]] = []
    for fp in footprints(text):
        wanted = fields.get(fp.ref)
        if not wanted:
            continue
        block = text[fp.start:fp.end]
        have = {_unescape(m.group(1)) for m in _PROP.finditer(block)}
        props = list(_PROP.finditer(block))
        if not props:
            continue
        # After the last existing property, at the end of its closing line.
        last = props[-1]
        close = block.index("\n\t\t)", last.end()) + len("\n\t\t)")
        added = "".join(_FIELD.format(name=_escape(n), value=_escape(v),
                                      uuid=_uuid.uuid5(_uuid.NAMESPACE_URL, f"{fp.ref}:{n}"))
                        for n, v in wanted.items() if n not in have)
        if added:
            inserts.append((fp.start + close, added))
    for pos, chunk in sorted(inserts, reverse=True):
        text = text[:pos] + chunk + text[pos:]
    return text


def fill_values(text: str, values: dict[str, str]) -> str:
    """Set the Value of footprints whose Value is empty; others are left alone."""
    edits = []
    for fp in footprints(text):
        if fp.ref in values and fp.value == "":
            block = text[fp.start:fp.end]
            new = _VALUE.sub(lambda m: f'\t\t(property "Value" "{_escape(values[fp.ref])}"', block, count=1)
            edits.append((fp.start, fp.end, new))
    for start, end, new in reversed(edits):
        text = text[:start] + new + text[end:]
    return text


def rename_nets(text: str, renames: dict[str, str]) -> str:
    """Rename nets everywhere they are named, whole names only."""
    if not renames:
        return text

    def sub(m):
        name = _unescape(m.group(1))
        new = renames.get(name)
        return m.group(0) if new is None else f'(net "{_escape(new)}")'

    return _NET.sub(sub, text)
