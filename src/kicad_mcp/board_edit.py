"""Build and edit a .kicad_pcb through pcbnew, with KiCad closed.

The read tools answer questions about a board somebody laid out. These make
one: the schematic's parts and nets onto a board (what KiCad's "Update PCB
from Schematic" does, which kicad-cli does not offer), footprints to their
places, the outline, net classes, and tracks from `autoroute`. Every function
loads the file, changes it and saves it, so each call leaves a board KiCad
opens; nothing here reaches a running KiCad, which keeps its own copy until
the file is reloaded there.

Net classes live in the project file beside the board. pcbnew reads and
writes it with the board, so it is the one writer here as well.
"""

from __future__ import annotations

import fnmatch
import json
import math
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import autoroute, cli as kcli, hierarchy, pcb_file, sexpr
from .errors import ToolInputError
from .footprints import SUFFIX, FootprintIndex
from .library import _before_close, _inline, _parse, _read, _usable
from .sexpr import make
from .symbols import resolve_tables

#: Gap between parked footprints, and between them and the board.
PARK_GAP = 2.0
#: Parked footprints wrap to a new row past this width.
PARK_WIDTH = 200.0


def _mm(v) -> float:
    return round(v / 1_000_000, 6)


def _nm(pcbnew, v: float) -> int:
    return pcbnew.FromMM(float(v))


def _pt(pcbnew, x: float, y: float):
    return pcbnew.VECTOR2I(_nm(pcbnew, x), _nm(pcbnew, y))


def _open(path: str | Path):
    """A board of its own to change: the cached one belongs to the read tools."""
    p = pcb_file.require_board_file(path)
    pcbnew = pcb_file._pcbnew()
    board = pcbnew.LoadBoard(str(p))
    if board is None:
        raise ToolInputError(f"pcbnew could not load {p.name}.",
                             remedy="Check that the file opens in KiCad 10.")
    return pcbnew, board, p


def _save(pcbnew, board, path: Path) -> None:
    board.BuildConnectivity()
    if not pcbnew.SaveBoard(str(path), board):
        raise ToolInputError(f"pcbnew could not write {path}.",
                             remedy="Check that the folder is writable and the file is not locked.")


def _by_reference(board) -> dict[str, object]:
    return {str(fp.GetReference()): fp for fp in board.GetFootprints()}


def _find(board, reference: str):
    found = _by_reference(board)
    fp = found.get(reference) or next(
        (f for r, f in found.items() if r.lower() == str(reference).lower()), None)
    if fp is None:
        raise ToolInputError(
            f"No footprint {reference!r} on the board.",
            remedy="Call list_footprints for the references it has, or "
                   "update_board_from_schematic to bring new parts over.",
        )
    return fp


# --- footprint libraries -------------------------------------------------

PAD_SHAPES = ("circle", "rect", "oval", "roundrect")


