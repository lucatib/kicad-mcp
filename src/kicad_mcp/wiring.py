"""Replace label-connected signal nets with drawn wires.

This is the last step of a session: placement, labelling and ERC are done, so
the sheet is finished and correct. Everything here is arranged so that it can
only leave the sheet as good as it was:

- Which pins form a net comes from kicad-cli's netlist, not our geometry.
- Only nets connected purely by local labels are touched -- each label either
  on a pin or at the end of one stub wire from a pin. Power, global and
  hierarchical nets, and anything with hand-drawn wiring, are left alone.
- A net is rewired all or nothing, and keeps one label so its name (and
  every netlist reference to it) survives.

`route_and_verify` then proves it with kicad-cli before writing a byte.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import cli as kcli
from . import sexpr
from .editor import SchematicEditor, _bounds, _local_points, _text_box
from .errors import ToolInputError
from .generator import GRID, PlacedSymbol
from .router import Cell, Dir, Grid, route_tree, segments
from .symbols import SymbolIndex

Point = tuple[float, float]


@dataclass
class _Net:
    name: str
    pins: dict[tuple[float, float], tuple[Point, Dir]] = field(default_factory=dict)
    labels: list = field(default_factory=list)
    stubs: list = field(default_factory=list)


def route_nets(
    editor: SchematicEditor,
    index: SymbolIndex,
    netlist: dict,
    nets: list[str] | None = None,
    max_length: float | None = None,
    bend_cost: int = 5,
) -> dict:
    """Rewire label-connected signal nets in `editor.tree`; see the module doc.

    `netlist` is `cli.parse_kicadxml_netlist` output for the file as loaded.
    """
    result: dict[str, list] = {"routed": [], "unrouted": [], "skipped": []}
    by_text: dict[str, list] = {}
    for label in sexpr.children(editor.tree, "label"):
        by_text.setdefault(str(label[1]), []).append(label)
    wanted = set(nets) if nets else None
    for name in sorted((wanted or set()) - set(by_text)):
        result["skipped"].append({"net": name, "reason": "no local label with this name on the sheet"})

    nodes_by_net = {n["name"]: n["nodes"] for n in netlist["nets"]}
    pins = _pin_table(editor, index)

    candidates = []
    for text in sorted(by_text):
        if wanted is not None and text not in wanted:
            continue
        nodes = nodes_by_net.get(f"/{text}")
        if nodes is None:
            result["skipped"].append({
                "net": text,
                "reason": "power, global or hierarchical net -- kept as symbols/labels",
            })
            continue
        net, reason = _collect(editor, text, by_text[text], nodes, pins)
        if reason:
            result["skipped"].append({"net": text, "reason": reason})
            continue
        candidates.append(net)

    # Fewest pins first: short, simple nets claim the direct paths, and the
    # big ones -- which can detour -- route around them.
    for net in sorted(candidates, key=lambda n: (len(n.pins), n.name)):
        grid = _grid(editor, index, net)
        cells = {_cell(p): d for p, d in net.pins.values()}
        edges = route_tree(grid, list(cells), cells, bend_cost)
        if edges is None:
            result["unrouted"].append({
                "net": net.name, "reason": "no clear path between its pins; labels kept",
            })
            continue
        runs, junctions = segments(edges)
        length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in runs) * GRID
        if max_length is not None and length > max_length:
            result["unrouted"].append({
                "net": net.name,
                "reason": f"route would be {length:.1f} mm, over max_length {max_length:g}; labels kept",
            })
            continue
        spot = _label_spot(runs, junctions, grid, set(cells))
        if spot is None:
            result["unrouted"].append({
                "net": net.name, "reason": "no free spot on the new wire for its name label; labels kept",
            })
            continue
        _apply(editor, net, runs, junctions, spot)
        result["routed"].append({
            "net": net.name, "pins": len(cells), "wires": len(runs),
            "junctions": len(junctions), "length_mm": round(length, 2),
        })
    return result


def route_and_verify(
    install,
    index: SymbolIndex,
    path: str | Path,
    nets: list[str] | None = None,
    max_length: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Route, then prove the result with kicad-cli before touching the file.

    Both versions are checked the same way, from copies in one scratch folder:
    the netlist must be identical and ERC must report no more violations.
    Only then -- and never with `dry_run` -- is the original replaced.
    """
    path = Path(path)
    editor = SchematicEditor.load(path)
    original = path.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="kicad-mcp-route-") as tmp:
        before = Path(tmp) / "before.kicad_sch"
        after = Path(tmp) / "after.kicad_sch"
        shutil.copyfile(path, before)
        net_before = _netlist(install, before)
        result = route_nets(editor, index, net_before, nets=nets, max_length=max_length)
        result["schematic"] = str(path)
        result["written"] = False
        if not result["routed"]:
            result["message"] = "Nothing was routed; the file is unchanged."
            return result

        after.write_text(sexpr.dumps(editor.tree), encoding="utf-8")
        net_after = _netlist(install, after)
        erc = {"before": _erc_count(install, before), "after": _erc_count(install, after)}
        result["erc_violations"] = erc

    changed = _connectivity_diff(net_before, net_after)
    if changed:
        result["verification"] = "failed: connectivity would change"
        result["changed_nets"] = changed[:20]
        result["message"] = "Routing was discarded; the file is unchanged."
        return result
    if erc["after"] > erc["before"]:
        result["verification"] = "failed: ERC violations would increase"
        result["message"] = "Routing was discarded; the file is unchanged."
        return result

    result["verification"] = "passed: identical netlist, no new ERC violations"
    if dry_run:
        result["message"] = "Dry run: verified but not written."
        return result
    if path.read_text(encoding="utf-8") != original:
        raise ToolInputError(
            f"{path.name} changed on disk while routing; nothing was written.",
            remedy="Save or close it in KiCad, then run route_nets again.",
        )
    editor.save()
    result["written"] = True
    return result


