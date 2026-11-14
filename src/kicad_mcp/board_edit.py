"""Build and edit a .kicad_pcb through pcbnew, with KiCad closed.

The read tools answer questions about a board somebody laid out. These make
one: the schematic's parts and nets onto a board (what KiCad's "Update PCB
from Schematic" does, which kicad-cli does not offer), footprints to their
places, the outline, net classes, and tracks from `autoroute`. Every function
loads the file, changes it and saves it, so each call leaves a board KiCad
opens. `route_live` plans on a copy of the board open in KiCad and hands the
tracks to pcb_live, which applies them there.

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

from . import autoroute, cli as kcli, drc_rules, hierarchy, pcb_file, pcb_live, sexpr
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


def _pad_points(fp) -> dict[str, tuple[int, int, str]]:
    return {f"{i}:{p.GetNumber()}": (p.GetPosition().x, p.GetPosition().y, str(p.GetNetname()))
            for i, p in enumerate(fp.Pads())}


def place_footprints(board: str | Path, placements: list[dict], drag_tracks: bool = False) -> dict:
    """Move footprints: each {"reference", "x", "y", "angle", "side"}, in mm and degrees.

    `x`/`y` put the footprint's origin there; leave both out to keep it.
    `angle` is counter-clockwise as KiCad shows it; `side` front or back.
    With `drag_tracks`, a track end on one of the footprint's pads moves with
    the pad; otherwise tracks stay where they are.
    """
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        rows, shift = [], {}
        for i, spec in enumerate(placements):
            if "reference" not in spec:
                raise ToolInputError(f"Placement {i + 1} has no reference.",
                                     remedy='Each needs "reference" and any of "x"/"y", "angle", "side".')
            fp = _find(pcb, spec["reference"])
            before = _pad_points(fp)
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
            after = _pad_points(fp)
            shift.update({v: after[k][:2] for k, v in before.items() if k in after})
            x0, y0, x1, y1 = _box(fp)
            rows.append({
                "reference": str(fp.GetReference()),
                "x": _mm(fp.GetPosition().x), "y": _mm(fp.GetPosition().y),
                "angle": round(fp.GetOrientationDegrees(), 4),
                "side": "back" if fp.IsFlipped() else "front",
                "box": {"x": x0, "y": y0, "width": round(x1 - x0, 4), "height": round(y1 - y0, 4)},
            })
        dragged = 0
        if drag_tracks:
            for track in pcb.GetTracks():
                if _is_via(pcbnew, track):
                    continue
                hit = False
                for get, put in ((track.GetStart, track.SetStart), (track.GetEnd, track.SetEnd)):
                    p = get()
                    to = shift.get((p.x, p.y, str(track.GetNetname())))
                    if to is not None and to != (p.x, p.y):
                        put(pcbnew.VECTOR2I(*to))
                        hit = True
                dragged += hit
        _save(pcbnew, pcb, path)
    out = {"board": str(path), "placed": rows}
    if drag_tracks:
        out["tracks_dragged"] = dragged
    return out


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


_PAD_TYPES = {"PAD_ATTRIB_PTH": "Through-hole", "PAD_ATTRIB_SMD": "SMD",
              "PAD_ATTRIB_CONN": "Edge connector", "PAD_ATTRIB_NPTH": "NPTH, mechanical"}


class _BoardPolicy(autoroute.Policy):
    """The net classes, then the board's custom rules (.kicad_dru) over them."""

    def __init__(self, rules, default_rule, edge_clearance, allow_vias, dru: drc_rules.Rules,
                 netclass_of: dict[str, str], layer_names: dict[int, str],
                 min_clearance: float, hole_clearance: float) -> None:
        super().__init__(rules, default_rule, edge_clearance, allow_vias)
        self.dru = dru
        self.netclass_of = netclass_of
        self.layer_names = layer_names
        self.min_clearance = min_clearance
        self.hole_clearance = hole_clearance
        self._rules: dict = {}
        self._gaps: dict = {}

    def _item(self, kind: str, net: str | None, layer: str | None = None) -> drc_rules.Item:
        return drc_rules.Item(kind, net or "", self.netclass_of.get(net or "", ""), layer)

    def rule(self, net):
        if net not in self._rules:
            base = super().rule(net)
            track = self._item("Track", net)
            opt = self.dru.value("track_width", track, bound="opt")
            low = self.dru.value("track_width", track, bound="min")
            width = opt if opt is not None else max(base.width, low or 0.0)
            self._rules[net] = autoroute.Rule(width, base.clearance, base.via_diameter, base.via_drill)
        return self._rules[net]

    def clearance(self, net, shape):
        layer = self.layer_names.get(shape.layers[0]) if len(shape.layers) == 1 else None
        other = shape.tag if isinstance(shape.tag, drc_rules.Item) else self._item("Track", shape.net, layer)
        key = (net, other)
        if key not in self._gaps:
            track = self._item("Track", net, layer)
            if other.pad_type == "NPTH, mechanical":
                v = self.dru.value("hole_clearance", track, other)
                gap = self.hole_clearance if v is None else v
            else:
                v = self.dru.value("clearance", track, other)
                gap = super().clearance(net, shape) if v is None else v
                phys = self.dru.value("physical_clearance", track, other)
                gap = max(gap, phys or 0.0, self.min_clearance)
            self._gaps[key] = gap
        return self._gaps[key]

    def edge(self, net):
        v = self.dru.value("edge_clearance", self._item("Track", net))
        return self.edge_clearance if v is None else v

    def layer_ok(self, net, layer):
        return not self.dru.disallowed("track", self._item("Track", net, self.layer_names.get(layer)))

    def vias(self, net):
        return self.allow_vias and not self.dru.disallowed("via", self._item("Via", net))