def create_footprint(
    install, project: str | Path, library: str, name: str, pads: list[dict],
    description: str = "", tags: str = "", outline: dict | None = None,
) -> dict:
    """Write a through-hole or surface footprint of plain pads into a project library.

    Each pad is {"number", "x", "y", "size", "drill", "shape"}: `size` one
    number or [x, y]; `drill` 0 or absent for a surface pad on the front;
    `shape` one of PAD_SHAPES, by default a rectangle for pad "1" and circles
    (ovals when the size is not square) for the rest. `outline` is
    {"x", "y", "width", "height"} for the silkscreen box, with the courtyard
    0.25 mm outside it; by default the box around the pads.
    """
    pcbnew = pcb_file._pcbnew()
    folder = hierarchy.find_project_dir(project)
    if folder is None:
        raise ToolInputError(
            f"No KiCad project (.kicad_pro) in {Path(project)} or the folders above it.",
            remedy="Pass the folder holding the project's .kicad_pro, or a file in it.",
        )
    name, library = _usable(name, "Footprint name"), _usable(library, "Library nickname")
    if not pads:
        raise ToolInputError("A footprint needs at least one pad.",
                             remedy='Pass pads such as {"number": "1", "x": 0, "y": 0, "size": 2.5, "drill": 1.2}.')

    table_path = folder / "fp-lib-table"
    table = _read(table_path)
    own = {str(sexpr.value(n, "name")) for n in sexpr.children(_parse(table, table_path), "lib")} if table else set()
    register = library not in own
    if register:
        taken = {n.lower(): e for n, e in resolve_tables(install, None, "fp-lib-table").items()}
        if library.lower() in taken:
            raise ToolInputError(
                f"{library} is a global library ({taken[library.lower()].path}); a project library "
                "of that name would hide it in this project.",
                remedy=f"Pick another nickname, such as the project's name, {folder.name}.",
            )
        lib_path = folder / f"{library}.pretty"
    else:
        lib_path = FootprintIndex(install, folder).entries[library].path
    if (lib_path / f"{name}{SUFFIX}").exists():
        raise ToolInputError(
            f"{library} already has a footprint {name}; nothing was written.",
            remedy="Pick another name, or delete the file to replace it.",
        )

    fp = pcbnew.FOOTPRINT(None)
    fp.SetFPIDAsString(name)
    fp.SetReference("REF**")
    fp.SetValue(name)
    if description:
        fp.SetLibDescription(description)
    if tags:
        fp.SetKeywords(tags)
    shapes = {"circle": pcbnew.PAD_SHAPE_CIRCLE, "rect": pcbnew.PAD_SHAPE_RECTANGLE,
              "oval": pcbnew.PAD_SHAPE_OVAL, "roundrect": pcbnew.PAD_SHAPE_ROUNDRECT}
    box = [math.inf, math.inf, -math.inf, -math.inf]
    through = False
    for i, spec in enumerate(pads):
        try:
            number = str(spec["number"])
            x, y = float(spec["x"]), float(spec["y"])
            size = spec["size"]
            sx, sy = (float(size), float(size)) if isinstance(size, (int, float)) else map(float, size)
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolInputError(
                f"Pad {i + 1} is not usable: {exc!r}.",
                remedy='Each pad needs "number", "x", "y" and "size" (a number or [x, y]).',
            ) from exc
        drill = float(spec.get("drill") or 0)
        shape = spec.get("shape") or ("rect" if number == "1" else "circle" if sx == sy else "oval")
        if shape not in shapes:
            raise ToolInputError(f"Pad {number} has shape {shape!r}.",
                                 remedy=f"Use one of {', '.join(PAD_SHAPES)}.")
        if drill and drill >= min(sx, sy):
            raise ToolInputError(
                f"Pad {number}: a {drill} mm drill leaves no copper in a {min(sx, sy)} mm pad.",
                remedy="Make the pad larger than its drill.",
            )
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH if drill else pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pcbnew.PAD.PTHMask() if drill else pcbnew.PAD.SMDMask())
        pad.SetShape(pcbnew.F_Cu, shapes[shape])
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(_nm(pcbnew, sx), _nm(pcbnew, sy)))
        if drill:
            pad.SetDrillSize(pcbnew.VECTOR2I(_nm(pcbnew, drill), _nm(pcbnew, drill)))
            through = True
        pad.SetFPRelativePosition(_pt(pcbnew, x, y))
        fp.Add(pad)
        box = [min(box[0], x - sx / 2), min(box[1], y - sy / 2), max(box[2], x + sx / 2), max(box[3], y + sy / 2)]
    fp.SetAttributes(pcbnew.FP_THROUGH_HOLE if through else pcbnew.FP_SMD)

    if outline:
        ox, oy = float(outline["x"]), float(outline["y"])
        frame = [ox, oy, ox + float(outline["width"]), oy + float(outline["height"])]
    else:
        frame = [box[0] - 0.5, box[1] - 0.5, box[2] + 0.5, box[3] + 0.5]
    for layer, width, grow in ((pcbnew.F_SilkS, 0.12, 0.0), (pcbnew.F_CrtYd, 0.05, 0.25)):
        rect = pcbnew.PCB_SHAPE(fp, pcbnew.SHAPE_T_RECTANGLE)
        rect.SetLayer(layer)
        rect.SetWidth(_nm(pcbnew, width))
        rect.SetStart(_pt(pcbnew, frame[0] - grow, frame[1] - grow))
        rect.SetEnd(_pt(pcbnew, frame[2] + grow, frame[3] + grow))
        fp.Add(rect)
    fp.Reference().SetPosition(_pt(pcbnew, (frame[0] + frame[2]) / 2, frame[1] - 1.5))
    fp.Value().SetPosition(_pt(pcbnew, (frame[0] + frame[2]) / 2, frame[3] + 1.5))
    fp.Value().SetLayer(pcbnew.F_Fab)

    lib_path.mkdir(parents=True, exist_ok=True)
    # An empty .pretty directory is guessed as another format on KiCad 10.
    # We are writing native KiCad footprints, so select that plugin explicitly.
    plugin = pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP)
    plugin.FootprintSave(str(lib_path), fp)
    if register:
        entry = [make("name", library), make("type", "KiCad"),
                 make("uri", f"${{KIPRJMOD}}/{library}.pretty"), make("options", ""), make("descr", "")]
        if table is None:
            text = f'(fp_lib_table\n\t(version 7)\n\t(lib {" ".join(_inline(f) for f in entry)})\n)\n'
        else:
            newline = "\r\n" if "\r\n" in table else "\n"
            text = _before_close(table, f'\t(lib {" ".join(_inline(f) for f in entry)}){newline}')
        table_path.write_bytes(text.encode("utf-8"))
    return {
        "lib_id": f"{library}:{name}",
        "file": str(lib_path / f"{name}{SUFFIX}"),
        "registered": register,
        "pads": len(pads),
        "outline": {"x": frame[0], "y": frame[1], "width": round(frame[2] - frame[0], 4),
                    "height": round(frame[3] - frame[1], 4)},
    }


