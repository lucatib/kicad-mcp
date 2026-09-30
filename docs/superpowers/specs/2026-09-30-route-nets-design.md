# route_nets — replace net labels with drawn wires

**Date:** 2026-09-30
**Status:** Approved in conversation; awaiting spec review

## 1. Purpose

The schematic tools connect pins with net labels: `create_pinout_schematic`,
`add_symbol_to_schematic` and `label_pins` all hang a label off each pin. That
is exact, but a sheet made only of labels is harder to read than one with drawn
wires. `route_nets` converts label-connected nets into real wires.

It is the **last step** of a session: called once placement, labelling and ERC
are done. It works on a finished, ERC-clean sheet, so the overriding
requirement is that it can never make that sheet worse.

## 2. Success criteria

1. After `route_nets`, `kicad-cli`'s netlist is identical to before: the same
   nets, the same names, the same pins on each.
2. The ERC violation count does not increase.
3. Any net that cannot meet (1) and (2) is left exactly as it was.
4. On a sheet with room around its parts, the wires are straight or single-bend
   where the geometry allows, and pass around part bodies, not through them.

## 3. Behaviour

`route_nets(path, nets=None, max_length=None, dry_run=False, project_dir=None)`

### 3.1 Which nets are candidates

A net is a group of local labels (`label`) sharing a name. It is routed only if
all of the following hold; otherwise it is skipped and reported with the reason:

- **In scope:** `nets` is omitted, or it names this net.
- **Signal net:** none of its pins is also reached by a power symbol, a
  `global_label` or a `hierarchical_label`. Power nets stay as power symbols,
  following the KiCad convention; cross-sheet nets must keep their labels.
- **Label-only wiring:** every label sits either directly on a pin's connection
  point or at the far end of a single wire segment whose other end is a pin of
  the same net. A net with any other wiring — hand-drawn wires, junctions,
  chains of segments — is left alone, since there is no way to tell which parts
  of it the user meant.
- **Two or more pins.** A single-pin net has nothing to route.

The pins of each net come from `kicad-cli`'s netlist export, not from our own
geometry, so what is routed is what KiCad actually connected.

### 3.2 Routing

For each candidate net, in order of fewest pins first:

1. Remove its labels and the label stub wires identified in 3.1.
2. Route a tree connecting all its pins (see 4). Each pin leaves its symbol
   in the pin's outward direction before turning.
3. Add a junction wherever three or more wire ends meet at a point that is not
   a pin.
4. Put one label back: on the tree's longest horizontal segment, or failing
   that its longest segment, so the net keeps its name (and the netlist its
   `/NAME`).

If any pin cannot be reached, or the tree's total length exceeds `max_length`,
the net is restored to its original labels and stubs and reported as unrouted.
A net is never left half-wired. Each routed net becomes an obstacle for the nets
routed after it.

### 3.3 Obstacles

| Obstacle | Rule |
|---|---|
| Part bodies (graphics and rectangles, inflated 1.27 mm) | Never crossed |
| Pins, labels, wire ends and junctions of other nets | Never touched |
| Existing wires of other nets | Crossed only at right angles, away from their ends; never run along |
| Wires already routed for this net | May be joined (that is how the tree grows) |
| Drawing area edge and title block | Never left or entered |

The pin, wire-end and junction rules exist because KiCad connects any pin or
wire end lying on a wire. A route that touches one silently merges two nets.

### 3.4 Verification and writing

The routed document is written to a temporary copy and checked with
`kicad-cli`:

- netlist of the copy == netlist of the original (net names, and each net's set
  of `(ref, pin)`)
- ERC violation count of the copy <= that of the original

Only if both hold is the original file replaced. If either fails, the file is
not touched and the result says which check failed and on which nets. With
`dry_run=True` the checks still run, but nothing is written in either case.

### 3.5 Result

```json
{
  "schematic": "...",
  "written": true,
  "routed": [{"net": "LED", "pins": 2, "wires": 3, "junctions": 0, "length_mm": 41.9}],
  "unrouted": [{"net": "CC_SCK", "reason": "no path from U2 pin 5 around U1"}],
  "skipped": [{"net": "GND", "reason": "power net"}],
  "erc_violations": {"before": 3, "after": 3}
}
```

## 4. Router

`router.py` knows nothing about KiCad. Its input is a grid description; its
output is lists of grid points.

- **Grid:** 1.27 mm, which is KiCad's connection grid; every pin of a stock
  symbol sits on it. The grid covers the drawing area only.
- **Cells:** each is free, blocked (part body, page), or forbidden-to-touch
  (foreign connection point). Existing foreign wires mark their cells as
  crossable only perpendicular to the wire's direction.
- **Search:** A* over (cell, direction). Cost is 1 per step plus a bend penalty
  (default 5 steps), so a clear path comes out as an L or a straight line
  rather than a staircase. The heuristic is Manhattan distance.
- **Multi-pin trees:** start from the net's first pin; repeatedly route the
  nearest unconnected pin to any cell already on the tree (multi-target A*),
  then add that path to the tree.
- **Output:** the path is compressed to its corner points, which become wire
  segments.

## 5. Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `router.py` | Grid, A*, tree building, path compression | nothing |
| `SchematicEditor.route_nets` | Find candidate nets, build obstacles, remove and restore labels, write segments and junctions | router, netlist from `cli` |
| `cli` netlist and ERC helpers | Compare connectivity and violation counts | kicad-cli |
| `server.route_nets` | MCP tool: temp copy, verify, write or refuse | the above |

## 6. Testing

- **router.py (no KiCad):** straight path on an empty grid; one bend on an L;
  detour around a blocked rectangle; never enters a forbidden cell; crosses a
  foreign wire only perpendicularly; three-pin tree reuses the trunk; returns
  None when walled in.
- **Editor (with KiCad):** a two-pin label net becomes wires, and the netlist is
  unchanged; a four-pin net, likewise; a power net is skipped; a net with
  hand-drawn wiring is skipped; a walled-in net stays labelled and is reported;
  `max_length` leaves a long net labelled; a pin sitting next to another net's
  pin is never merged with it.
- **Tool:** a forced netlist mismatch (routing monkeypatched to cross a foreign
  pin) leaves the file byte-identical and reports the failed check; `dry_run`
  never writes.
- **Visual:** render a routed test sheet with `kicad-cli sch export pdf` and
  inspect it.

## 7. Out of scope

- Routing power nets, or replacing power symbols with wires.
- Buses, hierarchical sheets, and nets spanning sheets.
- Moving parts to make routing easier. Placement is done by the time this runs.
- Minimising wire crossings between nets beyond what the order in 3.2 gives.
