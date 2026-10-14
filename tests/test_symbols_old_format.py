"""Libraries saved by older KiCad versions are found like any other.

KiCad 6 and 7 indent with two spaces, KiCad 10 with tabs, and KiCad 10 still
reads both. Plenty of libraries in use are in the older layout -- Würth
Elektronik's whole collection, for one -- so search must not depend on it.
"""

from __future__ import annotations

import textwrap
import xml.etree.ElementTree as ET

import pytest

from kicad_mcp import cli as kcli
from kicad_mcp import symbols
from kicad_mcp.discovery import find_install
from kicad_mcp.editor import SchematicEditor
from kicad_mcp.generator import generate_pinout_schematic
from kicad_mcp.symbols import SymbolIndex

pytestmark = pytest.mark.requires_kicad

#: As KiCad 7 wrote it: two-space indent, (id N) on properties, bare `hide`.
KICAD7_LIB = textwrap.dedent("""\
    (kicad_symbol_lib (version 20220914) (generator kicad_symbol_editor)
      (symbol "1010_7449150023" (pin_names hide) (in_bom yes) (on_board yes)
        (property "Reference" "L" (id 0) (at 0 5.08 0)
          (effects (font (size 1.27 1.27)))
        )
        (property "Value" "23.0 nH" (id 1) (at 0 -5.08 0)
          (effects (font (size 1.27 1.27)) hide)
        )
        (property "Footprint" "" (id 2) (at 0 0 0)
          (effects (font (size 1.27 1.27)) hide)
        )
        (symbol "1010_7449150023_0_1"
          (arc (start 0 -2.54) (mid 0.635 -1.905) (end 0 -1.27)
            (stroke (width 0) (type default))
            (fill (type none))
          )
        )
        (symbol "1010_7449150023_1_1"
          (pin passive line (at 0 -5.08 90) (length 2.54)
            (name "1" (effects (font (size 1.27 1.27))))
            (number "1" (effects (font (size 1.27 1.27))))
          )
          (pin passive line (at 0 5.08 270) (length 2.54)
            (name "2" (effects (font (size 1.27 1.27))))
            (number "2" (effects (font (size 1.27 1.27))))
          )
        )
      )
      (symbol "1010_7449150046" (pin_names hide) (in_bom yes) (on_board yes)
        (property "Reference" "L" (id 0) (at 0 5.08 0)
          (effects (font (size 1.27 1.27)))
        )
        (symbol "1010_7449150046_1_1"
          (pin passive line (at 0 -5.08 90) (length 2.54)
            (name "1" (effects (font (size 1.27 1.27))))
            (number "1" (effects (font (size 1.27 1.27))))
          )
        )
      )
    )
    """).replace("\n", "\r\n")  # and CRLF, as Würth ships it

TABLE = (
    '(sym_lib_table\n\t(version 7)\n'
    '\t(lib (name "Old") (type "KiCad") '
    '(uri "${KIPRJMOD}/Old.kicad_sym") (options "") (descr ""))\n)\n'
)


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "Old.kicad_sym").write_bytes(KICAD7_LIB.encode("utf-8"))
    (tmp_path / "sym-lib-table").write_text(TABLE, encoding="utf-8")
    return tmp_path


def test_search_finds_every_top_level_symbol_and_no_unit(install, project):
    hits = SymbolIndex(install, project).search("1010_", library="Old")

    assert [h["lib_id"] for h in hits] == ["Old:1010_7449150023", "Old:1010_7449150046"]


def test_a_name_list_cached_by_an_older_scan_is_redone(install, project, monkeypatch):
    lib = project / "Old.kicad_sym"
    stat = lib.stat()
    stale = {str(lib): {"stamp": [int(stat.st_mtime), stat.st_size], "names": []}}
    monkeypatch.setattr(symbols, "_NAME_CACHE", stale)
    monkeypatch.setattr(symbols, "_save_name_cache", lambda cache: None)

    assert SymbolIndex(install, project).search("7449150023", library="Old")


def test_a_placed_symbol_from_it_loads_in_kicad(install, project):
    index = SymbolIndex(install, project)
    sch = project / "t.kicad_sch"
    generate_pinout_schematic(index, sch, "RF_Module:ESP32-S3-MINI-1", {"IO4": "LED"})

    editor = SchematicEditor.load(sch)
    editor.place_symbol(index, "Old:1010_7449150023", {"1": "LED"}, reference="L1")
    editor.save()

    result = kcli.export_netlist(install, sch)
    root = ET.parse(result["output_file"]).getroot()
    led = next(n for n in root.findall("./nets/net") if n.get("name") == "/LED")
    assert ("L1", "1") in {(n.get("ref"), n.get("pin")) for n in led.findall("node")}