def _islands(shapes: list[autoroute.Shape], ends: list[list[tuple]]) -> list[int]:
    """For copper of one net, which pieces touch: a group number per shape.

    `ends[i]` are the points of shape i that join whatever copper holds them:
    a track's two ends, a pad's or a via's centre."""
    parent = list(range(len(shapes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, points in enumerate(ends):
        for x, y in points:
            for j, other in enumerate(shapes):
                if j != i and set(shapes[i].layers) & set(other.layers) and other.distance(x, y) <= 0.001:
                    parent[find(i)] = find(j)
    return [find(i) for i in range(len(shapes))]


def _kept_points(net: str, group, shape: autoroute.Shape, grid: float) -> list[autoroute.Pad]:
    """Lattice nodes on a kept track, where a new track may join it."""
    x1, y1, x2, y2, r = shape.geometry
    n = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / (grid / 2)))
    seen, out = set(), []
    for i in range(n + 1):
        x, y = x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n
        node = (round(x / grid), round(y / grid))
        if node in seen:
            continue
        seen.add(node)
        nx, ny = node[0] * grid, node[1] * grid
        if shape.distance(nx, ny) <= 0:
            out.append(autoroute.Pad(net, f"~{net}", nx, ny,
                                     autoroute.Shape("circle", (nx, ny, 0.001), shape.layers, net), group))
    return out


def _plan_route(pcbnew, pcb, dru_path: Path | None, nets, exclude, first, grid, via_cost, layer_cost,
                allow_vias, passes, edge_clearance, keep_existing):
    """Route `pcb` in memory. Returns (report, tracks, vias, taken-up tracks)."""
    if grid <= 0:
        raise ToolInputError("The routing grid must be positive.", remedy="Try 0.635 or 0.5 mm.")
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

    # Net classes through NET_SETTINGS: NETINFO_ITEM.GetNetClass() comes back
    # as a bare SwigPyObject in KiCad 10's bindings.
    net_settings = settings.m_NetSettings
    rules, netclass_of = {}, {}
    for net in pcb.GetNetsByName().values():
        if net.GetNetCode() > 0:
            name = str(net.GetNetname())
            nc = net_settings.GetEffectiveNetClass(name)
            rules[name] = _rule(nc)
            netclass_of[name] = str(nc.GetName())
    default_rule = _rule(net_settings.GetDefaultNetclass())
    dru = drc_rules.load(dru_path) if dru_path else drc_rules.Rules()
    layer_names = {i: str(pcb.GetLayerName(layer)) for layer, i in index.items()}
    policy = _BoardPolicy(rules, default_rule, edge_clearance, allow_vias, dru, netclass_of, layer_names,
                          _mm(settings.m_MinClearance), _mm(settings.m_HoleClearance))

    pads, holes = [], []
    for fp in pcb.GetFootprints():
        ref = str(fp.GetReference())
        for pad in fp.Pads():
            net = str(pad.GetNetname())
            through = pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
            layers = every if through else tuple(
                i for layer, i in index.items() if pad.IsOnLayer(layer))
            shape = _pad_shape(pcbnew, pad, layers)
            kind = next((v for k, v in _PAD_TYPES.items() if getattr(pcbnew, k, None) == pad.GetAttribute()), "")
            tag = drc_rules.Item("Pad", net, netclass_of.get(net, ""), None, ref, kind)
            shape = autoroute.Shape(shape.kind, shape.geometry, shape.layers, shape.net, tag)
            if net and layers and pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:
                pos = pad.GetPosition()
                pads.append(autoroute.Pad(net, f"{ref}.{pad.GetNumber()}", _mm(pos.x), _mm(pos.y), shape))
            else:
                holes.append(shape)

    names = sorted({p.net for p in pads})
    chosen = [n for n in names
              if (nets is None or any(fnmatch.fnmatchcase(n, g) for g in nets))
              and not any(fnmatch.fnmatchcase(n, g) for g in exclude or [])]
    if nets and not chosen:
        raise ToolInputError(f"No net on the board matches {nets}.",
                             remedy="Call list_nets for the names; sheet nets start with '/'.")

    taken_up, fixed, kept = [], list(holes), {}
    for track in list(pcb.GetTracks()):
        net = str(track.GetNetname())
        if net in chosen and not keep_existing:
            taken_up.append(track)
            continue
        nc = netclass_of.get(net, "")
        if _is_via(pcbnew, track):
            pos = track.GetPosition()
            shape = autoroute.Shape("circle", (_mm(pos.x), _mm(pos.y), _mm(track.GetWidth(pcbnew.F_Cu)) / 2),
                                    every, net or None, drc_rules.Item("Via", net, nc))
            ends = [(_mm(pos.x), _mm(pos.y))]
        elif track.GetLayer() in index:
            a, b = track.GetStart(), track.GetEnd()
            shape = autoroute.Shape("capsule", (_mm(a.x), _mm(a.y), _mm(b.x), _mm(b.y), _mm(track.GetWidth()) / 2),
                                    (index[track.GetLayer()],), net or None,
                                    drc_rules.Item("Track", net, nc, layer_names[index[track.GetLayer()]]))
            ends = [(_mm(a.x), _mm(a.y)), (_mm(b.x), _mm(b.y))]
        else:
            continue
        fixed.append(shape)
        if net in chosen:
            kept.setdefault(net, []).append((shape, ends))

    # Kept copper joins pads into groups; points on it are where new tracks may land.
    if keep_existing:
        for net, pieces in kept.items():
            own = [p for p in pads if p.net == net]
            shapes = [p.shape for p in own] + [s for s, _ in pieces]
            ends = [[(p.x, p.y)] for p in own] + [e for _, e in pieces]
            group = _islands(shapes, ends)
            sizes = {g: group.count(g) for g in group}
            for k, p in enumerate(own):
                if sizes[group[k]] > 1:
                    pads[pads.index(p)] = autoroute.Pad(p.net, p.name, p.x, p.y, p.shape, (net, group[k]))
            for k, (shape, _) in enumerate(pieces):
                g = (net, group[len(own) + k])
                if shape.kind == "capsule":
                    pads.extend(_kept_points(net, g, shape, grid))
                else:
                    x, y, _r = shape.geometry
                    pads.append(autoroute.Pad(net, f"~{net}", x, y,
                                              autoroute.Shape("circle", (x, y, 0.001), shape.layers, net), g))

    by_net: dict[str, list] = {}
    for p in pads:
        by_net.setdefault(p.net, []).append(p)
    order = sorted((n for n in chosen if autoroute._joins(by_net[n]) > 0),
                   key=lambda n: (autoroute._span(by_net[n]), n))
    head = [n for g in first or [] for n in order if fnmatch.fnmatchcase(n, g)]
    order = list(dict.fromkeys(head + order))

    result = autoroute.route(
        bounds, pads, fixed, nets=order, grid=grid, layers=len(index), via_cost=via_cost,
        layer_cost=tuple(layer_cost) if layer_cost else None, passes=passes, policy=policy,
    )
    layer_of = {i: layer for layer, i in index.items()}
    tracks = [(net, layer_of[layer], x1, y1, x2, y2, policy.rule(net).width)
              for net, layer, x1, y1, x2, y2 in result.tracks]
    vias = [(net, x, y, policy.rule(net).via_diameter, policy.rule(net).via_drill) for net, x, y in result.vias]

    failed: dict[str, list[str]] = {}
    for net, pad in result.failed:
        failed.setdefault(net, []).append(pad)
    length = round(sum(math.hypot(t[4] - t[2], t[5] - t[3]) for t in tracks), 1)
    out = {
        "nets_routed": len([n for n in order if n not in failed]),
        "nets_failed": len(failed),
        "tracks": len(tracks),
        "vias": len(vias),
        "length_mm": length,
        "passes": result.passes,
        "taken_up": len(taken_up),
        "grid": grid,
        "failed": [{"net": n, "pads_left_off": ps} for n, ps in failed.items()],
    }
    if dru_path and dru.rules:
        out["custom_rules"] = len(dru.rules)
    if dru.skipped:
        out["rules_not_understood"] = dru.skipped
    if failed:
        out["notes"] = ["Pads left off could not be reached at the net's width and clearance: "
                        "move parts apart, use a finer grid, or route those nets first with `first`."]
    return out, tracks, vias, taken_up


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
    keep_existing: bool = False,
    dry_run: bool = False,
) -> dict:
    """Route nets with `autoroute`, at each net class's width and clearance and the board's custom rules.

    `nets` and `exclude` are names or globs. Without `keep_existing` the
    tracks the chosen nets already have are taken up first; with it they stay
    and only the missing connections are added. Zones are not seen.
    """
    with pcb_file._LOCK:
        pcbnew, pcb, path = _open(board)
        out, tracks, vias, taken_up = _plan_route(
            pcbnew, pcb, path.with_suffix(".kicad_dru"), nets, exclude, first, grid, via_cost,
            layer_cost, allow_vias, passes, edge_clearance, keep_existing)
        if not dry_run:
            for track in taken_up:
                pcb.Remove(track)
            for net, layer, x1, y1, x2, y2, width in tracks:
                track = pcbnew.PCB_TRACK(pcb)
                track.SetStart(_pt(pcbnew, x1, y1))
                track.SetEnd(_pt(pcbnew, x2, y2))
                track.SetWidth(_nm(pcbnew, width))
                track.SetLayer(layer)
                track.SetNet(pcb.FindNet(net))
                pcb.Add(track)
            for net, x, y, diameter, drill in vias:
                via = pcbnew.PCB_VIA(pcb)
                via.SetPosition(_pt(pcbnew, x, y))
                via.SetWidth(pcbnew.F_Cu, _nm(pcbnew, diameter))
                via.SetDrill(_nm(pcbnew, drill))
                via.SetNet(pcb.FindNet(net))
                pcb.Add(via)
            _save(pcbnew, pcb, path)
    return {"board": str(path), "dry_run": dry_run, **out}