# --- schematic to board --------------------------------------------------


def _netlist(install, schematic: Path) -> tuple[list[dict], dict[tuple[str, str], dict]]:
    """Parts and, per (reference, pin), the net: what the board needs of a schematic."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "board.net.xml"
        result = kcli.export_netlist(install, schematic, output=out)
        if not result.get("output_file"):
            raise ToolInputError(
                f"kicad-cli could not export the netlist of {schematic.name}: "
                f"{result.get('stderr') or result.get('stdout') or 'no output'}",
                remedy="Open the schematic in KiCad and check that it loads.",
            )
        root = ET.parse(out).getroot()
    parts = []
    for comp in root.findall("./components/comp"):
        props = {p.get("name", ""): p.get("value", "") for p in comp.findall("property")}
        sheet = comp.find("sheetpath")
        stamps = (comp.findtext("tstamps") or "").split()
        parts.append({
            "reference": comp.get("ref", ""),
            "value": (comp.findtext("value") or "").strip(),
            "footprint": (comp.findtext("footprint") or "").strip(),
            "path": ((sheet.get("tstamps", "/") if sheet is not None else "/") + (stamps[0] if stamps else "")),
            "sheet_name": sheet.get("names", "/") if sheet is not None else "/",
            "sheet_file": props.get("Sheetfile", ""),
            "on_board": "exclude_from_board" not in props,
        })
    pins = {}
    for net in root.findall("./nets/net"):
        for node in net.findall("node"):
            pins[(node.get("ref", ""), node.get("pin", ""))] = {
                "net": net.get("name", ""), "function": node.get("pinfunction", ""),
                "type": node.get("pintype", ""),
            }
    return parts, pins


def _load_library_footprint(pcbnew, index: FootprintIndex, lib_id: str):
    if ":" not in lib_id:
        return None
    library, name = lib_id.split(":", 1)
    entry = index.entries.get(library)
    if entry is None or entry.path is None or not (entry.path / f"{name}{SUFFIX}").is_file():
        return None
    fp = pcbnew.FootprintLoad(str(entry.path), name)
    if fp is not None:
        fp.SetFPIDAsString(lib_id)
    return fp


def _box(fp) -> tuple[float, float, float, float]:
    """A footprint's box without its texts: (x0, y0, x1, y1) in mm."""
    b = fp.GetBoundingBox(False)
    return _mm(b.GetLeft()), _mm(b.GetTop()), _mm(b.GetRight()), _mm(b.GetBottom())


def _park(pcbnew, board, fresh: list) -> None:
    """Set new footprints in rows under everything the board has, tallest first."""
    if not fresh:
        return
    fresh_ids = {fp.m_Uuid.AsString() for fp in fresh}
    placed = [fp for fp in board.GetFootprints() if fp.m_Uuid.AsString() not in fresh_ids]
    boxes = [_box(fp) for fp in placed]
    edges = board.GetBoardEdgesBoundingBox()
    if edges.GetWidth() > 0:
        boxes.append((_mm(edges.GetLeft()), _mm(edges.GetTop()), _mm(edges.GetRight()), _mm(edges.GetBottom())))
    left = min((b[0] for b in boxes), default=20.0)
    top = max((b[3] for b in boxes), default=20.0 - PARK_GAP) + PARK_GAP
    x, row = left, 0.0
    for fp in sorted(fresh, key=lambda f: -(_box(f)[3] - _box(f)[1])):
        x0, y0, x1, y1 = _box(fp)
        if x > left and x + (x1 - x0) > left + PARK_WIDTH:
            x, top, row = left, top + row + PARK_GAP, 0.0
        pos = fp.GetPosition()
        fp.SetPosition(_pt(pcbnew, _mm(pos.x) + x - x0, _mm(pos.y) + top - y0))
        x += (x1 - x0) + PARK_GAP
        row = max(row, y1 - y0)