# --- which nets, which pins ----------------------------------------------


def _pin_table(editor: SchematicEditor, index: SymbolIndex) -> dict[str, dict[str, tuple[Point, Dir]]]:
    """reference -> pin number -> (canvas connection point, outward direction)."""
    table: dict[str, dict[str, tuple[Point, Dir]]] = {}
    for ref in editor.references():
        try:
            units = editor.placements(index, ref)
        except ToolInputError:
            continue  # library gone: its pins can't be routed to, nor its nets
        for sym in units:
            for pin in sym.pins:
                dx, dy = sym.pin_outward(pin)
                table.setdefault(ref, {})[pin.number] = (sym.pin_point(pin), (round(dx), round(dy)))
    return table


def _collect(editor, text, labels, nodes, pins) -> tuple[_Net | None, str | None]:
    net = _Net(text, labels=list(labels))
    for node in nodes:
        info = pins.get(node["reference"], {}).get(node["pin"])
        if info is None:
            return None, f"pin {node['reference']}:{node['pin']} is not drawn on this sheet"
        if _cell(info[0]) is None:
            return None, f"pin {node['reference']}:{node['pin']} is off the 1.27 mm grid"
        net.pins.setdefault(_key(info[0]), info)
    if len(net.pins) < 2:
        return None, "fewer than two pins: nothing to wire"

    ends: dict[tuple[float, float], list] = {}
    for wire in sexpr.children(editor.tree, "wire"):
        for end in _wire_ends(wire):
            ends.setdefault(_key(end), []).append(wire)
    hand = "connected by hand-drawn wiring as well as labels; left as drawn"

    reached: set[tuple[float, float]] = set()
    for label in labels:
        at = _key(_at(label))
        if at in net.pins:
            reached.add(at)
            continue
        wires = ends.get(at, [])
        if len(wires) != 1:
            return None, hand
        far = [e for e in _wire_ends(wires[0]) if _key(e) != at]
        if len(far) != 1 or _key(far[0]) not in net.pins:
            return None, hand
        net.stubs.append(wires[0])
        reached.add(_key(far[0]))

    stub_ids = {id(w) for w in net.stubs}
    junctions = {_key(_at(j)) for j in sexpr.children(editor.tree, "junction")}
    for key in net.pins:
        if key not in reached or key in junctions:
            return None, hand
        if any(id(w) not in stub_ids for w in ends.get(key, [])):
            return None, hand
    return net, None


# --- the obstacle map ----------------------------------------------------


