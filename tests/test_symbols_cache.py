"""Library resolution must reflect the filesystem, not a first-call snapshot.

A SymbolIndex is cached per project_dir for the life of the server process, so
anything it memoizes internally outlives every tool call. Two caches used to do
that unconditionally: the resolved sym-lib-table entries, and the parsed
library files. The effect was that registering a project-local library, or
editing one already registered, was invisible until the server restarted.
"""

from __future__ import annotations

import textwrap
import time

import pytest

from kicad_mcp.discovery import find_install
from kicad_mcp.symbols import SymbolIndex

pytestmark = pytest.mark.requires_kicad

MINIMAL_LIB = textwrap.dedent("""\
    (kicad_symbol_lib
    \t(version 20251024)
    \t(generator "test")
    \t(symbol "{name}"
    \t\t(exclude_from_sim no)
    \t\t(in_bom yes)
    \t\t(on_board yes)
    \t\t(symbol "{name}_1_1"
    \t\t\t(pin passive line
    \t\t\t\t(at 0 0 0)
    \t\t\t\t(length 2.54)
    \t\t\t\t(name "A")
    \t\t\t\t(number "1")
    \t\t\t)
    \t\t)
    \t)
    )
    """)

TABLE_EMPTY = '(sym_lib_table\n\t(version 7)\n)\n'
TABLE_WITH_LIB = (
    '(sym_lib_table\n\t(version 7)\n'
    '\t(lib (name "Scratch") (type "KiCad") '
    '(uri "${KIPRJMOD}/libs/Scratch.kicad_sym") (options "") (descr ""))\n)\n'
)


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "libs").mkdir()
    (tmp_path / "libs" / "Scratch.kicad_sym").write_text(
        MINIMAL_LIB.format(name="Widget"), encoding="utf-8"
    )
    return tmp_path


def test_newly_registered_library_is_visible_without_a_new_index(install, project):
    table = project / "sym-lib-table"
    table.write_text(TABLE_EMPTY, encoding="utf-8")
    index = SymbolIndex(install, project)
    assert "Scratch" not in index.entries

    table.write_text(TABLE_WITH_LIB, encoding="utf-8")

    assert "Scratch" in index.entries
    assert [r["lib_id"] for r in index.search("Widget")] == ["Scratch:Widget"]


def test_edited_library_file_is_reparsed(install, project):
    (project / "sym-lib-table").write_text(TABLE_WITH_LIB, encoding="utf-8")
    lib = project / "libs" / "Scratch.kicad_sym"
    index = SymbolIndex(install, project)
    assert len(index.pins("Scratch:Widget")) == 1

    # The stamp guarding the parse cache has 1s resolution.
    time.sleep(1.1)
    lib.write_text(MINIMAL_LIB.format(name="Gadget"), encoding="utf-8")

    assert index.definition("Scratch:Gadget") is not None
    with pytest.raises(Exception):
        index.definition("Scratch:Widget")
