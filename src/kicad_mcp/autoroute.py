"""A grid router for board tracks: nets in, segments and vias out.

KiCad has no autorouter and its Python API has no access to the interactive
one, so a board synced from a schematic stays a ratsnest until someone draws
every track. This routes it. It knows nothing of pcbnew: `board_edit` hands it
plain shapes in millimetres and writes back what it returns, so the search can
be tested on its own.

The board is a lattice of nodes `grid` apart on each copper layer. For the net
being routed, a node is blocked when a track centred on it would come closer
to another net's copper than the larger of the two nets' clearances. A net is
grown as a tree: from everything already connected, the cheapest path to any
pad not yet reached (Dijkstra with a distance bound, eight directions), until
no pad is left. A through-hole pad of the net changes layer for free; a via
does it at a cost, where a via fits. Routed nets become obstacles for the ones
after. When connections fail, the nets that failed go first on the next pass
and the best pass is kept.

What it does not do: push tracks aside, route differential pairs, or follow a
zone. It is a maze router: complete enough for through-hole boards and sparse
SMD ones, with every track at the width and clearance of its net class.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

SQRT2 = math.sqrt(2.0)
#: (dx, dy, length in grid steps)
#: Added, in grid steps, where a track changes direction.
BEND_COST = 0.5
STEPS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
         (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2))
#: A pad counts as on the lattice when its centre is this close to a node.
ON_NODE = 0.01


@dataclass(frozen=True)
class Rule:
    """What a net's class asks of its tracks, in mm."""
    width: float = 0.25
    clearance: float = 0.2
    via_diameter: float = 0.8
    via_drill: float = 0.4


@dataclass(frozen=True)
class Shape:
    """Copper on some layers: a circle, a capsule (a segment with round ends)
    or a rectangle turned by `angle` degrees.

    circle: (x, y, radius); capsule: (x1, y1, x2, y2, radius);
    rect: (x, y, half_width, half_height, angle).
    """
    kind: str
    geometry: tuple
    layers: tuple[int, ...]
    net: str | None = None

    def distance(self, px: float, py: float) -> float:
        """From a point to the copper's edge; negative inside it."""
        g = self.geometry
        if self.kind == "circle":
            return math.hypot(px - g[0], py - g[1]) - g[2]
        if self.kind == "capsule":
            return _to_segment(px, py, g[0], g[1], g[2], g[3]) - g[4]
        a = math.radians(g[4])
        dx, dy = px - g[0], py - g[1]
        u = abs(dx * math.cos(a) + dy * math.sin(a)) - g[2]
        v = abs(-dx * math.sin(a) + dy * math.cos(a)) - g[3]
        if u <= 0 and v <= 0:
            return max(u, v)
        return math.hypot(max(u, 0.0), max(v, 0.0))

    def bounds(self) -> tuple[float, float, float, float]:
        g = self.geometry
        if self.kind == "circle":
            return g[0] - g[2], g[1] - g[2], g[0] + g[2], g[1] + g[2]
        if self.kind == "capsule":
            return (min(g[0], g[2]) - g[4], min(g[1], g[3]) - g[4],
                    max(g[0], g[2]) + g[4], max(g[1], g[3]) + g[4])
        r = math.hypot(g[2], g[3])
        return g[0] - r, g[1] - r, g[0] + r, g[1] + r


@dataclass(frozen=True)
class Pad:
    """A pad to connect: where a track ends, and the copper around it."""
    net: str
    name: str            # "R1.2", for the report
    x: float
    y: float
    shape: Shape


@dataclass
class Result:
    #: (net, layer, x1, y1, x2, y2)
    tracks: list[tuple] = field(default_factory=list)
    #: (net, x, y)
    vias: list[tuple] = field(default_factory=list)
    #: (net, pad name) for each pad left off its net
    failed: list[tuple] = field(default_factory=list)
    passes: int = 0