def update_from_schematic(install, schematic: str | Path, board: str | Path | None = None) -> dict:
    """Bring a board in line with a schematic: its parts as footprints, its nets on their pads.

    A footprint is matched to its symbol by the symbol's identity, then by
    reference, so an annotated part keeps its place. A part whose Footprint
    field changed gets the new footprint where the old one stood. New
    footprints are parked in rows below the board. Footprints with no symbol
    behind them (mounting holes, logos) are left alone; ones whose symbol is
    gone are removed. Tracks stay as they are.
    """
    sch = Path(schematic)
    if not sch.is_file() or sch.suffix != ".kicad_sch":
        raise ToolInputError(f"Expected a .kicad_sch file, got {sch}.",
                             remedy="Pass the project's root schematic.")
    sch = sch.resolve()
    target = Path(board).resolve() if board else sch.with_suffix(".kicad_pcb")
    if target.suffix != ".kicad_pcb":
        raise ToolInputError(f"Expected a .kicad_pcb path, got {target.name}.",
                             remedy="Pass a .kicad_pcb path, or none for the one beside the schematic.")
    pcbnew = pcb_file._pcbnew()
    parts, pins = _netlist(install, sch)
    index = FootprintIndex(install, hierarchy.find_project_dir(sch) or sch.parent)

    with pcb_file._LOCK:
        created = not target.is_file()
        pcb = pcbnew.NewBoard(str(target)) if created else pcbnew.LoadBoard(str(target))
        by_path = {fp.GetPath().AsString(): fp for fp in pcb.GetFootprints() if fp.GetPath().AsString()}
        by_ref = _by_reference(pcb)

        report = {"added": [], "replaced": [], "updated": [], "removed": [],
                  "without_footprint": [], "unresolved": []}
        fresh, kept = [], set()
        for part in parts:
            ref = part["reference"]
            if not part["on_board"]:
                continue
            if not part["footprint"]:
                report["without_footprint"].append(ref)
                continue
            fp = by_path.get(part["path"]) or by_ref.get(ref)
            if fp is not None and fp.m_Uuid.AsString() in kept:
                fp = None
            if fp is None or str(fp.GetFPIDAsString()) != part["footprint"]:
                new = _load_library_footprint(pcbnew, index, part["footprint"])
                if new is None:
                    report["unresolved"].append({"reference": ref, "footprint": part["footprint"]})
                    if fp is not None:
                        kept.add(fp.m_Uuid.AsString())
                    continue
                pcb.Add(new)
                if fp is None:
                    fresh.append(new)
                    report["added"].append(ref)
                else:
                    if fp.IsFlipped():
                        new.SetLayerAndFlip(pcbnew.B_Cu)
                    new.SetOrientationDegrees(fp.GetOrientationDegrees())
                    new.SetPosition(fp.GetPosition())
                    pcb.Remove(fp)
                    report["replaced"].append(ref)
                fp = new
            elif (str(fp.GetReference()), str(fp.GetValue()), fp.GetPath().AsString()) != (ref, part["value"], part["path"]):
                report["updated"].append(ref)
            kept.add(fp.m_Uuid.AsString())
            fp.SetReference(ref)
            fp.SetValue(part["value"])
            fp.SetPath(pcbnew.KIID_PATH(part["path"]))
            fp.SetSheetname(part["sheet_name"])
            fp.SetSheetfile(part["sheet_file"])
            changed = False
            for pad in fp.Pads():
                pin = pins.get((ref, str(pad.GetNumber())))
                name = pin["net"] if pin else ""
                if str(pad.GetNetname()) != name:
                    changed = True
                    if name:
                        net = pcb.FindNet(name)
                        if net is None:
                            net = pcbnew.NETINFO_ITEM(pcb, name)
                            pcb.Add(net)
                        pad.SetNet(net)
                    else:
                        pad.SetNetCode(0)
                if pin:
                    pad.SetPinFunction(pin["function"])
                    pad.SetPinType(pin["type"])
            if changed and ref not in report["added"] + report["replaced"] + report["updated"]:
                report["updated"].append(ref)

        for fp in list(pcb.GetFootprints()):
            if fp.m_Uuid.AsString() not in kept and fp.GetPath().AsString():
                report["removed"].append(str(fp.GetReference()))
                pcb.Remove(fp)
        _park(pcbnew, pcb, fresh)
        _save(pcbnew, pcb, target)
        nets = sorted({str(pad.GetNetname()) for fp in pcb.GetFootprints() for pad in fp.Pads() if pad.GetNetname()})

    out = {"board": str(target), "created": created, "footprints": len(kept), "nets": len(nets),
           **{k: v for k, v in report.items()}}
    notes = []
    if report["without_footprint"]:
        notes.append("Parts with an empty Footprint field are not on the board; set it with "
                     "set_symbol_fields and run this again.")
    if report["unresolved"]:
        notes.append("Some Footprint fields name a library or footprint that does not resolve; "
                     "search_footprints finds the right Library:Name.")
    if report["added"]:
        notes.append("New footprints are parked below the board; place_footprints sets them in place.")
    if notes:
        out["notes"] = notes
    return out


