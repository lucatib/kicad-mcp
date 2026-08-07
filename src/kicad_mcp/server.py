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
from . import units
from .discovery import find_install, find_installs
from .errors import KicadMcpError, NoEditorOpenError
from .generator import generate_pinout_schematic
from .schematic import Schematic, find_project_schematics
from .session import SESSION, translate
from .symbols import SymbolIndex

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


# --- live PCB ------------------------------------------------------------


@mcp.tool()
@tool_result
def get_board_summary() -> dict:
    """Summarise the board open in KiCad: name, layer count, item counts, title block, and origin. A cheap orientation call before more specific queries."""
    board = SESSION.board()
    counts = {
        "footprints": len(board.get_footprints()),
        "tracks": len(board.get_tracks()),
        "vias": len(board.get_vias()),
        "zones": len(board.get_zones()),
        "pads": len(board.get_pads()),
        "shapes": len(board.get_shapes()),
        "text": len(board.get_text()),
        "nets": len(board.get_nets()),
    }
    out: dict[str, Any] = {
        "name": getattr(board, "name", None),
        "copper_layers": board.get_copper_layer_count(),
        "counts": counts,
    }
    try:
        tb = board.get_title_block_info()
        out["title_block"] = {
            "title": tb.title, "date": tb.date, "revision": tb.revision,
            "company": tb.company,
        }
    except Exception:  # noqa: BLE001 - optional metadata
        pass
    try:
        out["origin"] = units.point_to_mm(board.get_origin())
    except Exception:  # noqa: BLE001
        pass
    return out


@mcp.tool()
@tool_result
def list_footprints(reference: str | None = None, value: str | None = None,
                    limit: int = 200) -> dict:
    """List footprints on the open board. Filter with glob patterns on reference (e.g. 'R*') or value (e.g. '10k'). Positions are millimetres."""
    board = SESSION.board()
    items = []
    for fp in board.get_footprints():
        ref = fp.reference_field.text.value if hasattr(fp, "reference_field") else ""
        val = fp.value_field.text.value if hasattr(fp, "value_field") else ""
        if not _match(ref, reference) or not _match(val, value):
            continue
        items.append({
            "reference": ref,
            "value": val,
            "library_id": str(getattr(fp, "definition", None) and fp.definition.id or ""),
            "position": units.point_to_mm(fp.position),
            "rotation": units.angle_to_deg(getattr(fp, "orientation", None)),
            "layer": board.get_layer_name(fp.layer) if hasattr(fp, "layer") else None,
        })
    items.sort(key=lambda d: d["reference"])
    return _cap(items, limit)


@mcp.tool()
@tool_result
def get_footprint(reference: str) -> dict:
    """Full detail for one footprint on the open board, including every pad with its net, position, and layer."""
    board = SESSION.board()
    for fp in board.get_footprints():
        ref = fp.reference_field.text.value if hasattr(fp, "reference_field") else ""
        if ref.lower() != reference.lower():
            continue
        pads = []
        for pad in fp.definition.pads if hasattr(fp, "definition") else []:
            pads.append({
                "number": getattr(pad, "number", ""),
                "net": getattr(getattr(pad, "net", None), "name", ""),
                "position": units.point_to_mm(getattr(pad, "position", None)),
            })
        return {
            "reference": ref,
            "value": fp.value_field.text.value if hasattr(fp, "value_field") else "",
            "position": units.point_to_mm(fp.position),
            "rotation": units.angle_to_deg(getattr(fp, "orientation", None)),
            "layer": board.get_layer_name(fp.layer) if hasattr(fp, "layer") else None,
            "pad_count": len(pads),
            "pads": pads[:200],
        }
    return {
        "error": "NotFound",
        "message": f"No footprint with reference {reference!r} on the open board.",
        "remedy": "Call list_footprints to see available references.",
    }


@mcp.tool()
@tool_result
def list_nets(name: str | None = None, limit: int = 300) -> dict:
    """List nets on the open board, optionally filtered by a glob pattern on the net name."""
    board = SESSION.board()
    items = [
        {"name": n.name, "code": getattr(n, "code", None)}
        for n in board.get_nets()
        if _match(n.name, name)
    ]
    items.sort(key=lambda d: d["name"])
    return _cap(items, limit)