def _to_segment(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    span = dx * dx + dy * dy
    t = 0.0 if span == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / span))
    return math.hypot(px - x1 - t * dx, py - y1 - t * dy)


class _Lattice:
    def __init__(self, bounds, grid: float, layers: int) -> None:
        self.grid = grid
        self.layers = layers
        self.x0 = math.ceil(bounds[0] / grid - 1e-9) * grid
        self.y0 = math.ceil(bounds[1] / grid - 1e-9) * grid
        self.nx = int(math.floor((bounds[2] - self.x0) / grid + 1e-9)) + 1
        self.ny = int(math.floor((bounds[3] - self.y0) / grid + 1e-9)) + 1
        self.bounds = bounds

    def xy(self, ix: int, iy: int) -> tuple[float, float]:
        return self.x0 + ix * self.grid, self.y0 + iy * self.grid

    def nearest(self, x: float, y: float) -> tuple[int, int]:
        return round((x - self.x0) / self.grid), round((y - self.y0) / self.grid)

    def inside(self, ix: int, iy: int) -> bool:
        return 0 <= ix < self.nx and 0 <= iy < self.ny

    def block(self, mask: bytearray, shape: Shape, reach: float) -> None:
        """Mark the nodes closer than `reach` to `shape`."""
        bx0, by0, bx1, by1 = shape.bounds()
        g = self.grid
        ix0 = max(0, int(math.floor((bx0 - reach - self.x0) / g)))
        ix1 = min(self.nx - 1, int(math.ceil((bx1 + reach - self.x0) / g)))
        iy0 = max(0, int(math.floor((by0 - reach - self.y0) / g)))
        iy1 = min(self.ny - 1, int(math.ceil((by1 + reach - self.y0) / g)))
        distance = shape.distance
        for iy in range(iy0, iy1 + 1):
            y = self.y0 + iy * g
            row = iy * self.nx
            for ix in range(ix0, ix1 + 1):
                if not mask[row + ix] and distance(self.x0 + ix * g, y) < reach:
                    mask[row + ix] = 1

    def block_edge(self, mask: bytearray, reach: float) -> None:
        """Mark the nodes closer than `reach` to the board's edge."""
        bx0, by0, bx1, by1 = self.bounds
        for iy in range(self.ny):
            y = self.y0 + iy * self.grid
            row = iy * self.nx
            if y - by0 < reach or by1 - y < reach:
                mask[row:row + self.nx] = b"\x01" * self.nx
                continue
            for ix in range(self.nx):
                x = self.x0 + ix * self.grid
                if x - bx0 < reach or bx1 - x < reach:
                    mask[row + ix] = 1


def _pieces(shape: Shape, longest: float) -> list[Shape]:
    """A long capsule as short ones, so a diagonal track is not marked through
    the whole box around it."""
    if shape.kind != "capsule":
        return [shape]
    x1, y1, x2, y2, r = shape.geometry
    n = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / longest))
    return [Shape("capsule", (x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n,
                              x1 + (x2 - x1) * (i + 1) / n, y1 + (y2 - y1) * (i + 1) / n, r),
                  shape.layers, shape.net) for i in range(n)]