# --- placement and outline -----------------------------------------------


def place_footprints(board: str | Path, placements: list[dict]) -> dict:
    """Move footprints: each {"reference", "x", "y", "angle", "side"}, in mm and degrees.

    `x`/`y` put the footprint's origin there; leave both out to keep it.
    `angle` is counter-clockwise as KiCad shows it; `side` front or back.
    Tracks do not follow.
    """
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        rows = []
        for i, spec in enumerate(placements):
            if "reference" not in spec:
                raise ToolInputError(f"Placement {i + 1} has no reference.",
                                     remedy='Each needs "reference" and any of "x"/"y", "angle", "side".')
            fp = _find(pcb, spec["reference"])
            side = spec.get("side")
            if side is not None:
                if side not in ("front", "back"):
                    raise ToolInputError(f"{spec['reference']}: side is {side!r}.",
                                         remedy='Use "front" or "back".')
                if (side == "back") != bool(fp.IsFlipped()):
                    fp.SetLayerAndFlip(pcbnew.B_Cu if side == "back" else pcbnew.F_Cu)
            if "angle" in spec:
                fp.SetOrientationDegrees(float(spec["angle"]))
            if ("x" in spec) != ("y" in spec):
                raise ToolInputError(f"{spec['reference']}: x and y go together.",
                                     remedy="Pass both, or neither to keep the position.")
            if "x" in spec:
                fp.SetPosition(_pt(pcbnew, spec["x"], spec["y"]))
            x0, y0, x1, y1 = _box(fp)
            rows.append({
                "reference": str(fp.GetReference()),
                "x": _mm(fp.GetPosition().x), "y": _mm(fp.GetPosition().y),
                "angle": round(fp.GetOrientationDegrees(), 4),
                "side": "back" if fp.IsFlipped() else "front",
                "box": {"x": x0, "y": y0, "width": round(x1 - x0, 4), "height": round(y1 - y0, 4)},
            })
        _save(pcbnew, pcb, path)
    return {"board": str(path), "placed": rows}


def add_footprint(install, board: str | Path, lib_id: str, reference: str, x: float, y: float,
                  angle: float = 0.0, value: str | None = None) -> dict:
    """Put a footprint with no symbol on the board: a mounting hole, a fiducial, a logo."""
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        if reference in _by_reference(pcb):
            raise ToolInputError(f"The board already has a footprint {reference}.",
                                 remedy="Pick another reference, or move it with place_footprints.")
        index = FootprintIndex(install, hierarchy.find_project_dir(path) or path.parent)
        fp = _load_library_footprint(pcbnew, index, lib_id)
        if fp is None:
            raise ToolInputError(f"Footprint {lib_id!r} does not resolve.",
                                 remedy="Call search_footprints to find the right Library:Name.")
        pcb.Add(fp)
        fp.SetReference(reference)
        fp.SetValue(value if value is not None else lib_id.split(":", 1)[1])
        fp.SetAttributes(fp.GetAttributes() | pcbnew.FP_BOARD_ONLY | pcbnew.FP_EXCLUDE_FROM_BOM)
        fp.SetOrientationDegrees(float(angle))
        fp.SetPosition(_pt(pcbnew, x, y))
        _save(pcbnew, pcb, path)
    return {"board": str(path), "reference": reference, "lib_id": lib_id, "x": x, "y": y, "angle": angle}


