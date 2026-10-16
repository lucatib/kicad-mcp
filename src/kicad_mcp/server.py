"""MCP server exposing KiCad 10.

Tool design principle: a curated surface for the things people actually do,
plus `run_kicad_script` for everything else. Wrapping every API method one-to-one
would produce hundreds of tools and measurably worse tool selection; the escape
hatch gives full coverage without that cost.

Errors are returned as data, never raised as protocol errors. A model can act on
{"error": ..., "remedy": "open the PCB editor"}; it cannot act on a traceback.
"""

from __future__ import annotations

import fnmatch
import functools
import traceback
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import cli as kcli
from . import hierarchy, pcb_common, pcb_file, pcb_live, units
from .discovery import find_install, find_installs
from .editor import SchematicEditor, add_decoupling_capacitors as _add_decoupling_capacitors
from .errors import KicadMcpError, NoEditorOpenError
from .footprints import FootprintIndex, footprint_warning
from .generator import generate_pinout_schematic
from .library import create_symbol
from .schematic import Schematic, find_project_schematics
from .session import SESSION, translate
from .symbols import SymbolIndex
from . import wiring

mcp = MCPServer(
    name="kicad",
    version="0.1.0",
    instructions=(
        "Tools for KiCad 10. Live PCB access needs KiCad running with the PCB "
        "editor open; schematic tools work on files and need neither. "
        "Call kicad_status first if anything is unexpected. "
        "All PCB dimensions are millimetres."
    ),
)

_INSTALL = None
_INDEX_CACHE: dict[str, SymbolIndex] = {}
_FP_INDEX_CACHE: dict[str, FootprintIndex] = {}


def install():
    global _INSTALL
    if _INSTALL is None:
        _INSTALL = find_install()
    return _INSTALL


def symbol_index(project_dir: str | None = None) -> SymbolIndex:
    key = str(project_dir or "")
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = SymbolIndex(install(), Path(project_dir) if project_dir else None)
    return _INDEX_CACHE[key]


def footprint_index(project_dir: str | None = None) -> FootprintIndex:
    key = str(project_dir or "")
    if key not in _FP_INDEX_CACHE:
        _FP_INDEX_CACHE[key] = FootprintIndex(install(), Path(project_dir) if project_dir else None)
    return _FP_INDEX_CACHE[key]


def _project_dir_of(path: str, project_dir: str | None) -> str:
    """The project a file belongs to, where its sym-lib-table and fp-lib-table live.

    The folder of the .kicad_pro beside it or a few folders above; a file in
    no project is its own, in its own folder.
    """
    if project_dir:
        return project_dir
    return str(hierarchy.find_project_dir(path) or Path(path).resolve().parent)


