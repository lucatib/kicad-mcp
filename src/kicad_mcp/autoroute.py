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
    #: What the copper belongs to, for a Policy that asks more than net classes.
    tag: object = field(default=None, compare=False)

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
    #: Pads of one net with the same group are joined already, by copper the
    #: router keeps: reaching one of them reaches them all. None: on its own.
    group: object = None


class Policy:
    """What a net's tracks must keep to. This one asks only the net classes:
    the larger of the two classes' clearances, one edge clearance, any layer,
    vias where `allow_vias`. Board rules subclass it (board_edit)."""

    def __init__(self, rules: dict[str, Rule] | None = None, default_rule: Rule = Rule(),
                 edge_clearance: float = 0.5, allow_vias: bool = True) -> None:
        self.rules = rules or {}
        self.default_rule = default_rule
        self.edge_clearance = edge_clearance
        self.allow_vias = allow_vias

    def rule(self, net: str | None) -> Rule:
        return self.rules.get(net, self.default_rule) if net else self.default_rule

    def clearance(self, net: str, shape: Shape) -> float:
        """Between a track of `net` and `shape`, another net's copper or a hole."""
        return max(self.rule(net).clearance, self.rule(shape.net).clearance)

    def edge(self, net: str) -> float:
        return self.edge_clearance

    def layer_ok(self, net: str, layer: int) -> bool:
        return True

    def vias(self, net: str) -> bool:
        return self.allow_vias


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
                  shape.layers, shape.net, shape.tag) for i in range(n)]


class _Router:
    def __init__(self, bounds, pads, obstacles, policy: Policy, grid, layers,
                 via_cost, layer_cost) -> None:
        self.lattice = _Lattice(bounds, grid, layers)
        self.pads = pads
        self.fixed = list(obstacles) + [p.shape for p in pads]
        self.policy = policy
        self.via_cost = via_cost
        self.layer_cost = layer_cost
        self.layers = layers

    def rule(self, net: str | None) -> Rule:
        return self.policy.rule(net)

    def run(self, order: list[str]) -> Result:
        result = Result()
        copper: list[Shape] = []        # what this pass has laid down
        for net in order:
            pads = [p for p in self.pads if p.net == net]
            if _joins(pads) < 1:
                continue
            self._route_net(net, pads, copper, result)
        return result

    # --- one net -------------------------------------------------------

    def _masks(self, net: str, copper: list[Shape]):
        """Per layer, where a track of `net` may not be centred; and where a via may not."""
        lat, rule, policy = self.lattice, self.rule(net), self.policy
        size = lat.nx * lat.ny
        tracks = [bytearray(size) for _ in range(lat.layers)]
        vias = bytearray(size) if self.layers > 1 and policy.vias(net) else None
        # Between two nodes a track dips closer to a corner than at either
        # node; asking a little more at the nodes covers the dip exactly.
        sag = (lat.grid * SQRT2 / 2) ** 2
        allowed = [policy.layer_ok(net, layer) for layer in range(lat.layers)]
        for layer, mask in enumerate(tracks):
            if allowed[layer]:
                lat.block_edge(mask, policy.edge(net) + rule.width / 2)
            else:
                mask[:] = b"\x01" * size
        if vias is not None:
            lat.block_edge(vias, policy.edge(net) + rule.via_diameter / 2)
        for shape in self.fixed + copper:
            if shape.net == net:
                continue
            gap = policy.clearance(net, shape)
            need = gap + rule.width / 2
            reach = math.sqrt(need * need + sag)
            for piece in _pieces(shape, 8 * lat.grid):
                for layer in piece.layers:
                    if layer < lat.layers and allowed[layer]:
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
            need = self.policy.clearance(net, shape) + rule.width / 2
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
        layers = [layer for layer in pad.shape.layers
                  if layer < lat.layers and self.policy.layer_ok(net, layer)]
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
        entries = [self._entries(net, p, tracks, copper) for p in pads]
        # A through-hole pad on a node joins its layers there at no cost.
        free_change = set()
        for nodes in entries:
            if len(nodes) > 1 and all(stub is None for stub in nodes.values()):
                free_change.update(index for _, index in nodes)

        # Pads already joined by kept copper travel together.
        groups: dict[object, list[int]] = {}
        for i, p in enumerate(pads):
            groups.setdefault(("pad", i) if p.group is None else ("group", p.group), []).append(i)
        members = list(groups.values())
        new: list[Shape] = []

        def lay(layer, x1, y1, x2, y2):
            if (x1, y1) == (x2, y2):
                return
            result.tracks.append((net, layer, round(x1, 6), round(y1, 6), round(x2, 6), round(y2, 6)))
            new.append(Shape("capsule", (x1, y1, x2, y2, rule.width / 2), (layer,), net))

        def grow(seed: int, waiting: list[int]) -> list[int]:
            """Join what can be reached from group `seed`; return the groups that cannot."""
            tree: dict[tuple[int, int], tuple | None] = {}
            for i in members[seed]:
                tree.update(entries[i])
            drawn: set[tuple] = set()

            def stub(node):
                line = tree.get(node)
                if line is not None and (node, line) not in drawn:
                    drawn.add((node, line))
                    lay(node[0], *line)

            waiting = list(waiting)
            while waiting:
                targets: dict[tuple[int, int], tuple[int, int]] = {}
                for g in waiting:
                    for i in members[g]:
                        for node in entries[i]:
                            targets.setdefault(node, (g, i))
                boxes = [_box([pads[i] for i in members[g]]) for g in waiting]
                path = self._search(tree, targets, boxes, tracks, vias, free_change) if targets else None
                if path is None:
                    return waiting
                g, i = targets[path[-1]]
                waiting.remove(g)
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
                tree[path[-1]] = entries[i].get(path[-1])
                stub(path[-1])
                for j in members[g]:
                    for node, line in entries[j].items():
                        tree.setdefault(node, line)
            return []

        # Start from the pad nearest the middle, or the kept copper around it.
        cx = sum(p.x for p in pads) / len(pads)
        cy = sum(p.y for p in pads) / len(pads)
        nearest = min(range(len(pads)), key=lambda i: math.hypot(pads[i].x - cx, pads[i].y - cy))
        seed = next(g for g, ms in enumerate(members) if nearest in ms)
        if any(p.group is not None for p in pads):
            seed = max(range(len(members)), key=lambda g: len(members[g]))
        left = [g for g in range(len(members)) if g != seed]
        parts = [[seed]]
        while left:
            stuck = grow(seed, left)
            parts[-1].extend(g for g in left if g not in stuck)
            if not stuck:
                break
            # What is left cannot reach this part; join what it can among itself.
            seed = max(stuck, key=lambda g: len(members[g]))
            left = [g for g in stuck if g != seed]
            parts.append([seed])
        if len(parts) > 1:
            # The part with the most real pads is the net; the rest failed.
            # Points on kept copper are named "~...".
            main = max(parts, key=lambda part: sum(
                1 for g in part for i in members[g] if not pads[i].name.startswith("~")))
            for part in parts:
                if part is main:
                    continue
                for g in part:
                    named = [pads[i].name for i in members[g] if not pads[i].name.startswith("~")]
                    result.failed.append((net, ", ".join(named[:4]) or pads[members[g][0]].name))
        copper.extend(new)

    def _search(self, tree, targets, boxes, tracks, vias, free_change):
        """The cheapest path from the tree to a target node, as [(layer, index)].

        `boxes` hold the targets, (x0, y0, x1, y1) in mm, for the estimate."""
        lat = self.lattice
        nx, grid = lat.nx, lat.grid
        goals = [((x0 - lat.x0) / grid, (y0 - lat.y0) / grid, (x1 - lat.x0) / grid, (y1 - lat.y0) / grid)
                 for x0, y0, x1, y1 in boxes]

        def bound(index: int) -> float:
            ix, iy = index % nx, index // nx
            best = math.inf
            for gx0, gy0, gx1, gy1 in goals:
                dx = max(gx0 - ix, 0.0, ix - gx1)
                dy = max(gy0 - iy, 0.0, iy - gy1)
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


