"""`extends`-based symbol resolution.

Many stock symbols (regulator variants, several logic families) declare
`(extends "Base")` and carry no pins of their own -- the base holds them. This
was silently broken: `get_symbol_pins` returned zero pins for every derived
symbol, and the first embedded schematic built from one was rejected outright
by kicad-cli with "Failed to load schematic". Two separate defects, both
covered here: pins must resolve, and the embedded file must be structurally
valid to KiCad itself, not just parse cleanly through our own reader.
"""

from __future__ import annotations

import subprocess

import pytest

from kicad_mcp.discovery import find_install
from kicad_mcp.generator import SchematicBuilder
from kicad_mcp.symbols import SymbolIndex

pytestmark = pytest.mark.requires_kicad

#: A stock symbol declared as `(extends "AP1117-15")` with no pins of its own.
DERIVED = "Regulator_Linear:AMS1117-3.3"


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


@pytest.fixture(scope="module")
def index(install):
    return SymbolIndex(install)


def test_derived_symbol_definition_has_no_dangling_extends(index):
    from kicad_mcp import sexpr

    node = index.definition(DERIVED)
    assert sexpr.child(node, "extends") is None


def test_derived_symbol_pins_resolve_from_base(index):
    pins = index.pins(DERIVED)
    names = {p.name for p in pins}
    assert names == {"GND", "VO", "VI"}


def test_derived_symbol_subsymbols_are_renamed_to_match_parent(index):
    """KiCad rejects the file if a unit sub-symbol keeps the base's name."""
    from kicad_mcp import sexpr

    node = index.definition(DERIVED)
    sub_names = [s[1] for s in sexpr.children(node, "symbol") if len(s) > 1]
    assert sub_names, "expected pin/graphic sub-symbols to be present"
    for name in sub_names:
        assert name.startswith("AMS1117-3.3_"), name


def test_embedded_derived_symbol_is_accepted_by_kicad_cli(install, index, tmp_path):
    """The structural check that actually matters: kicad-cli must load the file.

    Our own S-expression reader accepting a file proves nothing about whether
    KiCad's parser will -- that gap is exactly how this bug shipped once.
    """
    out = tmp_path / "extends_check.kicad_sch"
    builder = SchematicBuilder("t")
    builder.add_symbol(index, DERIVED, "U1", "AMS1117-3.3", x=100, y=100)
    builder.write(out)

    result = subprocess.run(
        [str(install.cli_path), "sch", "export", "netlist",
         "--format", "kicadxml", "-o", str(tmp_path / "out.xml"), str(out)],
        capture_output=True, text=True, timeout=60,
        stdin=subprocess.DEVNULL,  # pytest's captured stdin has no real handle to inherit
    )
    assert result.returncode == 0, result.stderr or result.stdout
    xml = (tmp_path / "out.xml").read_text(encoding="utf-8")
    assert 'ref="U1"' in xml