def _grid(editor: SchematicEditor, index: SymbolIndex, net: _Net) -> Grid:
    """Everything on the sheet except `net`'s own labels and stubs, as router cells."""
    w, h = editor.page_size()
    grid = Grid(int(w / GRID) + 1, int(h / GRID) + 1)
    own = {_cell(p) for p, _ in net.pins.values()}
    removed = {id(n) for n in net.labels + net.stubs}

    area = editor.drawing_area()
    title = editor.title_block()
    for cx in range(grid.cols):
        for cy in range(grid.rows):
            x, y = cx * GRID, cy * GRID
            if not (area[0] <= x <= area[2] and area[1] <= y <= area[3]) or (
                title[0] <= x <= title[2] and title[1] <= y <= title[3]
            ):
                grid.block((cx, cy))

    embedded = {
        str(n[1]): n
        for n in sexpr.children(sexpr.child(editor.tree, "lib_symbols") or [], "symbol")
        if len(n) > 1
    }
    for symbol in sexpr.children(editor.tree, "symbol"):
        lib_id = str(sexpr.value(symbol, "lib_id", ""))
        at = sexpr.values(symbol, "at")
        if not lib_id or not at:
            continue
        unit = int(sexpr.value(symbol, "unit", 1) or 1)
        mirror = sexpr.value(symbol, "mirror")
        placed = PlacedSymbol("", "", lib_id, x=float(at[0]), y=float(at[1]),
                              angle=float(at[2]) if len(at) > 2 else 0.0, unit=unit, uuid="",
                              mirror=str(mirror) if mirror else None)
        definition = embedded.get(lib_id)
        if definition is not None:
            body = [placed.to_canvas(px, py) for px, py in _local_points(definition, unit, include_pins=False)]
            _fill(grid, _bounds(body), grid.block)
        try:
            pins = [p for p in index.pins(lib_id) if p.unit in (0, unit)]
        except ToolInputError:
            pins = []
        for pin in pins:
            point = _cell(placed.pin_point(pin))
            if point is None:
                continue
            grid.forbid(point)
            # The pin's own line, from its end toward the body: never run along it.
            dx, dy = placed.pin_outward(pin)
            inward = (-round(dx), -round(dy))
            for k in range(1, max(1, round(pin.length / GRID)) + 1):
                grid.block((point[0] + inward[0] * k, point[1] + inward[1] * k))
        for prop in sexpr.children(symbol, "property"):
            p_at = sexpr.values(prop, "at")
            if p_at and "(hide yes)" not in sexpr.dumps(prop):
                box = _text_box(float(p_at[0]) - len(str(prop[2])) * 0.635, float(p_at[1]), 0.0, str(prop[2]))
                _fill(grid, box, grid.soften)

    for kind in ("label", "global_label", "hierarchical_label"):
        for label in sexpr.children(editor.tree, kind):
            if id(label) in removed:
                continue
            at = sexpr.values(label, "at")
            cell = _cell(_at(label))
            if cell is not None:
                grid.forbid(cell)
            _fill(grid, _text_box(float(at[0]), float(at[1]), float(at[2]) if len(at) > 2 else 0.0,
                                  str(label[1])), grid.soften)
    for text in sexpr.children(editor.tree, "text"):
        at = sexpr.values(text, "at")
        if at:
            _fill(grid, _text_box(float(at[0]), float(at[1]), 0.0, str(text[1])), grid.soften)
    for kind in ("junction", "no_connect"):
        for node in sexpr.children(editor.tree, kind):
            cell = _cell(_at(node))
            if cell is not None:
                grid.forbid(cell)
    for wire in sexpr.children(editor.tree, "wire"):
        if id(wire) in removed:
            continue
        a, b = (_cell(e) for e in _wire_ends(wire)[:2])
        if a is None or b is None:
            _fill(grid, _bounds(_wire_ends(wire)), grid.block)
        else:
            grid.add_wire(a, b)
    for sheet in sexpr.children(editor.tree, "sheet"):
        at, size = sexpr.values(sheet, "at"), sexpr.values(sheet, "size")
        if at and size:
            x, y = float(at[0]), float(at[1])
            _fill(grid, (x, y, x + float(size[0]), y + float(size[1])), grid.block)

    # Our own pins are where the route starts and ends, whatever sits there.
    for cell in own:
        grid.blocked.discard(cell)
        grid.forbidden.discard(cell)
    return grid


