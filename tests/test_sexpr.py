"""S-expression round-trip tests.

The writer is the riskiest part of the package: if it emits something KiCad
rejects, generated schematics are worthless. These tests pin the behaviour that
matters -- bare symbols staying bare, quoted strings staying quoted, and
re-serialisation being stable.
"""

from __future__ import annotations

import pytest

from kicad_mcp import sexpr
from kicad_mcp.sexpr import Sym


def test_parses_nested_lists():
    tree = sexpr.parse('(kicad_sch (version 20250610) (paper "A4"))')
    assert tree[0] == Sym("kicad_sch")
    assert sexpr.value(tree, "version") == 20250610
    assert sexpr.value(tree, "paper") == "A4"


def test_distinguishes_bare_symbols_from_strings():
    """`(hide yes)` and `(hide "yes")` are different tokens to KiCad."""
    bare = sexpr.parse("(hide yes)")
    quoted = sexpr.parse('(hide "yes")')
    assert bare[1] == Sym("yes")
    assert quoted[1] == "yes"
    assert sexpr.dumps(bare).strip() == "(hide yes)"
    assert sexpr.dumps(quoted).strip() == '(hide "yes")'


def test_numbers_round_trip_without_scientific_notation():
    tree = sexpr.parse("(at 152.4 -101.6 270)")
    out = sexpr.dumps(tree).strip()
    assert out == "(at 152.4 -101.6 270)"


def test_escapes_quotes_in_strings():
    tree = sexpr.parse(r'(property "Name" "a \"b\" c")')
    assert tree[2] == 'a "b" c'
    assert sexpr.parse(sexpr.dumps(tree)) == tree


def test_reserialising_is_idempotent():
    src = '(a (b 1) (c "x") (d (e yes)))'
    once = sexpr.dumps(sexpr.parse(src))
    twice = sexpr.dumps(sexpr.parse(once))
    assert once == twice


def test_nested_nodes_are_indented_with_tabs():
    out = sexpr.dumps(sexpr.parse("(a (b 1))"))
    assert out == "(a\n\t(b 1)\n)\n"


def test_value_returns_default_when_absent():
    tree = sexpr.parse("(a (b 1))")
    assert sexpr.value(tree, "missing", "fallback") == "fallback"


def test_rejects_trailing_content():
    with pytest.raises(SyntaxError):
        sexpr.parse("(a) (b)")


def test_rejects_unterminated_list():
    with pytest.raises(SyntaxError):
        sexpr.parse("(a (b)")


def test_children_and_find_all():
    tree = sexpr.parse("(root (x 1) (x 2) (y (x 3)))")
    assert len(list(sexpr.children(tree, "x"))) == 2  # direct children only
    assert len(list(sexpr.find_all(tree, "x"))) == 3  # recursive