def _box(pads: list[Pad]) -> tuple[float, float, float, float]:
    xs, ys = [p.x for p in pads], [p.y for p in pads]
    return min(xs), min(ys), max(xs), max(ys)


def _joins(pads: list[Pad]) -> int:
    """How many connections a net still needs: one fewer than its groups."""
    return len({("pad", i) if p.group is None else ("group", p.group) for i, p in enumerate(pads)}) - 1


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
    policy: Policy | None = None,
) -> Result:
    """Route `nets` (all nets with two pads or more when None), first to last.

    `bounds` is the board's box (x0, y0, x1, y1); `pads` every pad that
    carries a net, routed or not, since the others are in the way;
    `obstacles` any other copper or hole, with `net` None for what belongs to
    no net. `via_cost` is the track length in mm a via is worth; `layer_cost`
    weighs a mm of track on each layer. Without `nets` the short nets go first.
    Pads sharing a `group` are joined already and are connected as one.
    `policy` replaces `rules`, `default_rule`, `edge_clearance` and
    `allow_vias` with whatever a board's own rules ask.
    """
    if policy is None:
        policy = Policy(rules, default_rule, edge_clearance, allow_vias)
    by_net: dict[str, list[Pad]] = {}
    for p in pads:
        by_net.setdefault(p.net, []).append(p)
    if nets is None:
        order = sorted((n for n, ps in by_net.items() if _joins(ps) > 0), key=lambda n: (_span(by_net[n]), n))
    else:
        order = [n for n in dict.fromkeys(nets) if _joins(by_net.get(n, [])) > 0]
    layer_cost = tuple(layer_cost) if layer_cost else (1.0,) * layers
    router = _Router(bounds, list(pads), list(obstacles), policy, grid, layers, via_cost, layer_cost)

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