def _fill(grid: Grid, box: tuple[float, float, float, float], mark) -> None:
    for cx in range(int(box[0] // GRID), int(-(-box[2] // GRID)) + 1):
        for cy in range(int(box[1] // GRID), int(-(-box[3] // GRID)) + 1):
            x, y = cx * GRID, cy * GRID
            if box[0] <= x <= box[2] and box[1] <= y <= box[3] and grid.inside((cx, cy)):
                mark((cx, cy))


# --- writing the result --------------------------------------------------


def _apply(editor: SchematicEditor, net: _Net, runs, junctions, spot: tuple[Cell, float]) -> None:
    removed = {id(n) for n in net.labels + net.stubs}
    editor.tree[:] = [n for n in editor.tree if id(n) not in removed]
    builder = editor._builder()
    for a, b in runs:
        builder.add_wire(_mm(a), _mm(b))
    for j in junctions:
        builder.add_junction(_mm(j))
    anchor, angle = spot
    builder.add_label(net.name, _mm(anchor), angle, ["left", "bottom"])
    editor._merge(builder)


def _label_spot(runs, junctions, grid: Grid, pins: set[Cell]) -> tuple[Cell, float] | None:
    """Where the one kept label goes: on this net's wire, touching nothing else.

    A label joins every wire under its anchor, so a spot where the new route
    crosses another net's wire would short the two -- the reason this is not
    simply "the middle of the longest run". Pins and junctions are avoided
    too, for legibility. Horizontal runs first, longest first, so the name
    reads along the wire.
    """
    taken = pins | set(junctions)
    ordered = sorted(
        runs,
        key=lambda r: (r[0][1] != r[1][1], -(abs(r[0][0] - r[1][0]) + abs(r[0][1] - r[1][1]))),
    )
    for a, b in ordered:
        step = ((b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1]))
        span = abs(a[0] - b[0]) + abs(a[1] - b[1])
        for k in range(1, span):  # interior cells only: ends are corners, pins or joins
            c = (a[0] + step[0] * k, a[1] + step[1] * k)
            if c in taken or c in grid.horizontal or c in grid.vertical or c in grid.forbidden:
                continue
            return c, (0.0 if a[1] == b[1] else 90.0)
    return None


# --- small helpers -------------------------------------------------------


def _netlist(install, path: Path) -> dict:
    result = kcli.export_netlist(install, path, output=path.with_suffix(".net.xml"))
    if not result.get("output_file"):
        raise ToolInputError(
            f"kicad-cli could not export a netlist for {path.name}: {result.get('stderr') or result.get('stdout')}",
            remedy="Run schematic_netlist on the file to see what KiCad reports.",
        )
    return kcli.parse_kicadxml_netlist(result["output_file"])


def _erc_count(install, path: Path) -> int:
    return kcli.erc_violation_count(install, path)


def _connectivity(netlist: dict) -> dict[str, frozenset]:
    return {
        n["name"]: frozenset((x["reference"], x["pin"]) for x in n["nodes"])
        for n in netlist["nets"]
    }


def _connectivity_diff(before: dict, after: dict) -> list[str]:
    a, b = _connectivity(before), _connectivity(after)
    return sorted(name for name in set(a) | set(b) if a.get(name) != b.get(name))


def _cell(p: Point) -> Cell | None:
    cx, cy = p[0] / GRID, p[1] / GRID
    rx, ry = round(cx), round(cy)
    if abs(cx - rx) > 1e-3 or abs(cy - ry) > 1e-3:
        return None
    return (rx, ry)


def _mm(c: Cell) -> Point:
    return (round(c[0] * GRID, 4), round(c[1] * GRID, 4))


def _key(p: Point) -> tuple[float, float]:
    return (round(p[0], 2), round(p[1], 2))


def _at(node) -> Point:
    at = sexpr.values(node, "at")
    return (float(at[0]), float(at[1]))


def _wire_ends(wire) -> list[Point]:
    pts = sexpr.child(wire, "pts")
    return [(float(xy[1]), float(xy[2])) for xy in sexpr.children(pts or [], "xy")]