def tool_result(fn):
    """Convert exceptions into structured, actionable tool output."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> dict:
        try:
            return fn(*args, **kwargs)
        except KicadMcpError as exc:
            return exc.as_dict()
        except Exception as exc:  # noqa: BLE001 - boundary: nothing may escape
            translated = translate(exc)
            if isinstance(translated, KicadMcpError):
                return translated.as_dict()
            return {
                "error": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc()[-2000:],
            }

    return wrapper


def _cap(items: list, limit: int) -> dict:
    """Cap a result list and say so, rather than silently truncating.

    Boards routinely hold thousands of items; an uncapped dump would exhaust the
    context window and the model would never know it saw a partial view.
    """
    total = len(items)
    shown = items[:limit]
    out: dict[str, Any] = {"count": total, "shown": len(shown), "items": shown}
    if total > len(shown):
        out["truncated"] = True
        out["hint"] = f"{total - len(shown)} more; narrow the filter or raise limit."
    return out


def _match(text: str, pattern: str | None) -> bool:
    if not pattern:
        return True
    return fnmatch.fnmatch(str(text).lower(), pattern.lower())


# --- environment ---------------------------------------------------------


@mcp.tool()
@tool_result
def kicad_status() -> dict:
    """Report the KiCad environment: install, versions, live connection, and which capabilities are available. Call this first when anything behaves unexpectedly."""
    out: dict[str, Any] = {}
    try:
        inst = install()
        out["install"] = inst.to_dict()
        out["all_installs"] = [i.to_dict() for i in find_installs()]
    except KicadMcpError as exc:
        out["install_error"] = exc.as_dict()
        return out

    out["live"] = SESSION.status()
    try:
        import pcbnew  # noqa: PLC0415

        out["pcbnew"] = {"available": True, "build": pcbnew.GetBuildVersion()}
    except Exception as exc:  # noqa: BLE001
        out["pcbnew"] = {"available": False, "reason": str(exc)}

    out["capabilities"] = {
        "live_pcb": out["live"].get("board_open", False),
        "headless_pcb": out["pcbnew"]["available"],
        "schematic_files": True,
        "schematic_ipc": False,
    }
    out["notes"] = [
        "KiCad 10 has no schematic IPC API; schematic tools are file-based.",
        "Live PCB tools need the PCB editor open, not just KiCad running.",
    ]
    return out


# --- PCB -----------------------------------------------------------------
#
# Every read tool takes an optional `path`. Without it the tool asks the board
# open in KiCad (live, unsaved edits included); with it the tool reads that
# .kicad_pcb through pcbnew, so it works with KiCad closed.

_PATH_DOC = (
    " Pass `path` (a .kicad_pcb) to read the saved file instead of the live "
    "board; that works with KiCad closed."
)


def _with_path_doc(fn):
    fn.__doc__ = (fn.__doc__ or "") + _PATH_DOC
    return fn


@mcp.tool()
@tool_result
@_with_path_doc
def get_board_summary(path: str | None = None) -> dict:
    """Summarise a board: name, layer count, item counts, title block, and origin. A cheap orientation call before more specific queries."""
    if path:
        return pcb_file.summary(path)
    return pcb_live.summary(SESSION.board())


@mcp.tool()
@tool_result
@_with_path_doc
def list_footprints(reference: str | None = None, value: str | None = None,
                    limit: int = 200, path: str | None = None) -> dict:
    """List footprints on a board. Filter with glob patterns on reference (e.g. 'R*') or value (e.g. '10k'). Positions are millimetres."""
    rows = pcb_file.footprints(path) if path else pcb_live.footprints(SESSION.board())
    items = [r for r in rows if _match(r["reference"], reference) and _match(r["value"], value)]
    items.sort(key=lambda d: d["reference"])
    return _cap(items, limit)


@mcp.tool()
@tool_result
@_with_path_doc
def get_footprint(reference: str, path: str | None = None) -> dict:
    """Full detail for one footprint, including every pad with its net, position, and layer."""
    if path:
        found = pcb_file.footprint(path, reference)
    else:
        found = pcb_live.footprint(SESSION.board(), reference)
    if found is not None:
        return found
    return {
        "error": "NotFound",
        "message": f"No footprint with reference {reference!r} on the board.",
        "remedy": "Call list_footprints to see available references.",
    }


@mcp.tool()
@tool_result
@_with_path_doc
def list_nets(name: str | None = None, limit: int = 300, path: str | None = None) -> dict:
    """List nets on a board, optionally filtered by a glob pattern on the net name."""
    rows = pcb_file.nets(path) if path else pcb_live.nets(SESSION.board())
    items = [r for r in rows if _match(r["name"], name)]
    items.sort(key=lambda d: d["name"])
    return _cap(items, limit)


@mcp.tool()
@tool_result
@_with_path_doc
def get_layers(path: str | None = None) -> dict:
    """List the board's enabled, visible, and active layers with their names."""
    if path:
        return pcb_file.layers(path)
    return pcb_live.layers(SESSION.board())


@mcp.tool()
@tool_result
@_with_path_doc
def get_stackup(path: str | None = None) -> dict:
    """The board's physical stackup: layer order, materials, and thicknesses."""
    if path:
        return pcb_file.stackup(path)
    return pcb_live.stackup(SESSION.board())