def set_outline(board: str | Path, x: float, y: float, width: float, height: float,
                corner_radius: float = 0.0) -> dict:
    """Replace the board's outline with a rectangle, its top left corner at (x, y)."""
    if width <= 0 or height <= 0:
        raise ToolInputError("The outline needs a positive width and height.",
                             remedy="Pass the board's size in mm.")
    if corner_radius < 0 or corner_radius * 2 > min(width, height):
        raise ToolInputError(f"A {corner_radius} mm corner does not fit a {width} x {height} mm board.",
                             remedy="Use a radius of at most half the shorter side.")
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        old = [d for d in pcb.GetDrawings()
               if isinstance(d, pcbnew.PCB_SHAPE) and d.GetLayer() == pcbnew.Edge_Cuts]
        for d in old:
            pcb.Remove(d)
        rect = pcbnew.PCB_SHAPE(pcb, pcbnew.SHAPE_T_RECTANGLE)
        rect.SetLayer(pcbnew.Edge_Cuts)
        rect.SetWidth(_nm(pcbnew, 0.1))
        rect.SetStart(_pt(pcbnew, x, y))
        rect.SetEnd(_pt(pcbnew, x + width, y + height))
        if corner_radius:
            rect.SetCornerRadius(_nm(pcbnew, corner_radius))
        pcb.Add(rect)
        _save(pcbnew, pcb, path)
    return {"board": str(path), "outline": {"x": x, "y": y, "width": width, "height": height,
                                            "corner_radius": corner_radius},
            "replaced_shapes": len(old)}


# --- net classes ---------------------------------------------------------

CLASS_KEYS = ("name", "clearance", "track_width", "via_diameter", "via_drill", "nets")


def set_net_classes(board: str | Path, classes: list[dict]) -> dict:
    """Define net classes and which nets they take, in the project beside the board.

    Each class is {"name", "clearance", "track_width", "via_diameter",
    "via_drill", "nets"}: sizes in mm, left as they are (or as Default has
    them) when absent; `nets` a list of net names or globs ("/HV_*"), which
    replaces the patterns the class had. Earlier classes win a net that two
    of them claim. "Default" changes the class every other net falls in.
    """
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        settings = pcb.GetDesignSettings().m_NetSettings
        names = [str(c.get("name", "")) for c in classes]
        if any(not n for n in names) or len(set(names)) != len(names):
            raise ToolInputError("Each net class needs a name of its own.",
                                 remedy='Pass {"name": "HV", "clearance": 1.5, ...}.')
        for spec in classes:
            unknown = set(spec) - set(CLASS_KEYS)
            if unknown:
                raise ToolInputError(f"Net class {spec['name']}: unknown keys {sorted(unknown)}.",
                                     remedy=f"Use {', '.join(CLASS_KEYS)}.")
        kept = _project_patterns(path)
        if kept is None:
            kept = [(str(p.GetPattern()) if hasattr(p, "GetPattern") else str(p[0].GetPattern()), str(c))
                    for p, c in _patterns(settings)]
        kept = [(pattern, cls) for pattern, cls in kept if cls not in names]
        settings.ClearNetclassPatternAssignments()
        setters = {"clearance": "SetClearance", "track_width": "SetTrackWidth",
                   "via_diameter": "SetViaDiameter", "via_drill": "SetViaDrill"}
        for priority, spec in enumerate(classes):
            name = spec["name"]
            if name == "Default":
                cls = settings.GetDefaultNetclass()
            elif settings.HasNetclass(name):
                cls = settings.GetNetClassByName(name)
            else:
                cls = pcbnew.NETCLASS(name)
            for key, setter in setters.items():
                if spec.get(key) is not None:
                    getattr(cls, setter)(_nm(pcbnew, spec[key]))
            if name != "Default":
                cls.SetPriority(priority)
                settings.SetNetclass(name, cls)
                for pattern in spec.get("nets") or []:
                    settings.SetNetclassPatternAssignment(str(pattern), name)
        for pattern, cls in kept:
            settings.SetNetclassPatternAssignment(pattern, cls)
        _save(pcbnew, pcb, path)

        pcb = pcbnew.LoadBoard(str(path))
        assigned: dict[str, list[str]] = {}
        for net in pcb.GetNetsByName().values():
            if net.GetNetCode() > 0:
                assigned.setdefault(str(net.GetNetClassName()), []).append(str(net.GetNetname()))
    return {"board": str(path), "project": str(path.with_suffix(".kicad_pro")),
            "classes": {name: sorted(nets) for name, nets in sorted(assigned.items())}}