def route_live(live, path: Path, dry_run: bool = False, **kwargs) -> dict:
    """`route` on the board open in KiCad: planned on a saved copy, applied as one undo step."""
    with pcb_file._LOCK, tempfile.TemporaryDirectory(prefix="kicad-mcp-route-") as tmp:
        copy = Path(tmp) / path.name
        live.save_as(str(copy), overwrite=True, include_project=True)
        pcbnew = pcb_file._pcbnew()
        pcb = pcbnew.LoadBoard(str(copy))
        if pcb is None:
            raise ToolInputError("pcbnew could not load a copy of the open board.",
                                 remedy="Check that the board opens in KiCad 10.")
        out, tracks, vias, taken_up = _plan_route(pcbnew, pcb, path.with_suffix(".kicad_dru"), **kwargs)
        removed = [t.m_Uuid.AsString() for t in taken_up]
        layers = {layer: str(pcb.GetLayerName(layer)) for layer in {t[1] for t in tracks}}
        tracks = [(net, layers[layer], *rest) for net, layer, *rest in tracks]
    if not dry_run:
        pcb_live.apply_route(live, removed, tracks, vias)
    return {"board": str(path), "live": True, "dry_run": dry_run, **out}


def _rule(netclass) -> autoroute.Rule:
    return autoroute.Rule(
        width=_mm(netclass.GetTrackWidth()), clearance=_mm(netclass.GetClearance()),
        via_diameter=_mm(netclass.GetViaDiameter()), via_drill=_mm(netclass.GetViaDrill()),
    )