@mcp.tool()
@tool_result
@_with_path_doc
def list_board_shapes(layer: str | None = None, shape_type: str | None = None,
                      include_footprints: bool = False, limit: int = 300,
                      path: str | None = None) -> dict:
    """List graphic shapes on a board: lines, arcs, circles, rectangles, polygons and beziers, with uuid, layer, stroke width and geometry in millimetres.

    Filter by `layer` (glob; short names like 'F.SilkS' work) and `shape_type`
    ('segment', 'arc', 'circle', 'rectangle', 'polygon', 'bezier'). Board
    outline and cutouts are layer 'Edge.Cuts'. Set `include_footprints` to also
    list footprint graphics -- slots, cutouts, courtyards -- each tagged with
    its footprint's reference.
    """
    if path:
        rows = pcb_file.shapes(path, include_footprints)
    else:
        rows = pcb_live.shapes(SESSION.board(), include_footprints)
    return _cap(pcb_common.filter_shapes(rows, layer, shape_type), limit)


@mcp.tool()
@tool_result
def get_selection() -> dict:
    """What the user currently has selected in the KiCad GUI. Use this to act on 'the thing I'm looking at'."""
    board = SESSION.board()
    items = []
    for item in board.get_selection():
        entry: dict[str, Any] = {"type": type(item).__name__}
        for attr in ("reference", "name", "number"):
            if hasattr(item, attr):
                entry[attr] = str(getattr(item, attr))
        if hasattr(item, "position"):
            entry["position"] = units.point_to_mm(item.position)
        items.append(entry)
    return _cap(items, 200)


@mcp.tool()
@tool_result
@_with_path_doc
def list_tracks_vias(net: str | None = None, limit: int = 300,
                     path: str | None = None) -> dict:
    """List tracks and vias on a board, optionally filtered by net name."""
    rows = pcb_file.tracks_vias(path) if path else pcb_live.tracks_vias(SESSION.board())
    return _cap([r for r in rows if not net or _match(r["net"], net)], limit)


@mcp.tool()
@tool_result
@_with_path_doc
def list_zones(limit: int = 100, path: str | None = None) -> dict:
    """List copper zones on a board with their nets and layers."""
    rows = pcb_file.zones(path) if path else pcb_live.zones(SESSION.board())
    return _cap(rows, limit)


# --- schematic (file-based) ---------------------------------------------


@mcp.tool()
@tool_result
def open_schematic(path: str) -> dict:
    """Open a .kicad_sch file and summarise it: title block, counts, child sheets, and the text of its notes. Works with KiCad closed - schematic access is file-based because KiCad 10 has no schematic IPC API."""
    return Schematic.load(path).summary()


@mcp.tool()
@tool_result
def list_schematic_symbols(path: str, reference: str | None = None,
                           value: str | None = None, limit: int = 300) -> dict:
    """List symbols placed in a .kicad_sch file, with reference, value, lib_id, position and footprint. Filter with glob patterns."""
    sch = Schematic.load(path)
    items = [
        s.to_dict() for s in sch.symbols()
        if _match(s.reference, reference) and _match(s.value, value)
    ]
    return _cap(items, limit)


@mcp.tool()
@tool_result
def find_project_files(directory: str) -> dict:
    """List KiCad project files in a directory: schematics, boards, and project files."""
    d = Path(directory)
    return {
        "directory": str(d),
        "schematics": find_project_schematics(d),
        "boards": sorted(str(p) for p in d.glob("*.kicad_pcb")),
        "projects": sorted(str(p) for p in d.glob("*.kicad_pro")),
    }


@mcp.tool()
@tool_result
def schematic_netlist(path: str, include_nodes: bool = True, limit: int = 400) -> dict:
    """Export and parse a schematic's netlist via kicad-cli: every component and every net with its connected pins. The most complete view of schematic connectivity available in KiCad 10."""
    result = kcli.export_netlist(install(), path)
    if not result.get("output_file"):
        return {"error": "NetlistFailed", "message": result.get("stderr") or result.get("stdout"),
                "remedy": "Check the schematic path and that the file is valid."}
    parsed = kcli.parse_kicadxml_netlist(result["output_file"])
    nets = parsed["nets"]
    if not include_nodes:
        nets = [{k: v for k, v in n.items() if k != "nodes"} for n in nets]
    return {
        "source": parsed["source"],
        "component_count": parsed["component_count"],
        "net_count": parsed["net_count"],
        "components": parsed["components"][:limit],
        "nets": nets[:limit],
    }