def _project_patterns(board: Path) -> list[tuple[str, str]] | None:
    """The pattern assignments saved in the project beside the board, or None without one.

    KiCad 10.0.8 hands GetNetclassPatternAssignments back as a bare
    SwigPyObject, so the project file is the reliable record of them.
    """
    try:
        data = json.loads(board.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = (data.get("net_settings") or {}).get("netclass_patterns")
    if not isinstance(rows, list):
        return None
    return [(str(r["pattern"]), str(r["netclass"])) for r in rows
            if isinstance(r, dict) and "pattern" in r and "netclass" in r]


def _patterns(settings) -> list[tuple]:
    try:
        return list(settings.GetNetclassPatternAssignments())
    except Exception:  # noqa: BLE001 - the SWIG wrapper of this vector varies by build
        return []


# --- routing -------------------------------------------------------------


def _pad_shape(pcbnew, pad, layers: tuple[int, ...]) -> autoroute.Shape:
    layer = pad.GetPrincipalLayer()
    size = pad.GetSize(layer)
    sx, sy = _mm(size.x), _mm(size.y)
    pos = pad.GetPosition()
    x, y = _mm(pos.x), _mm(pos.y)
    net = str(pad.GetNetname()) or None
    shape = pad.GetShape(layer)
    angle = pad.GetOrientationDegrees()
    if shape == pcbnew.PAD_SHAPE_CIRCLE or (shape == pcbnew.PAD_SHAPE_OVAL and sx == sy):
        return autoroute.Shape("circle", (x, y, sx / 2), layers, net)
    if shape == pcbnew.PAD_SHAPE_OVAL:
        r = min(sx, sy) / 2
        half = max(sx, sy) / 2 - r
        # KiCad turns pads clockwise for a positive angle, with y pointing down.
        a = math.radians(-angle if sx > sy else -angle + 90)
        dx, dy = half * math.cos(a), half * math.sin(a)
        return autoroute.Shape("capsule", (x - dx, y - dy, x + dx, y + dy, r), layers, net)
    return autoroute.Shape("rect", (x, y, sx / 2, sy / 2, -angle), layers, net)


def _copper_index(pcbnew, pcb) -> dict[int, int]:
    """KiCad layer id to router layer: front 0, back last, inner in order."""
    ids = [layer for layer in pcb.GetEnabledLayers().CuStack()]
    return {layer: i for i, layer in enumerate(ids)}


def _is_via(pcbnew, track) -> bool:
    return track.GetClass() == "PCB_VIA"


def unroute(board: str | Path, nets: list[str] | None = None) -> dict:
    """Remove tracks and vias: of the nets matching `nets` (names or globs), or all."""
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        gone = 0
        for track in list(pcb.GetTracks()):
            if nets is None or any(fnmatch.fnmatchcase(str(track.GetNetname()), n) for n in nets):
                pcb.Remove(track)
                gone += 1
        _save(pcbnew, pcb, path)
    return {"board": str(path), "removed": gone}


def route(
    board: str | Path,
    nets: list[str] | None = None,
    exclude: list[str] | None = None,
    first: list[str] | None = None,
    grid: float = 0.635,
    via_cost: float = 10.0,
    layer_cost: list[float] | None = None,
    allow_vias: bool = True,
    passes: int = 4,
    edge_clearance: float | None = None,
) -> dict:
    """Route nets with `autoroute`, at each net class's width and clearance.

    `nets` and `exclude` are names or globs; tracks the chosen nets already
    have are taken up first, everything else on the board stays and is
    routed around. `first` puts nets at the head of the order (short nets
    lead otherwise). Zones are not seen: fill them after routing.
    """
    if grid <= 0:
        raise ToolInputError("The routing grid must be positive.", remedy="Try 0.635 or 0.5 mm.")
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        edges = pcb.GetBoardEdgesBoundingBox()
        if edges.GetWidth() <= 0 or edges.GetHeight() <= 0:
            raise ToolInputError("The board has no outline, so there is no area to route in.",
                                 remedy="Draw one with set_board_outline first.")
        bounds = (_mm(edges.GetLeft()), _mm(edges.GetTop()), _mm(edges.GetRight()), _mm(edges.GetBottom()))
        index = _copper_index(pcbnew, pcb)
        every = tuple(range(len(index)))
        settings = pcb.GetDesignSettings()
        if edge_clearance is None:
            edge_clearance = _mm(settings.m_CopperEdgeClearance)

        pads, holes, rules = [], [], {}
        for fp in pcb.GetFootprints():
            for pad in fp.Pads():
                net = str(pad.GetNetname())
                through = pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
                layers = every if through else tuple(
                    i for layer, i in index.items() if pad.IsOnLayer(layer))
                shape = _pad_shape(pcbnew, pad, layers)
                if net and layers and pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
                    pos = pad.GetPosition()
                    pads.append(autoroute.Pad(net, f"{fp.GetReference()}.{pad.GetNumber()}",
                                              _mm(pos.x), _mm(pos.y), shape))
                else:
                    holes.append(shape)
        default = pcb.GetDesignSettings().m_NetSettings.GetDefaultNetclass()
        default_rule = _rule(default)
        for net in pcb.GetNetsByName().values():
            if net.GetNetCode() > 0:
                rules[str(net.GetNetname())] = _rule(net.GetNetClass())

        names = sorted({p.net for p in pads})
        chosen = [n for n in names
                  if (nets is None or any(fnmatch.fnmatchcase(n, g) for g in nets))
                  and not any(fnmatch.fnmatchcase(n, g) for g in exclude or [])]
        if nets and not chosen:
            raise ToolInputError(f"No net on the board matches {nets}.",
                                 remedy="Call list_nets for the names; sheet nets start with '/'.")
        by_net: dict[str, list] = {}
        for p in pads:
            by_net.setdefault(p.net, []).append(p)
        order = sorted((n for n in chosen if len(by_net[n]) > 1),
                       key=lambda n: (autoroute._span(by_net[n]), n))
        head = [n for g in first or [] for n in order if fnmatch.fnmatchcase(n, g)]
        order = list(dict.fromkeys(head + order))

        taken_up = 0
        fixed = list(holes)
        for track in list(pcb.GetTracks()):
            net = str(track.GetNetname())
            if net in chosen:
                pcb.Remove(track)
                taken_up += 1
                continue
            if _is_via(pcbnew, track):
                pos = track.GetPosition()
                fixed.append(autoroute.Shape(
                    "circle", (_mm(pos.x), _mm(pos.y), _mm(track.GetWidth(pcbnew.F_Cu)) / 2), every, net or None))
            elif track.GetLayer() in index:
                a, b = track.GetStart(), track.GetEnd()
                fixed.append(autoroute.Shape(
                    "capsule", (_mm(a.x), _mm(a.y), _mm(b.x), _mm(b.y), _mm(track.GetWidth()) / 2),
                    (index[track.GetLayer()],), net or None))

        result = autoroute.route(
            bounds, pads, fixed, rules, default_rule, nets=order, grid=grid, layers=len(index),
            edge_clearance=edge_clearance, via_cost=via_cost,
            layer_cost=tuple(layer_cost) if layer_cost else None, allow_vias=allow_vias, passes=passes,
        )

        layer_of = {i: layer for layer, i in index.items()}
        length: dict[str, float] = {}
        for net, layer, x1, y1, x2, y2 in result.tracks:
            track = pcbnew.PCB_TRACK(pcb)
            track.SetStart(_pt(pcbnew, x1, y1))
            track.SetEnd(_pt(pcbnew, x2, y2))
            track.SetWidth(_nm(pcbnew, rules.get(net, default_rule).width))
            track.SetLayer(layer_of[layer])
            track.SetNet(pcb.FindNet(net))
            pcb.Add(track)
            length[net] = length.get(net, 0.0) + math.hypot(x2 - x1, y2 - y1)
        for net, x, y in result.vias:
            rule = rules.get(net, default_rule)
            via = pcbnew.PCB_VIA(pcb)
            via.SetPosition(_pt(pcbnew, x, y))
            via.SetWidth(pcbnew.F_Cu, _nm(pcbnew, rule.via_diameter))
            via.SetDrill(_nm(pcbnew, rule.via_drill))
            via.SetNet(pcb.FindNet(net))
            pcb.Add(via)
        _save(pcbnew, pcb, path)

    failed: dict[str, list[str]] = {}
    for net, pad in result.failed:
        failed.setdefault(net, []).append(pad)
    out = {
        "board": str(path),
        "nets_routed": len([n for n in order if n not in failed]),
        "nets_failed": len(failed),
        "tracks": len(result.tracks),
        "vias": len(result.vias),
        "length_mm": round(sum(length.values()), 1),
        "passes": result.passes,
        "taken_up": taken_up,
        "grid": grid,
        "failed": [{"net": n, "pads_left_off": ps} for n, ps in failed.items()],
    }
    if failed:
        out["notes"] = ["Pads left off could not be reached at the net's width and clearance: "
                        "move parts apart, use a finer grid, or route those nets first with `first`."]
    return out


def _rule(netclass) -> autoroute.Rule:
    return autoroute.Rule(
        width=_mm(netclass.GetTrackWidth()), clearance=_mm(netclass.GetClearance()),
        via_diameter=_mm(netclass.GetViaDiameter()), via_drill=_mm(netclass.GetViaDrill()),
    )
