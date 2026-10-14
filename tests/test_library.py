"""Drawing symbols into a project's own library.

Every symbol written here is read back two ways: through `SymbolIndex`, as the
other tools will see it, and through kicad-cli, which must load the library,
draw the symbol and connect the pins of a placed copy -- our parser agreeing
with our writer proves nothing.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from kicad_mcp import cli as kcli
from kicad_mcp import sexpr
from kicad_mcp.discovery import find_install
from kicad_mcp.editor import SchematicEditor
from kicad_mcp.errors import ToolInputError
from kicad_mcp.generator import generate_pinout_schematic
from kicad_mcp.library import create_symbol
from kicad_mcp.symbols import SymbolIndex

pytestmark = pytest.mark.requires_kicad

NAME = "LevelShifter_BSS138_4ch"
#: The 4-channel BSS138 module: LV header on the left, HV header on the right,
#: each side in the order it should read from the top.
SHIFTER = [
    {"number": "3", "name": "LV", "side": "left"},
    {"number": "1", "name": "LV1", "type": "bidirectional", "side": "left"},
    {"number": "2", "name": "LV2", "type": "bidirectional", "side": "left"},
    {"number": "5", "name": "LV3", "type": "bidirectional", "side": "left"},
    {"number": "6", "name": "LV4", "type": "bidirectional", "side": "left"},
    {"number": "4", "name": "GND", "side": "left"},
    {"number": "9", "name": "HV", "side": "right"},
    {"number": "7", "name": "HV1", "type": "bidirectional", "side": "right"},
    {"number": "8", "name": "HV2", "type": "bidirectional", "side": "right"},
    {"number": "11", "name": "HV3", "type": "bidirectional", "side": "right"},
    {"number": "12", "name": "HV4", "type": "bidirectional", "side": "right"},
    {"number": "10", "name": "GND", "side": "right"},
]


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    return folder


def _create(install, project, name=NAME, pins=SHIFTER, library="proj", **kw):
    return create_symbol(install, project, library, name, pins, **kw)


def _pins(install, project, lib_id):
    return SymbolIndex(install, project).pins(lib_id)


def _on_grid(v):
    return abs(round(v / 2.54) * 2.54 - v) < 1e-6


class TestCreateSymbol:
    def test_a_new_library_is_created_and_registered(self, install, project):
        res = _create(install, project, description="4-channel BSS138 level shifter module",
                      datasheet="https://example.com/ds.pdf", keywords="level shifter")

        assert res["lib_id"] == f"proj:{NAME}"
        assert res["created_library"] is True and res["registered"] is True
        table = (project / "sym-lib-table").read_text(encoding="utf-8")
        assert '(name "proj")' in table and '"${KIPRJMOD}/proj.kicad_sym"' in table
        pins = _pins(install, project, res["lib_id"])
        assert {(p.number, p.name, p.electrical_type) for p in pins} == {
            (p["number"], p["name"], p.get("type", "passive")) for p in SHIFTER
        }
        definition = SymbolIndex(install, project).definition(res["lib_id"])
        fields = {p[1]: p[2] for p in sexpr.children(definition, "property")}
        assert fields["Reference"] == "U"
        assert fields["Value"] == NAME
        assert fields["Datasheet"] == "https://example.com/ds.pdf"
        assert fields["Description"] == "4-channel BSS138 level shifter module"
        assert fields["ki_keywords"] == "level shifter"

    def test_search_finds_it(self, install, project):
        _create(install, project)
        hits = SymbolIndex(install, project).search("LevelShifter", library="proj")
        assert [h["lib_id"] for h in hits] == [f"proj:{NAME}"]

    def test_pins_sit_on_their_sides_in_list_order_on_the_grid(self, install, project):
        pins = [
            {"number": "1", "name": "IN", "type": "input", "side": "left"},
            {"number": "2", "name": "EN", "type": "input", "side": "left"},
            {"number": "3", "name": "NC", "type": "no_connect", "side": "left"},
            {"number": "4", "name": "OUT", "type": "power_out", "side": "right"},
            {"number": "5", "name": "PG", "type": "open_collector", "side": "right"},
            {"number": "6", "name": "VIN", "type": "power_in", "side": "top"},
            {"number": "7", "name": "GND", "type": "power_in", "side": "bottom"},
            {"number": "8", "name": "EP", "type": "power_in", "side": "bottom"},
        ]
        res = _create(install, project, name="Reg", pins=pins)
        got = {p.number: p for p in _pins(install, project, res["lib_id"])}

        assert all(_on_grid(p.x) and _on_grid(p.y) for p in got.values())
        for side, angle, numbers in (("left", 0, "123"), ("right", 180, "45"),
                                     ("top", 270, "6"), ("bottom", 90, "78")):
            placed = [got[n] for n in numbers]
            assert {p.angle for p in placed} == {angle}, side
            along = [p.y for p in placed] if angle in (0, 180) else [p.x for p in placed]
            across = {p.x for p in placed} if angle in (0, 180) else {p.y for p in placed}
            assert len(across) == 1, f"{side} pins line up"
            ordered = sorted(along, reverse=angle in (0, 180))  # top down, or left to right
            assert along == ordered and len(set(along)) == len(along), f"{side} pins in list order"
        assert got["1"].x < 0 < got["4"].x and got["7"].y < 0 < got["6"].y

    def test_without_sides_the_first_half_go_left(self, install, project):
        pins = [{"number": str(n), "name": f"P{n}"} for n in range(1, 6)]
        res = _create(install, project, name="Five", pins=pins)
        angles = {p.number: p.angle for p in _pins(install, project, res["lib_id"])}
        assert angles == {"1": 0, "2": 0, "3": 0, "4": 180, "5": 180}

    def test_the_body_clears_the_pin_names(self, install, project):
        left, right = "VERY_LONG_LEFT_NAME", "ANOTHER_LONG_ONE"
        pins = [{"number": "1", "name": left, "side": "left"},
                {"number": "2", "name": right, "side": "right"}]
        res = _create(install, project, name="Wide", pins=pins)
        definition = SymbolIndex(install, project).definition(res["lib_id"])
        rect = next(sexpr.find_all(definition, "rectangle"))
        width = abs(float(sexpr.values(rect, "end")[0]) - float(sexpr.values(rect, "start")[0]))
        assert width > (len(left) + len(right)) * 1.27 * 0.8

    def test_kicad_draws_it(self, install, project, tmp_path):
        res = _create(install, project)
        out = tmp_path / "svg"
        cli = kcli.run_cli(install, ["sym", "export", "svg", "-o", str(out), "--symbol", NAME,
                                     res["library_file"]])
        assert cli["returncode"] == 0, cli
        assert list(out.glob("*.svg"))

    def test_a_placed_copy_connects_its_pins(self, install, project):
        sheet = project / "proj.kicad_sch"
        stock = SymbolIndex(install)
        generate_pinout_schematic(stock, sheet, "Connector_Generic:Conn_01x03",
                                  {"1": "SDA", "2": "SCL"}, mcu_reference="J1")
        res = _create(install, project)

        editor = SchematicEditor.load(sheet)
        editor.place_symbol(SymbolIndex(install, project), res["lib_id"],
                            {"LV1": "SDA", "LV2": "SCL"}, reference="U9")
        editor.save()

        netlist = ET.parse(kcli.export_netlist(install, sheet)["output_file"]).getroot()
        nets = {n.get("name"): {(m.get("ref"), m.get("pin")) for m in n.findall("node")}
                for n in netlist.findall("./nets/net")}
        assert nets["/SDA"] == {("J1", "1"), ("U9", "1")}
        assert nets["/SCL"] == {("J1", "2"), ("U9", "2")}
        erc = kcli.run_erc(install, sheet)
        assert "Failed to load" not in erc["stdout"] + erc.get("report", "")

    def test_a_second_symbol_leaves_the_library_text_as_it_was(self, install, project):
        _create(install, project)
        lib = project / "proj.kicad_sym"
        first, table = lib.read_bytes(), (project / "sym-lib-table").read_bytes()

        res = _create(install, project, name="Other", pins=SHIFTER[:2])

        assert res["created_library"] is False and res["registered"] is False
        head = first[: first.rstrip().rfind(b")")]
        assert lib.read_bytes().startswith(head), "existing text kept byte for byte"
        assert (project / "sym-lib-table").read_bytes() == table
        assert {h["symbol"] for h in SymbolIndex(install, project).search("", library="proj")} == {NAME, "Other"}

    def test_a_registered_library_elsewhere_in_the_project_is_used(self, install, project):
        libs = project / "libs"
        libs.mkdir()
        original = (
            '(kicad_symbol_lib\r\n\t(version 20251024)\r\n\t(generator "kicad_symbol_editor")\r\n'
            '\t(generator_version "10.0")\r\n)\r\n'
        )
        (libs / "mine.kicad_sym").write_bytes(original.encode())
        table = (
            '(sym_lib_table\n  (version 7)\n'
            '  (lib (name "mine")(type "KiCad")(uri "${KIPRJMOD}/libs/mine.kicad_sym")(options "")(descr "Mine"))\n'
            ')\n'
        )
        (project / "sym-lib-table").write_text(table, encoding="utf-8")

        res = _create(install, project, library="mine")

        assert res["library_file"] == str(libs / "mine.kicad_sym")
        assert res["registered"] is False and res["created_library"] is False
        text = (libs / "mine.kicad_sym").read_bytes()
        assert text.startswith(original.encode()[: original.rstrip().rfind(")")])
        assert b"\n" not in text.replace(b"\r\n", b""), "keeps the file's CRLF line ends"
        assert (project / "sym-lib-table").read_text(encoding="utf-8") == table
        assert _pins(install, project, f"mine:{NAME}")

    def test_a_kicad_7_library_keeps_its_symbols_findable(self, install, project):
        # KiCad 6 and 7 indent with two spaces; search tells top-level symbols
        # by indentation, so a tab-indented addition would hide the old ones.
        original = (
            '(kicad_symbol_lib (version 20211014) (generator kicad_symbol_editor)\n'
            '  (symbol "Old_Part" (in_bom yes) (on_board yes)\n'
            '    (property "Reference" "U" (id 0) (at 0 0 0)\n'
            '      (effects (font (size 1.27 1.27)))\n'
            '    )\n'
            '    (symbol "Old_Part_1_1"\n'
            '      (pin passive line (at -5.08 0 0) (length 2.54)\n'
            '        (name "A" (effects (font (size 1.27 1.27))))\n'
            '        (number "1" (effects (font (size 1.27 1.27))))\n'
            '      )\n'
            '    )\n'
            '  )\n'
            ')\n'
        )
        (project / "proj.kicad_sym").write_text(original, encoding="utf-8")
        (project / "sym-lib-table").write_text(
            '(sym_lib_table\n  (lib (name "proj")(type "KiCad")(uri "${KIPRJMOD}/proj.kicad_sym")'
            '(options "")(descr ""))\n)\n', encoding="utf-8")

        _create(install, project)

        text = (project / "proj.kicad_sym").read_text(encoding="utf-8")
        assert "\t" not in text, "the addition is indented as the file is"
        assert f'\n  (symbol "{NAME}"' in text
        found = {h["symbol"] for h in SymbolIndex(install, project).search("", library="proj")}
        assert found == {"Old_Part", NAME}

    def test_an_existing_table_gets_the_new_library_in_its_own_style(self, install, project):
        table = (
            '(sym_lib_table\n  (version 7)\n'
            '  (lib (name "mine")(type "KiCad")(uri "${KIPRJMOD}/mine.kicad_sym")(options "")(descr ""))\n'
            ')\n'
        )
        (project / "sym-lib-table").write_text(table, encoding="utf-8")

        res = _create(install, project)

        assert res["registered"] is True
        lines = (project / "sym-lib-table").read_text(encoding="utf-8").splitlines()
        assert lines[:3] == table.splitlines()[:3]
        assert lines[3].startswith('  (lib (name "proj")') and lines[4] == ")"
        assert _pins(install, project, f"proj:{NAME}")

    @pytest.mark.parametrize("change,needle", [
        ({"library": "Device"}, "global"),
        ({"name": "Bad:Name"}, ":"),
        ({"name": " "}, "name"),
        ({"pins": []}, "pin"),
        ({"pins": [{"number": "1"}, {"number": "1"}]}, "more than once"),
        ({"pins": [{"number": "1", "type": "power"}]}, "power_in"),
        ({"pins": [{"number": "1", "side": "middle"}]}, "middle"),
        ({"pins": [{"number": "1", "side": "left"}, {"number": "2"}]}, "side"),
        ({"pins": [{"number": "1", "x": 2}]}, "x"),
        ({"pins": [{"name": "A"}]}, "number"),
        ({"reference": "U1"}, "reference"),
    ])
    def test_a_bad_request_is_refused_and_nothing_written(self, install, project, change, needle):
        kw = {"name": NAME, "pins": SHIFTER, "library": "proj", **change}
        with pytest.raises(ToolInputError) as exc:
            create_symbol(install, project, kw.pop("library"), kw.pop("name"), kw.pop("pins"), **kw)
        assert needle in exc.value.message
        assert exc.value.remedy
        assert sorted(p.name for p in project.iterdir()) == ["proj.kicad_pro"]

    def test_a_name_the_library_has_is_refused(self, install, project):
        _create(install, project)
        before = (project / "proj.kicad_sym").read_bytes()
        with pytest.raises(ToolInputError) as exc:
            _create(install, project)
        assert "already" in exc.value.message
        assert (project / "proj.kicad_sym").read_bytes() == before

    def test_outside_a_project_is_refused(self, install, tmp_path):
        with pytest.raises(ToolInputError) as exc:
            create_symbol(install, tmp_path, "proj", NAME, SHIFTER)
        assert ".kicad_pro" in exc.value.message
        assert list(tmp_path.iterdir()) == []