@mcp.tool()
@tool_result
def run_erc(path: str, severity_all: bool = False) -> dict:
    """Run Electrical Rules Check on a schematic via kicad-cli and return the report. A non-zero violation count is a result, not a failure."""
    return kcli.run_erc(install(), path, severity_all=severity_all)


@mcp.tool()
@tool_result
def run_drc(path: str, violation_type: str | None = None, severity: str | None = None,
            limit: int = 100) -> dict:
    """Run Design Rules Check on a .kicad_pcb file via kicad-cli.

    Returns counts over the whole report -- by type (e.g. invalid_outline,
    clearance) and by severity -- plus unconnected and schematic-parity counts,
    and a list of violations capped at `limit`. Narrow the list with
    `violation_type` (glob, e.g. 'invalid_outline' or 'silk_*') and `severity`
    ('error' or 'warning'). The full JSON stays in `report_file`.
    """
    return kcli.pcb_drc(install(), path, violation_type=violation_type,
                        severity=severity, limit=limit)


@mcp.tool()
@tool_result
def export_bom(path: str, output: str | None = None) -> dict:
    """Export a Bill of Materials from a schematic to CSV via kicad-cli."""
    return kcli.export_bom(install(), path, output)


@mcp.tool()
@tool_result
def export_schematic(path: str, fmt: str = "pdf", output: str | None = None) -> dict:
    """Plot a schematic to pdf, svg, dxf, ps, or hpgl via kicad-cli."""
    return kcli.export_plot(install(), path, fmt, output)


# --- symbol libraries ----------------------------------------------------


@mcp.tool()
@tool_result
def list_symbol_libraries(project_dir: str | None = None) -> dict:
    """List the symbol libraries available, resolving KiCad's global and project sym-lib-tables."""
    libs = symbol_index(project_dir).list_libraries()
    return {"count": len(libs), "libraries": libs}


@mcp.tool()
@tool_result
def search_symbols(query: str, library: str | None = None, limit: int = 40,
                   project_dir: str | None = None) -> dict:
    """Search symbol libraries by name substring, e.g. 'ESP32-S3' or 'LM358'. Returns lib_ids usable with get_symbol_pins and create_pinout_schematic."""
    hits = symbol_index(project_dir).search(query, limit=limit, library=library)
    return {"query": query, "count": len(hits), "results": hits}


@mcp.tool()
@tool_result
def get_symbol_pins(lib_id: str, project_dir: str | None = None) -> dict:
    """Every pin of a library symbol: number, name, electrical type, and position. Use this to learn a part's pinout before wiring it."""
    pins = symbol_index(project_dir).pins(lib_id)
    return {
        "lib_id": lib_id,
        "pin_count": len(pins),
        "pins": [p.to_dict() for p in pins],
    }


