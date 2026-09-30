"""The grid router on its own, no KiCad involved.

Coordinates are grid cells. The rules pinned here are the ones whose failure
would silently merge nets once drawn in KiCad: never touching a forbidden
point, and never running along or turning on another net's wire.
"""

from __future__ import annotations

from kicad_mcp.router import Grid, route_tree, segments

RIGHT, LEFT, DOWN, UP = (1, 0), (-1, 0), (0, 1), (0, -1)


def _cells(edges):
    return {c for e in edges for c in e}


def _bends(edges):
    segs, _ = segments(edges)
    return len(segs) - 1


class TestSinglePath:
    def test_straight_line_on_an_empty_grid(self):
        edges = route_tree(Grid(20, 10), [(2, 5), (12, 5)], {(2, 5): RIGHT, (12, 5): LEFT})
        segs, junctions = segments(edges)
        assert segs == [((2, 5), (12, 5))]
        assert junctions == []

    def test_offset_pins_get_a_single_bend(self):
        # Pins face each other's column: out, one turn, in.
        edges = route_tree(Grid(20, 20), [(2, 2), (12, 10)], {(2, 2): RIGHT, (12, 10): UP})
        assert _bends(edges) == 1

    def test_first_step_leaves_each_pin_in_its_exit_direction(self):
        # (5,5) exits LEFT although the other pin is to its right.
        edges = route_tree(Grid(30, 20), [(5, 5), (15, 5)], {(5, 5): LEFT, (15, 5): LEFT})
        assert frozenset({(5, 5), (4, 5)}) in edges
        assert frozenset({(15, 5), (16, 5)}) not in edges  # arrives inward, i.e. from its left
        assert frozenset({(15, 5), (14, 5)}) in edges

    def test_detours_around_a_blocked_block(self):
        grid = Grid(30, 20)
        for x in range(8, 13):
            for y in range(2, 12):
                grid.block((x, y))
        edges = route_tree(grid, [(2, 6), (20, 6)], {(2, 6): RIGHT, (20, 6): LEFT})
        assert edges
        assert not _cells(edges) & grid.blocked

    def test_never_touches_a_forbidden_cell(self):
        grid = Grid(20, 10)
        grid.forbid((7, 5))  # another net's pin right on the straight line
        edges = route_tree(grid, [(2, 5), (12, 5)], {(2, 5): RIGHT, (12, 5): LEFT})
        assert edges
        assert (7, 5) not in _cells(edges)

    def test_walled_in_pin_returns_none(self):
        grid = Grid(20, 10)
        for c in [(3, 4), (3, 5), (3, 6), (2, 4), (2, 6), (1, 4), (1, 5), (1, 6)]:
            grid.block(c)
        assert route_tree(grid, [(2, 5), (12, 5)], {(2, 5): RIGHT, (12, 5): LEFT}) is None


class TestForeignWires:
    def test_crosses_a_foreign_wire_only_at_right_angles(self):
        grid = Grid(30, 20)
        grid.add_wire((10, 1), (10, 18))  # vertical wall of wire between the pins
        edges = route_tree(grid, [(2, 8), (20, 8)], {(2, 8): RIGHT, (20, 8): LEFT})
        assert edges
        on_wire = [c for c in _cells(edges) if c[0] == 10]
        assert on_wire == [(10, 8)] or len(on_wire) == 1
        # And no bend on that cell: both neighbours along the route are horizontal.
        (cx, cy), = on_wire
        assert frozenset({(cx - 1, cy), (cx, cy)}) in edges
        assert frozenset({(cx, cy), (cx + 1, cy)}) in edges

    def test_never_runs_along_a_foreign_wire(self):
        grid = Grid(30, 10)
        grid.add_wire((4, 5), (16, 5))  # lies exactly on the straight path
        edges = route_tree(grid, [(2, 5), (18, 5)], {(2, 5): RIGHT, (18, 5): LEFT})
        # The ends of the foreign wire are forbidden and its interior refuses
        # parallel travel, so the route must leave row 5.
        assert edges is None or not ({(x, 5) for x in range(4, 17)} & _cells(edges))

    def test_foreign_wire_ends_are_forbidden(self):
        grid = Grid(20, 10)
        grid.add_wire((7, 5), (7, 9))  # ends at (7,5), on the straight path
        edges = route_tree(grid, [(2, 5), (12, 5)], {(2, 5): RIGHT, (12, 5): LEFT})
        assert edges
        assert (7, 5) not in _cells(edges)


class TestTrees:
    def test_three_pins_share_a_trunk_with_one_junction(self):
        pins = [(2, 5), (20, 5), (11, 15)]
        exits = {(2, 5): RIGHT, (20, 5): LEFT, (11, 15): UP}
        edges = route_tree(Grid(30, 20), pins, exits)
        segs, junctions = segments(edges)
        assert len(junctions) == 1
        assert all(p in _cells(edges) for p in pins)

    def test_duplicate_pins_are_routed_once(self):
        pins = [(2, 5), (2, 5), (12, 5)]
        edges = route_tree(Grid(20, 10), pins, {(2, 5): RIGHT, (12, 5): LEFT})
        assert segments(edges)[0] == [((2, 5), (12, 5))]

    def test_unrouted_pins_of_the_same_net_are_not_run_through(self):
        # Routing (2,5)->(12,5) must not pass over (7,5) before (7,5) joins.
        pins = [(2, 5), (12, 5), (7, 5)]
        exits = {(2, 5): RIGHT, (12, 5): LEFT, (7, 5): DOWN}
        edges = route_tree(Grid(20, 12), pins, exits)
        assert edges
        # (7,5) is a tree leaf reached from below, never a through-point.
        assert sum(1 for e in edges if (7, 5) in e) == 1