@mcp.tool()
@tool_result
def get_layers() -> dict:
    """List the board's enabled, visible, and active layers with their names."""
    board = SESSION.board()
    enabled = list(board.get_enabled_layers())
    visible = set(board.get_visible_layers())
    active = board.get_active_layer()
    return {
        "copper_layer_count": board.get_copper_layer_count(),
        "active_layer": board.get_layer_name(active),
        "layers": [
            {
                "name": board.get_layer_name(layer),
                "visible": layer in visible,
                "active": layer == active,
            }
            for layer in enabled
        ],
    }


@mcp.tool()
@tool_result
def get_stackup() -> dict:
    """The board's physical stackup: layer order, materials, and thicknesses."""
    board = SESSION.board()
    stackup = board.get_stackup()
    layers = []
    for layer in getattr(stackup, "layers", []):
        layers.append({
            "name": getattr(layer, "user_name", "") or board.get_layer_name(layer.layer),
            "type": str(getattr(layer, "type", "")),
            "material": getattr(layer, "material_name", ""),
            "thickness_mm": units.nm_to_mm(getattr(layer, "thickness", None)),
            "dielectric": getattr(layer, "dielectric", None),
            "enabled": getattr(layer, "enabled", None),
        })
    return {"layer_count": len(layers), "layers": layers}


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
def list_tracks_vias(net: str | None = None, limit: int = 300) -> dict:
    """List tracks and vias on the open board, optionally filtered by net name."""
    board = SESSION.board()
    items: list[dict] = []
    for track in board.get_tracks():
        net_name = getattr(getattr(track, "net", None), "name", "")
        if net and not _match(net_name, net):
            continue
        items.append({
            "kind": type(track).__name__,
            "net": net_name,
            "start": units.point_to_mm(getattr(track, "start", None)),
            "end": units.point_to_mm(getattr(track, "end", None)),
            "width_mm": units.nm_to_mm(getattr(track, "width", None)),
        })
    for via in board.get_vias():
        net_name = getattr(getattr(via, "net", None), "name", "")
        if net and not _match(net_name, net):
            continue
        items.append({
            "kind": "Via",
            "net": net_name,
            "position": units.point_to_mm(getattr(via, "position", None)),
        })
    return _cap(items, limit)


@mcp.tool()
@tool_result
def list_zones(limit: int = 100) -> dict:
    """List copper zones on the open board with their nets and layers."""
    board = SESSION.board()
    items = []
    for zone in board.get_zones():
        items.append({
            "name": getattr(zone, "name", ""),
            "net": getattr(getattr(zone, "net", None), "name", ""),
            "filled": getattr(zone, "filled", None),
        })
    return _cap(items, limit)


# --- schematic (file-based) ---------------------------------------------


@mcp.tool()
@tool_result
def open_schematic(path: str) -> dict:
    """Open a .kicad_sch file and summarise it: title block, counts, and child sheets. Works with KiCad closed - schematic access is file-based because KiCad 10 has no schematic IPC API."""
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
def run_drc(path: str) -> dict:
    """Run Design Rules Check on a .kicad_pcb file via kicad-cli and return the report."""
    return kcli.pcb_drc(install(), path)


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
    project_dir: str | None = None,
) -> dict:
    """Create a .kicad_sch from a firmware pinout by importing a symbol from a KiCad library.

    `assignments` maps pin names or numbers to net names, e.g. {"IO4": "LED_STATUS", "GPIO_8": "SPI_MOSI"}.
    Matching ignores case, underscores and hyphens, so IO4/GPIO_4/io4 all work.
    Assigned pins get a wire and a net label; power pins get power symbols and PWR_FLAG automatically.
    The file is written directly and does not need KiCad running.
    """
    return generate_pinout_schematic(
        symbol_index(project_dir), output, mcu_lib_id, assignments,
        title=title, mcu_reference=reference, mcu_footprint=footprint,
        connect_power=connect_power,
    )


# --- escape hatch --------------------------------------------------------


@mcp.tool()
@tool_result
def run_kicad_script(code: str, timeout: int = 30) -> dict:
    """Execute Python with the KiCad API pre-imported. Use this for anything the curated tools do not cover.

    Available names: `kicad` (live KiCad client), `board` (open board, or None),
    `kipy`, `pcbnew`, and `sexpr` for KiCad file parsing.
    Print output is captured; assign to `result` to return a value.

    This runs arbitrary local code with your privileges. Do not import kipy.schematic - it is broken in kipy 0.7.1 and KiCad 10 has no schematic API anyway.
    """
    from .sandbox import run_script

    return run_script(code, timeout=timeout)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