@mcp.tool()
@tool_result
def create_library_symbol(
    project_dir: str,
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
    """Draw a new symbol into a project's own symbol library, for a part no library has (a module, a custom header).

    `pins` lists {"number", "name", "type", "side"}: type is a KiCad electrical
    type, passive when left out (input, output, bidirectional, tri_state,
    power_in, power_out, open_collector, open_emitter, no_connect, free,
    unspecified); side is left, right, top or bottom. Each side's pins run in
    list order from the top, or from the left, 2.54 mm apart; with no side
    on any pin the first half go left and the rest right. The body is a
    rectangle sized to clear the pin names. `project_dir` is the project's
    folder or any file in it. `library` is a nickname: one in the project's
    sym-lib-table gets the symbol added; a new one becomes
    <project>/<library>.kicad_sym, registered in the project's sym-lib-table.
    Text already in either file is left as it was. Refused, writing nothing:
    a name the library has, the nickname of a global library (a project one
    would hide it), repeated pin numbers, and a folder in no KiCad project.
    Place it with add_symbol_to_schematic as lib_id "<library>:<name>".
    """
    result = create_symbol(
        install(), project_dir, library, name, pins, reference=reference, value=value,
        footprint=footprint, datasheet=datasheet, description=description, keywords=keywords,
    )
    result["warnings"] = []
    warning = footprint_warning(footprint_index(_project_dir_of(project_dir, None)), footprint)
    if warning:
        result["warnings"].append(warning)
    return result


# --- footprint libraries -------------------------------------------------


@mcp.tool()
@tool_result
def search_footprints(query: str, library: str | None = None, limit: int = 40,
                      project_dir: str | None = None) -> dict:
    """Search footprint libraries by name substring, e.g. 'SOIC-8' or 'HLK-PM'. Resolves KiCad's global and project fp-lib-tables, so project-local .pretty libraries are included. Returns Library:Name ids for a symbol's Footprint field."""
    hits = footprint_index(project_dir).search(query, limit=limit, library=library)
    return {"query": query, "count": len(hits), "results": hits}


@mcp.tool()
@tool_result
def get_library_footprint(lib_id: str, project_dir: str | None = None) -> dict:
    """Description, tags, attributes and pad numbers of a library footprint (not the open board -- that is get_footprint). Compare pads with get_symbol_pins before assigning it to a symbol."""
    return footprint_index(project_dir).info(lib_id)


# --- generation ----------------------------------------------------------


@mcp.tool()
@tool_result
def create_pinout_schematic(
    output: str,
    mcu_lib_id: str,
    assignments: dict[str, str],
    title: str = "",
    reference: str = "U1",
    footprint: str = "",
    connect_power: bool = True,
    global_nets: list[str] | None = None,
    project_dir: str | None = None,
) -> dict:
    """Create a .kicad_sch from a firmware pinout by importing a symbol from a KiCad library.

    `assignments` maps pin names or numbers to net names, e.g. {"IO4": "LED_STATUS", "GPIO_8": "SPI_MOSI"}.
    Matching ignores case, underscores and hyphens, so IO4/GPIO_4/io4 all work.
    Each assigned pin gets a wire and, by net: a power symbol for rails (GND, +3V3,
    any power-library name, or a lib_id like "power:+5V"); a global label for names
    in `global_nets` or already global in the project (these reach other sheets);
    otherwise a local label, which connects only within this sheet. Unassigned
    power pins get power symbols and PWR_FLAG automatically. If `output` is a
    sheet of a KiCad project, symbols are annotated under that project's
    hierarchy. The file is written directly and does not need KiCad running.
    """
    return generate_pinout_schematic(
        symbol_index(_project_dir_of(output, project_dir)), output, mcu_lib_id, assignments,
        title=title, mcu_reference=reference, mcu_footprint=footprint,
        connect_power=connect_power, global_nets=global_nets,
    )


# --- editing existing schematics -----------------------------------------


@mcp.tool()
@tool_result
def add_symbol_to_schematic(
    path: str,
    lib_id: str,
    assignments: dict[str, str],
    reference: str | None = None,
    value: str | None = None,
    footprint: str = "",
    x: float | None = None,
    y: float | None = None,
    global_nets: list[str] | None = None,
    project_dir: str | None = None,
) -> dict:
    """Place a symbol into an existing .kicad_sch and wire named pins to nets.

    Unlike create_pinout_schematic this edits a file that already has content
    instead of authoring a new one -- existing symbols, wires and labels are
    left untouched. `assignments` maps pin names or numbers to net names, same
    matching rules as create_pinout_schematic (case/underscore/hyphen-insensitive).
    How each pin joins its net, reported as `via`: a power symbol for rails
    (GND, +3V3, any power-library name, a lib_id like "power:+5V", or a power
    net already in the project); a global label for names in `global_nets` or
    already global in the project -- use these for signals that cross sheets;
    otherwise a local label, which connects only within this sheet.
    Unlike an MCU there is no automatic power-pin wiring: a connector's pins are
    not VDD/GND by convention, so nothing is guessed. Call mark_pins_unused
    separately for pins that should be no-connected rather than left floating.
    On a sheet of a KiCad project the symbol is annotated under the project's
    hierarchy, with a reference free across all sheets (one per use of a
    reused sheet, listed in `instances`).
    Without x/y the symbol goes in the first free spot on the page -- right of
    existing parts, wrapping to a new row, never over the title block; if the
    sheet is full it refuses rather than placing off-page. An empty footprint
    takes the library symbol's default. `warnings` flags an explicit x/y off
    the page, a footprint the project's fp-lib-table cannot resolve, a
    reference already used elsewhere, and a label that will not reach a
    same-named local label on another sheet.
    """
    index = symbol_index(_project_dir_of(path, project_dir))
    editor = SchematicEditor.load(path)
    at = (x, y) if x is not None and y is not None else None
    result = editor.place_symbol(
        index, lib_id, assignments, reference=reference, value=value, footprint=footprint, at=at,
        global_nets=global_nets,
    )
    editor.save()
    warning = footprint_warning(footprint_index(_project_dir_of(path, project_dir)), result["footprint"])
    if warning:
        result["warnings"].append(warning)
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def label_pins(
    path: str,
    reference: str,
    assignments: dict[str, str],
    global_nets: list[str] | None = None,
    project_dir: str | None = None,
) -> dict:
    """Wire named pins of an already-placed symbol to nets.

    add_symbol_to_schematic only wires pins at the moment it places a symbol;
    use this for symbols already on the sheet, placed earlier or by hand
    (rotated and mirrored symbols included). `assignments` maps pin names or
    numbers to net names, same matching and the same power-symbol / global-label
    / local-label choice (`via`, `global_nets`) as add_symbol_to_schematic. Two
    pins given the same net name are connected. A pin that already has a wire,
    label, no-connect or another pin on it is left alone and listed under
    already_connected, since a second label would short two nets together.
    """
    index = symbol_index(_project_dir_of(path, project_dir))
    editor = SchematicEditor.load(path)
    result = editor.label_pins(index, reference, assignments, global_nets=global_nets)
    editor.save()
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def set_symbol_fields(
    path: str,
    reference: str,
    fields: dict[str, str],
    project_dir: str | None = None,
) -> dict:
    """Set fields of an already-placed symbol: Value, Footprint, Datasheet, or any custom field (MPN, ...).

    Applies to every unit of a multi-unit part. Fields the symbol lacks are
    added hidden. Renaming via {"Reference": "J5"} updates KiCad's annotation
    record too and refuses a reference already in use on any sheet of the
    project. A Footprint that the
    project's fp-lib-table cannot resolve is written but reported in
    warnings, with the same footprint under a library that does resolve.
    """
    editor = SchematicEditor.load(path)
    result = editor.set_fields(
        reference, fields, footprints=footprint_index(_project_dir_of(path, project_dir)),
    )
    editor.save()
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def remove_symbols(
    path: str,
    references: list[str],
    dry_run: bool = False,
    project_dir: str | None = None,
) -> dict:
    """Delete placed symbols from a .kicad_sch, with what was there only to connect them.

    Every unit of each part goes; on a sheet used more than once it goes
    from every use (reported in warnings). Also removed, and listed under
    cleaned_up: wire runs that end in nothing once the part is gone, and
    labels, power symbols, PWR_FLAGs, no-connects and junctions no longer
    reaching any pin. Anything still wired to another part or a sheet pin
    stays. Kept and reported instead: wiring that joins two net names
    (removing it would split those nets) and hierarchical labels (the parent
    sheet's pin refers to them). An unknown reference removes nothing.
    `dry_run` reports the same without writing.
    """
    editor = SchematicEditor.load(path)
    result = editor.remove_symbols(symbol_index(_project_dir_of(path, project_dir)), references)
    if not dry_run:
        editor.save()
    result["written"] = not dry_run
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def disconnect_pins(
    path: str,
    reference: str,
    pin_numbers: list[str] | None = None,
    name_contains: str | None = None,
    dry_run: bool = False,
    project_dir: str | None = None,
) -> dict:
    """Take pins of an already-placed symbol off their nets; the symbol stays.

    Choose pins as for mark_pins_unused: exact pin_numbers, a case-insensitive
    name_contains, or both. Removed, listed under cleaned_up: the pin's
    no-connect flag, the wire run from it back to the first point where
    something else connects (another pin, a branch), and labels, power
    symbols, PWR_FLAGs and junctions that then reach no pin. Use it to free
    a pin for label_pins or mark_pins_unused, or to retire a net from a part
    that stays. A pin whose wiring carries the net on to other pins (a label
    on the pin with a wire beyond, two wires meeting on it, another part's
    pin) keeps it and is listed under still_connected, as is one of several
    stacked pins unless all are chosen. A pin number the part lacks changes
    nothing. `dry_run` reports without writing.
    """
    editor = SchematicEditor.load(path)
    result = editor.disconnect_pins(
        symbol_index(_project_dir_of(path, project_dir)), reference,
        pin_numbers=pin_numbers, name_contains=name_contains,
    )
    if not dry_run:
        editor.save()
    result["written"] = not dry_run
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def rename_labels(path: str, renames: dict[str, str], dry_run: bool = False) -> dict:
    """Rename nets on a .kicad_sch by renaming their labels, e.g. {"IN3": "IN1", "IN4": "IN2"}.

    Every label of each name is renamed -- local, global and hierarchical --
    all at once, so a numbered series can shift onto itself and two names
    can swap. Refused, writing nothing: a name with no label on the sheet,
    and any rename that would join two nets (onto a name another label on
    the sheet keeps, onto a power net, or two names onto one). Only this
    sheet changes: warnings say when a global label's old name stays on
    other sheets or its new one is already there, and when a hierarchical
    label leaves the parent's sheet pin on the old name. `dry_run` reports
    without writing.
    """
    editor = SchematicEditor.load(path)
    result = editor.rename_labels(renames)
    if not dry_run:
        editor.save()
    result["written"] = not dry_run
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def edit_notes(path: str, replacements: dict[str, str], dry_run: bool = False) -> dict:
    """Rewrite text in a sheet's notes -- free text and text boxes -- e.g. {"spare GPIO8, 9": "spare GPIO35-42"}.

    Each key is a fragment of one note (open_schematic lists them; line
    breaks are \\n), replaced by its value; the note keeps its uuid,
    position and look. Quote a whole note to replace all of it, or rewrite
    it to "" to remove it. All replacements apply to the text as it was, so
    two can swap. Refused, writing nothing: a fragment found in no note or
    more than once, and two overlapping in one note. A text box keeps its
    size, so warnings flag one whose text grew. Labels are not notes; nets
    are renamed with rename_labels. `dry_run` reports without writing.
    """
    editor = SchematicEditor.load(path)
    result = editor.edit_notes(replacements)
    if not dry_run:
        editor.save()
    result["written"] = not dry_run
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def move_symbol_fields(
    path: str,
    reference: str,
    fields: dict[str, dict],
    unit: int | None = None,
) -> dict:
    """Move fields of an already-placed symbol, e.g. {"Reference": {"x": 120.65, "y": 60.96}, "Value": {"dy": 2.54}}.

    Per field: absolute `x`/`y` in sheet mm (either alone keeps the other),
    or a `dx`/`dy` offset from where it is, and optionally `orientation`,
    "horizontal" or "vertical" -- how the text reads on the sheet, whatever
    the symbol's rotation. Works on any field, hidden ones included. On a
    multi-unit part an absolute position needs `unit`; offsets apply to
    every unit unless one is given. The symbol is no longer marked
    autoplaced, so KiCad keeps the new spots. A spot off the drawing area
    or on the title block is written but warned. Nothing moves if any
    request is invalid.
    """
    editor = SchematicEditor.load(path)
    result = editor.move_fields(reference, fields, unit=unit)
    editor.save()
    result["schematic"] = str(editor.path)
    return result


@mcp.tool()
@tool_result
def route_nets(
    path: str,
    nets: list[str] | None = None,
    max_length: float | None = None,
    dry_run: bool = False,
    project_dir: str | None = None,
) -> dict:
    """Replace net labels with drawn wires -- the LAST step, after placement, labelling and ERC are done.

    Routing runs locally in this server; pass only the file. Every signal net
    connected purely by local labels is wired pin to pin around the parts
    and keeps one label with its name. Power nets, global/hierarchical labels
    and hand-drawn wiring are left alone. `nets` limits it to named nets;
    `max_length` (mm) keeps longer routes as labels. The result is checked
    with kicad-cli before writing -- identical netlist, no new ERC
    violations -- and discarded otherwise, leaving the file untouched.
    `dry_run` runs the same checks without writing.
    """
    return wiring.route_and_verify(
        install(), symbol_index(_project_dir_of(path, project_dir)), path,
        nets=nets, max_length=max_length, dry_run=dry_run,
    )


@mcp.tool()
@tool_result
def mark_pins_unused(
    path: str,
    reference: str,
    pin_numbers: list[str] | None = None,
    name_contains: str | None = None,
    annotate: str | None = None,
    project_dir: str | None = None,
) -> dict:
    """No-connect a set of an already-placed symbol's pins.

    Select pins by exact pin_numbers, by a case-insensitive substring of the
    pin's own name (name_contains="SD_" catches every SD-bus pin at once), or
    both. Pass annotate with the pin's real function (e.g. "/RES") to also
    place that text next to each marked pin -- useful when the symbol's pin
    name is generic or doesn't say what the net actually is.
    """
    index = symbol_index(_project_dir_of(path, project_dir))
    editor = SchematicEditor.load(path)
    marked = editor.mark_pins_unused(
        index, reference, pin_numbers=pin_numbers, name_contains=name_contains, annotate=annotate,
    )
    editor.save()
    return {"schematic": str(editor.path), "reference": reference, "marked_count": len(marked), "marked": marked}


@mcp.tool()
@tool_result
def rewire_power_symbol(path: str, old_lib_id: str, new_lib_id: str, project_dir: str | None = None) -> dict:
    """Repoint every instance of one power symbol to another, e.g. power:VDD to power:+3V3.

    Only succeeds when both symbols place their pin at the identical position --
    checked first, since otherwise the existing wire would no longer meet the
    new symbol's pin and the change would silently break the connection.
    """
    index = symbol_index(_project_dir_of(path, project_dir))
    editor = SchematicEditor.load(path)
    changed = editor.swap_power_symbol(index, old_lib_id, new_lib_id)
    editor.save()
    return {"schematic": str(editor.path), "old_lib_id": old_lib_id, "new_lib_id": new_lib_id, "changed_count": changed}


@mcp.tool()
@tool_result
def add_decoupling_capacitors(
    path: str,
    rails: list[str],
    value: str = "100nF",
    ground: str = "power:GND",
    project_dir: str | None = None,
) -> dict:
    """Add one decoupling capacitor per named power rail, each tied to ground.

    `rails` are power-symbol lib_ids such as ["power:+3V3", "power:+5V"]. Each
    capacitor is placed clear of existing content and wired rail-to-ground with
    its own wire -- not dependent on label matching.
    """
    index = symbol_index(_project_dir_of(path, project_dir))
    return _add_decoupling_capacitors(index, path, rails, value=value, ground=ground)


# --- escape hatch --------------------------------------------------------


@mcp.tool()
@tool_result
def run_kicad_script(code: str, timeout: int = 30) -> dict:
    """Execute Python with the KiCad API pre-imported. Use this for anything the curated tools do not cover.

    Available names: `kicad` (live KiCad client), `board` (open board, or None),
    `connection_error` (why they are None, else None), `kipy`, `pcbnew`, and
    `sexpr` for KiCad file parsing.
    Print output is captured; assign to `result` to return a value.

    Group live edits into one undo step with `with board_commit("message"):` --
    it pushes on success and drops the commit if the block raises. In kipy
    0.7.1, KIID takes keywords only: `KIID(value="...")`, or reuse `item.id`.
    With KiCad closed, use `pcbnew.LoadBoard(path)` for file-based PCB work.

    This runs arbitrary local code with your privileges. Do not import kipy.schematic - it is broken in kipy 0.7.1 and KiCad 10 has no schematic API anyway.
    """
    from .sandbox import run_script

    return run_script(code, timeout=timeout)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