class _Router:
    def __init__(self, bounds, pads, obstacles, rules, default_rule, grid, layers,
                 edge_clearance, via_cost, layer_cost, allow_vias) -> None:
        self.lattice = _Lattice(bounds, grid, layers)
        self.pads = pads
        self.fixed = list(obstacles) + [p.shape for p in pads]
        self.rules = rules
        self.default_rule = default_rule
        self.edge_clearance = edge_clearance
        self.via_cost = via_cost
        self.layer_cost = layer_cost
        self.allow_vias = allow_vias and layers > 1

    def rule(self, net: str | None) -> Rule:
        return self.rules.get(net, self.default_rule) if net else self.default_rule

    def run(self, order: list[str]) -> Result:
        result = Result()
        copper: list[Shape] = []        # what this pass has laid down
        for net in order:
            pads = [p for p in self.pads if p.net == net]
            if len(pads) < 2:
                continue
            self._route_net(net, pads, copper, result)
        return result

    # --- one net -------------------------------------------------------

    def _masks(self, net: str, copper: list[Shape]):
        """Per layer, where a track of `net` may not be centred; and where a via may not."""
        lat, rule = self.lattice, self.rule(net)
        size = lat.nx * lat.ny
        tracks = [bytearray(size) for _ in range(lat.layers)]
        vias = bytearray(size) if self.allow_vias else None
        # Between two nodes a track dips closer to a corner than at either
        # node; asking a little more at the nodes covers the dip exactly.
        sag = (lat.grid * SQRT2 / 2) ** 2
        for mask in tracks:
            lat.block_edge(mask, self.edge_clearance + rule.width / 2)
        if vias is not None:
            lat.block_edge(vias, self.edge_clearance + rule.via_diameter / 2)
        for shape in self.fixed + copper:
            if shape.net == net:
                continue
            gap = max(rule.clearance, self.rule(shape.net).clearance)
            need = gap + rule.width / 2
            reach = math.sqrt(need * need + sag)
            for piece in _pieces(shape, 8 * lat.grid):
                for layer in piece.layers:
                    if layer < lat.layers:
                        lat.block(tracks[layer], piece, reach)
                if vias is not None:
                    lat.block(vias, piece, gap + rule.via_diameter / 2)
        return tracks, vias

    def _clear(self, net: str, layer: int, x1, y1, x2, y2, copper: list[Shape]) -> bool:
        """Whether a track of `net` from one point to another keeps its distance."""
        rule = self.rule(net)
        n = max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / (self.lattice.grid / 4)))
        points = [(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n) for i in range(n + 1)]
        for shape in self.fixed + copper:
            if shape.net == net or layer not in shape.layers:
                continue
            need = max(rule.clearance, self.rule(shape.net).clearance) + rule.width / 2
            bx0, by0, bx1, by1 = shape.bounds()
            if (max(x1, x2) < bx0 - need or min(x1, x2) > bx1 + need
                    or max(y1, y2) < by0 - need or min(y1, y2) > by1 + need):
                continue
            if any(shape.distance(x, y) < need for x, y in points):
                return False
        return True

    def _entries(self, net: str, pad: Pad, tracks, copper) -> dict[tuple[int, int], tuple | None]:
        """The nodes a track can leave `pad` from: {(layer, index): stub or None}.

        A pad on a node is entered there. One between nodes is entered at a
        node beside it, with a short stub from the pad's centre.
        """
        lat = self.lattice
        ix, iy = lat.nearest(pad.x, pad.y)
        out: dict[tuple[int, int], tuple | None] = {}
        layers = [layer for layer in pad.shape.layers if layer < lat.layers]
        if lat.inside(ix, iy):
            nx, ny = lat.xy(ix, iy)
            if math.hypot(nx - pad.x, ny - pad.y) <= ON_NODE:
                return {(layer, iy * lat.nx + ix): None for layer in layers}
        for jx in range(ix - 1, ix + 2):
            for jy in range(iy - 1, iy + 2):
                if not lat.inside(jx, jy):
                    continue
                nx, ny = lat.xy(jx, jy)
                if math.hypot(nx - pad.x, ny - pad.y) > lat.grid * 1.01:
                    continue
                index = jy * lat.nx + jx
                for layer in layers:
                    if not tracks[layer][index] and self._clear(net, layer, pad.x, pad.y, nx, ny, copper):
                        out[(layer, index)] = (pad.x, pad.y, nx, ny)
        return out

    def _route_net(self, net: str, pads: list[Pad], copper: list[Shape], result: Result) -> None:
        lat, rule = self.lattice, self.rule(net)
        tracks, vias = self._masks(net, copper)
        entries = {p.name: self._entries(net, p, tracks, copper) for p in pads}
        # A through-hole pad on a node joins its layers there at no cost.
        free_change = set()
        for p in pads:
            nodes = entries[p.name]
            if len(nodes) > 1 and all(stub is None for stub in nodes.values()):
                free_change.update(index for _, index in nodes)

        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)
        first = min(pads, key=lambda p: math.hypot(p.x - cx, p.y - cy))
        tree: dict[tuple[int, int], tuple | None] = dict(entries[first.name])
        waiting = [p for p in pads if p is not first]
        drawn: set[tuple] = set()
        new: list[Shape] = []

        def lay(layer, x1, y1, x2, y2):
            if (x1, y1) == (x2, y2):
                return
            result.tracks.append((net, layer, round(x1, 6), round(y1, 6), round(x2, 6), round(y2, 6)))
            new.append(Shape("capsule", (x1, y1, x2, y2, rule.width / 2), (layer,), net))

        def stub(node):
            line = tree.get(node)
            if line is not None and (node, line) not in drawn:
                drawn.add((node, line))
                lay(node[0], *line)

        while waiting:
            targets: dict[tuple[int, int], Pad] = {}
            for p in waiting:
                for node in entries[p.name]:
                    targets.setdefault(node, p)
            path = self._search(tree, targets, waiting, tracks, vias, free_change) if targets else None
            if path is None:
                # Whatever is left cannot reach the tree; nor, mostly, each other.
                result.failed.extend((net, p.name) for p in waiting)
                break
            reached = targets[path[-1]]
            waiting.remove(reached)
            stub(path[0])
            for a, b in self._runs(path):
                if a[0] != b[0]:
                    if a[1] not in free_change:
                        x, y = lat.xy(a[1] % lat.nx, a[1] // lat.nx)
                        result.vias.append((net, round(x, 6), round(y, 6)))
                        new.append(Shape("circle", (x, y, rule.via_diameter / 2),
                                         tuple(range(lat.layers)), net))
                    continue
                lay(a[0], *lat.xy(a[1] % lat.nx, a[1] // lat.nx), *lat.xy(b[1] % lat.nx, b[1] // lat.nx))
            for node in path:
                tree.setdefault(node, None)
            for node, line in entries[reached.name].items():
                tree[node] = line
            stub(path[-1])
        copper.extend(new)

    def _search(self, tree, targets, waiting, tracks, vias, free_change):
        """The cheapest path from the tree to a target node, as [(layer, index)]."""
        lat = self.lattice
        nx, grid = lat.nx, lat.grid
        goals = [((p.x - lat.x0) / grid, (p.y - lat.y0) / grid) for p in waiting]

        def bound(index: int) -> float:
            ix, iy = index % nx, index // nx
            best = math.inf
            for gx, gy in goals:
                dx, dy = abs(gx - ix), abs(gy - iy)
                d = max(dx, dy) + (SQRT2 - 1) * min(dx, dy)
                if d < best:
                    best = d
            # A pad between nodes is entered one node away from its centre.
            return max(0.0, best - SQRT2) * min(self.layer_cost)

        cost = {node: 0.0 for node in tree}
        came: dict[tuple[int, int], tuple[int, int]] = {}
        heading: dict[tuple[int, int], tuple[int, int]] = {}
        heap = [(bound(node[1]), 0.0, node) for node in tree]
        heapq.heapify(heap)
        done = set()
        while heap:
            _, c, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if node in targets:
                path = [node]
                while path[-1] in came:
                    path.append(came[path[-1]])
                return path[::-1]
            layer, index = node
            ix, iy = index % nx, index // nx
            mask = tracks[layer]
            weight = self.layer_cost[layer]
            ahead = heading.get(node)
            for dx, dy, length in STEPS:
                jx, jy = ix + dx, iy + dy
                if not (0 <= jx < nx and 0 <= jy < lat.ny):
                    continue
                j = jy * nx + jx
                other = (layer, j)
                if other in done or (mask[j] and other not in targets):
                    continue
                nc = c + length * weight
                # Not part of the search state, so a nudge rather than a rule:
                # enough to keep equal-length paths from wandering in steps.
                if ahead is not None and ahead != (dx, dy):
                    nc += BEND_COST
                if nc < cost.get(other, math.inf):
                    cost[other] = nc
                    came[other] = node
                    heading[other] = (dx, dy)
                    heapq.heappush(heap, (nc + bound(j), nc, other))
            free = index in free_change
            if free or (vias is not None and not vias[index]):
                for up in range(lat.layers):
                    other = (up, index)
                    if up == layer or other in done or (tracks[up][index] and not free):
                        continue
                    nc = c + (0.0 if free else self.via_cost / grid)
                    if nc < cost.get(other, math.inf):
                        cost[other] = nc
                        came[other] = node
                        heapq.heappush(heap, (nc + bound(index), nc, other))
        return None

    def _runs(self, path):
        """`path` as straight runs: [(first node, last node)], layer changes on their own."""
        nx = self.lattice.nx
        runs = []
        start = prev = path[0]
        heading = None
        for node in path[1:]:
            if node[0] != prev[0]:
                if start != prev:
                    runs.append((start, prev))
                runs.append((prev, node))
                start, heading = node, None
            else:
                step = (node[1] % nx - prev[1] % nx, node[1] // nx - prev[1] // nx)
                if heading is not None and step != heading:
                    runs.append((start, prev))
                    start = prev
                heading = step
            prev = node
        if start != prev:
            runs.append((start, prev))
        return runs


def _span(pads: list[Pad]) -> float:
    xs, ys = [p.x for p in pads], [p.y for p in pads]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def route(
    bounds: tuple[float, float, float, float],
    pads: list[Pad],
    obstacles: list[Shape] = (),
    rules: dict[str, Rule] | None = None,
    default_rule: Rule = Rule(),
    nets: list[str] | None = None,
    grid: float = 0.635,
    layers: int = 2,
    edge_clearance: float = 0.5,
    via_cost: float = 10.0,
    layer_cost: tuple[float, ...] | None = None,
    allow_vias: bool = True,
    passes: int = 4,
) -> Result:
    """Route `nets` (all nets with two pads or more when None), first to last.

    `bounds` is the board's box (x0, y0, x1, y1); `pads` every pad that
    carries a net, routed or not, since the others are in the way;
    `obstacles` any other copper or hole, with `net` None for what belongs to
    no net. `via_cost` is the track length in mm a via is worth; `layer_cost`
    weighs a mm of track on each layer. Without `nets` the short nets go first.
    """
    rules = rules or {}
    by_net: dict[str, list[Pad]] = {}
    for p in pads:
        by_net.setdefault(p.net, []).append(p)
    if nets is None:
        order = sorted((n for n, ps in by_net.items() if len(ps) > 1), key=lambda n: (_span(by_net[n]), n))
    else:
        order = [n for n in dict.fromkeys(nets) if len(by_net.get(n, ())) > 1]
    layer_cost = tuple(layer_cost) if layer_cost else (1.0,) * layers
    router = _Router(bounds, list(pads), list(obstacles), rules, default_rule, grid, layers,
                     edge_clearance, via_cost, layer_cost, allow_vias)

    best: Result | None = None
    for attempt in range(max(1, passes)):
        result = router.run(order)
        result.passes = attempt + 1
        if best is None or len(result.failed) < len(best.failed):
            best = result
        if not result.failed:
            break
        failed = list(dict.fromkeys(net for net, _ in result.failed))
        again = failed + [n for n in order if n not in failed]
        if again == order:
            break
        order = again
    best.passes = attempt + 1
    return best
