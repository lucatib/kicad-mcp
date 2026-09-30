"""Orthogonal wire routing on a grid, knowing nothing about KiCad.

Cells are `(col, row)` on the schematic's 1.27 mm connection grid, row
growing downward like the canvas. The rules exist because of how KiCad joins
things: any pin, label or wire *end* lying on a wire becomes part of its net.
So a route may cross another net's wire only straight through, at a right
angle, and may never touch another net's connection point at all. Anything
subtler -- a bend or a stop on someone else's wire -- would silently merge
two nets.

Search is A* over (cell, heading) with a penalty per bend, so an open path
comes out as a straight line or a single L rather than a staircase. A net
with more pins grows as a tree: each further pin is routed to the nearest
cell already on it.
"""

from __future__ import annotations

import heapq
from itertools import count

Cell = tuple[int, int]
Dir = tuple[int, int]
Edge = frozenset  # of two orthogonally adjacent cells

DIRS: tuple[Dir, ...] = ((1, 0), (-1, 0), (0, 1), (0, -1))
SOFT_COST = 8  # stepping onto a soft cell (field text): allowed, discouraged


class Grid:
    """What each cell allows. Everything not recorded is free."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        self.blocked: set[Cell] = set()     # part bodies, title block: never entered
        self.forbidden: set[Cell] = set()   # other nets' connection points: never touched
        self.soft: set[Cell] = set()        # text: entered at a cost
        self.horizontal: set[Cell] = set()  # interior of a foreign horizontal wire
        self.vertical: set[Cell] = set()    # interior of a foreign vertical wire

    def inside(self, c: Cell) -> bool:
        return 0 <= c[0] < self.cols and 0 <= c[1] < self.rows

    def block(self, c: Cell) -> None:
        self.blocked.add(c)

    def forbid(self, c: Cell) -> None:
        self.forbidden.add(c)

    def soften(self, c: Cell) -> None:
        self.soft.add(c)

    def add_wire(self, a: Cell, b: Cell) -> None:
        """A straight foreign wire: ends forbidden, interior crossable at right angles."""
        self.forbid(a)
        self.forbid(b)
        if a[1] == b[1]:
            lo, hi = sorted((a[0], b[0]))
            cells, mine, other = [(x, a[1]) for x in range(lo + 1, hi)], self.horizontal, self.vertical
        elif a[0] == b[0]:
            lo, hi = sorted((a[1], b[1]))
            cells, mine, other = [(a[0], y) for y in range(lo + 1, hi)], self.vertical, self.horizontal
        else:  # diagonal wire: no safe crossing rule, so treat its box as solid
            for x in range(min(a[0], b[0]), max(a[0], b[0]) + 1):
                for y in range(min(a[1], b[1]), max(a[1], b[1]) + 1):
                    self.block((x, y))
            return
        for c in cells:
            if c in other:  # two foreign wires already cross here
                self.forbid(c)
            mine.add(c)


def _on_wire(grid: Grid, c: Cell) -> bool:
    return c in grid.horizontal or c in grid.vertical


def _route(
    grid: Grid,
    start: Cell,
    exit_dir: Dir,
    goals: set[Cell],
    arrive: dict[Cell, Dir],
    avoid: set[Cell],
    bend_cost: int,
) -> list[Cell] | None:
    """Cheapest path from `start` (first step along `exit_dir`) to any goal.

    `arrive[g]`, when present, is the heading the path must have on entering
    goal g -- a pin must be entered from outside, moving toward its body.
    """
    if not goals:
        return None
    gx0 = min(g[0] for g in goals)
    gx1 = max(g[0] for g in goals)
    gy0 = min(g[1] for g in goals)
    gy1 = max(g[1] for g in goals)

    def h(c: Cell) -> int:  # Manhattan distance to the goals' bounding box
        dx = gx0 - c[0] if c[0] < gx0 else c[0] - gx1 if c[0] > gx1 else 0
        dy = gy0 - c[1] if c[1] < gy0 else c[1] - gy1 if c[1] > gy1 else 0
        return dx + dy

    tie = count()
    first = (start[0] + exit_dir[0], start[1] + exit_dir[1])
    best: dict[tuple[Cell, Dir], int] = {}
    parent: dict[tuple[Cell, Dir], tuple[Cell, Dir] | None] = {}
    heap: list = []

    def push(state, g, prev):
        if g < best.get(state, 1 << 30):
            best[state] = g
            parent[state] = prev
            heapq.heappush(heap, (g + h(state[0]), next(tie), g, state))

    if _enterable(grid, first, exit_dir, goals, arrive, avoid):
        push((first, exit_dir), 1 + (SOFT_COST if first in grid.soft else 0), None)

    while heap:
        _, _, g, state = heapq.heappop(heap)
        if g > best.get(state, 1 << 30):
            continue
        cell, heading = state
        if cell in goals:
            path = [cell]
            s = state
            while parent[s] is not None:
                s = parent[s]
                path.append(s[0])
            path.append(start)
            return path[::-1]
        # On another net's wire: straight through only, no turning, no stopping.
        turns = (heading,) if _on_wire(grid, cell) else DIRS
        for d in turns:
            if d == (-heading[0], -heading[1]):
                continue
            nxt = (cell[0] + d[0], cell[1] + d[1])
            if not _enterable(grid, nxt, d, goals, arrive, avoid):
                continue
            step = 1 + (bend_cost if d != heading else 0) + (SOFT_COST if nxt in grid.soft else 0)
            push((nxt, d), g + step, state)
    return None


def _enterable(grid: Grid, c: Cell, d: Dir, goals: set[Cell], arrive: dict[Cell, Dir],
               avoid: set[Cell]) -> bool:
    if not grid.inside(c) or c in grid.blocked:
        return False
    if c in goals:
        need = arrive.get(c)
        return need is None or need == d
    if c in grid.forbidden or c in avoid:
        return False
    if c in grid.horizontal and d[1] == 0:  # running along a horizontal wire
        return False
    if c in grid.vertical and d[0] == 0:
        return False
    return True


def _edges(path: list[Cell]) -> set[Edge]:
    return {frozenset((a, b)) for a, b in zip(path, path[1:])}


def route_tree(grid: Grid, pins: list[Cell], exits: dict[Cell, Dir], bend_cost: int = 5) -> set[Edge] | None:
    """Wire every pin together, or None if any pin cannot be reached.

    Each pin is left along `exits[pin]` (its outward direction) and entered
    against it. Every pin is a leaf: routes join the wiring, never pass
    through another pin.
    """
    pins = list(dict.fromkeys(pins))
    if len(pins) < 2:
        return set()

    root = pins[0]
    waiting = pins[1:]
    tree: set[Edge] = set()
    tree_cells: set[Cell] = set()
    pin_set = set(pins)

    def nearest(cands: list[Cell], targets: set[Cell]) -> Cell:
        return min(cands, key=lambda p: min(abs(p[0] - t[0]) + abs(p[1] - t[1]) for t in targets))

    while waiting:
        if not tree:
            goals = {root}
            arrive = {root: (-exits[root][0], -exits[root][1])}
        else:
            # Join the wiring, not a pin: a pin already has its one wire.
            goals = tree_cells - pin_set
            arrive = {}
        pin = nearest(waiting, goals or {root})
        # Every other pin, connected or not: running through one would
        # hug its symbol's edge and fuse into it.
        avoid = pin_set - {pin} - goals
        path = _route(grid, pin, exits[pin], goals, arrive, avoid, bend_cost)
        if path is None:
            return None
        tree |= _edges(path)
        tree_cells.update(path)
        waiting.remove(pin)
    return tree


def segments(edges: set[Edge]) -> tuple[list[tuple[Cell, Cell]], list[Cell]]:
    """Maximal straight runs of a tree, and the cells where three or more meet."""
    adj: dict[Cell, set[Cell]] = {}
    for e in edges:
        a, b = tuple(e)
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    def is_break(c: Cell) -> bool:
        n = adj[c]
        if len(n) != 2:
            return True
        a, b = n
        return not (a[0] == b[0] == c[0] or a[1] == b[1] == c[1])

    runs: list[tuple[Cell, Cell]] = []
    seen: set[Edge] = set()
    for start in sorted(c for c in adj if is_break(c)):
        for nxt in sorted(adj[start]):
            if frozenset((start, nxt)) in seen:
                continue
            d = (nxt[0] - start[0], nxt[1] - start[1])
            prev, cur = start, nxt
            seen.add(frozenset((prev, cur)))
            while not is_break(cur):
                step = (cur[0] + d[0], cur[1] + d[1])
                seen.add(frozenset((cur, step)))
                prev, cur = cur, step
            runs.append(tuple(sorted((start, cur))))
    junctions = sorted(c for c, n in adj.items() if len(n) >= 3)
    return sorted(runs), junctions
