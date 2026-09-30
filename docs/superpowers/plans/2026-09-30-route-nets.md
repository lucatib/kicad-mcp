# route_nets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An MCP tool, run last, that replaces label-connected signal nets with drawn wires and never makes the sheet worse.

**Architecture:** A pure grid router (`router.py`) does A* over (cell, direction) with a bend penalty and grows multi-pin trees. `wiring.py` turns a schematic plus its kicad-cli netlist into router input, and applies successful routes back to the document. The MCP tool routes on an in-memory copy, verifies the result with kicad-cli (identical netlist, no more ERC violations), and only then writes.

**Tech Stack:** Python 3.11 (KiCad's bundled interpreter), stdlib only; kicad-cli 10.0.6 as oracle; pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-route-nets-design.md`

## Global Constraints

- Grid 1.27 mm; every coordinate written is snapped to it.
- Candidate nets: netlist name is exactly `/` + label text (local labels on the root sheet). Power, global and hierarchical nets are skipped.
- A net is routed all-or-nothing; one label is put back on each routed net.
- File is written only if netlist(after) == netlist(before) and ERC count(after) <= ERC count(before), and never with `dry_run=True`.
- No new dependencies. Windows only.

## Review Focus

1. Pins 2.54 mm apart on an MCU edge, on different nets: a route must never touch the neighbour's connection point. (Task 2 test: adjacent-pin net pair stays separate.)
2. Labels placed directly on pin ends with no stub wire, as a previous session did by hand: must be recognised as label-only wiring. (Task 2 test.)
3. Stacked pins sharing one point (several GND pins): deduplicated to one routing point, never routed "to themselves". (Task 2: point dedupe in `net_points`.)
4. Rotated and mirrored symbols: a pin must be left in its real outward direction. (Task 2 test with a 90-degree, mirrored connector.)
5. A net whose pin also has a hand-drawn wire: skipped, not rerouted. (Task 2 test.)

---

### Task 1: Grid router

**Files:** Create `src/kicad_mcp/router.py`; Test `tests/test_router.py` (no KiCad).

**Produces:**
- `Grid(cols: int, rows: int)` with `block(cell)`, `forbid(cell)`, `soften(cell)`, `add_wire(a: Cell, b: Cell)` (foreign straight wire: interior cells crossable only perpendicularly, ends forbidden).
- `route_tree(grid, pins: list[Cell], exits: dict[Cell, Dir], bend_cost=5) -> set[Edge] | None` where `Cell = tuple[int, int]`, `Dir = tuple[int, int]`, `Edge = frozenset[Cell]` of two adjacent cells.
- `segments(edges) -> tuple[list[tuple[Cell, Cell]], list[Cell]]`: maximal straight segments and junction cells (degree >= 3).

Tests: straight line on an empty grid; one bend for an L; detour around a blocked block; never enters a forbidden cell; crosses a foreign wire only perpendicularly and never turns on it; a 3-pin tree joins the trunk and yields one junction; walled-in pin returns None; the first step leaves each pin in its exit direction.

### Task 2: Net extraction and application

**Files:** Create `src/kicad_mcp/wiring.py`; Test `tests/test_wiring.py` (KiCad).

**Consumes:** Task 1; `SchematicEditor.placements`, `editor.drawing_area()`, `editor.title_block()`, `_local_points`; `cli.export_netlist`, `cli.parse_kicadxml_netlist`.

**Produces:** `route_nets(editor, index, netlist: dict, nets: list[str] | None, max_length: float | None) -> dict` mutating `editor.tree`, returning `{"routed": [...], "unrouted": [...], "skipped": [...]}` as in spec 3.5.

Tests (each compares kicad-cli netlists before/after): 2-pin net becomes wires plus one label and the netlist is unchanged; 4-pin net likewise, with a junction; power net skipped; hand-drawn-wire net skipped; labels directly on pins accepted; adjacent-pin nets stay separate; rotated and mirrored connector routed; walled-in net unrouted and untouched; `max_length` leaves a long net labelled.

### Task 3: Verified MCP tool

**Files:** Modify `src/kicad_mcp/wiring.py` (add `route_and_verify`), `src/kicad_mcp/cli.py` (add `erc_violation_count`), `src/kicad_mcp/server.py` (tool), `README.md`; Test `tests/test_wiring.py`.

**Produces:** `route_and_verify(install, index, path, nets, max_length, dry_run) -> dict` and the `route_nets` tool.

Tests: happy path writes and reports `written: true` with equal ERC counts; `dry_run` never changes the file bytes; a monkeypatched router that shorts two nets leaves the file byte-identical and reports the failed check; nothing routable leaves the file untouched.

### Task 4: Visual check and docs

Render a routed sheet with `kicad-cli sch export pdf`, inspect it, and update the README tool list.
