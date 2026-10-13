"""Sheets inside a project: annotation paths, power symbols, global labels.

A sheet is not a project of its own. Symbols on it are annotated under the
path of sheet symbols leading to it from the root, a sheet used twice needs a
reference per use, and only power symbols and global labels connect across
sheets -- a local label "GND" on two sheets is two nets. Every connectivity
claim here is checked with kicad-cli on the whole project, never on our own
reading of the files.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from kicad_mcp import cli as kcli
from kicad_mcp import hierarchy, sexpr
from kicad_mcp.discovery import find_install
from kicad_mcp.editor import SchematicEditor, _local_points
from kicad_mcp.errors import ToolInputError
from kicad_mcp.generator import PlacedSymbol, SchematicBuilder, generate_pinout_schematic
from kicad_mcp.sexpr import Sym, make
from kicad_mcp.symbols import SymbolIndex

pytestmark = pytest.mark.requires_kicad

CONN = "Connector_Generic:Conn_01x03"
RES = "Device:R"


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


@pytest.fixture(scope="module")
def index(install):
    return SymbolIndex(install)


def _sheet(parent_path: str, name: str, file: str, page: int, x: float) -> list:
    """A sheet symbol as KiCad writes it, pointing at `file`."""
    font = make("font", make("size", 1.27, 1.27))
    return [
        Sym("sheet"), make("at", x, 30), make("size", 25.4, 15.24),
        make("exclude_from_sim", Sym("no")), make("in_bom", Sym("yes")),
        make("on_board", Sym("yes")), make("dnp", Sym("no")),
        [Sym("stroke"), make("width", 0), make("type", Sym("solid"))],
        [Sym("fill"), [Sym("color"), 0, 0, 0, 0.0]],
        make("uuid", str(uuidlib.uuid4())),
        [Sym("property"), "Sheetname", name, make("at", x, 29.2, 0),
         make("effects", font, make("justify", Sym("left"), Sym("bottom")))],
        [Sym("property"), "Sheetfile", file, make("at", x, 45.8, 0),
         make("effects", font, make("justify", Sym("left"), Sym("top")))],
        [Sym("instances"), [Sym("project"), "proj", [Sym("path"), parent_path, make("page", str(page))]]],
    ]


def _project(tmp_path, uses: int = 1, sub: str = "sub.kicad_sch"):
    """proj.kicad_pro with a root sheet that uses `sub` `uses` times.

    Returns (root path, sub path, [sheet uuid per use]).
    """
    (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    root = SchematicBuilder("proj", paper="A4")
    tree = root.build()
    sheets = [_sheet(f"/{root.uuid}", f"S{k}", sub, k + 2, 40 + 50 * k) for k in range(uses)]
    tree[-2:-2] = sheets
    root_path = tmp_path / "proj.kicad_sch"
    root_path.write_text(sexpr.dumps(tree), encoding="utf-8")
    sub_path = tmp_path / sub
    sub_path.parent.mkdir(parents=True, exist_ok=True)
    SchematicBuilder("ignored", paper="A4").write(sub_path)
    return root_path, sub_path, [str(sexpr.value(s, "uuid")) for s in sheets], root.uuid


def _instances(path, reference):
    """(project, [(path, reference)]) of the symbol shown as `reference`."""
    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    for symbol in sexpr.children(tree, "symbol"):
        props = {p[1]: p[2] for p in sexpr.children(symbol, "property")}
        if props.get("Reference") == reference:
            project = sexpr.child(sexpr.child(symbol, "instances"), "project")
            return project[1], [(p[1], sexpr.value(p, "reference")) for p in sexpr.children(project, "path")]
    raise AssertionError(f"{reference} not in {path.name}")


def _nets(install, root):
    netlist = kcli.parse_kicadxml_netlist(kcli.export_netlist(install, root)["output_file"])
    return netlist, {
        n["name"]: {(x["reference"], x["pin"]) for x in n["nodes"]} for n in netlist["nets"]
    }


def _edit(path, fn):
    editor = SchematicEditor.load(path)
    result = fn(editor)
    editor.save()
    return result


# --- where a sheet sits ------------------------------------------------------


class TestLocate:
    def test_sub_sheet_path_runs_through_its_sheet_symbol(self, tmp_path):
        root, sub, (sheet,), root_uuid = _project(tmp_path)
        place = hierarchy.locate(sub)
        assert place.project == "proj"
        assert place.paths == [f"/{root_uuid}/{sheet}"]

    def test_root_sheet_is_annotated_under_its_own_uuid(self, tmp_path):
        root, _, _, root_uuid = _project(tmp_path)
        place = hierarchy.locate(root)
        assert (place.project, place.paths) == ("proj", [f"/{root_uuid}"])

    def test_a_reused_sheet_has_one_path_per_use(self, tmp_path):
        _, sub, sheets, root_uuid = _project(tmp_path, uses=2)
        assert hierarchy.locate(sub).paths == [f"/{root_uuid}/{s}" for s in sheets]

    def test_sheet_in_a_subfolder_finds_the_project_above_it(self, tmp_path):
        _, sub, (sheet,), root_uuid = _project(tmp_path, sub="sheets/power.kicad_sch")
        place = hierarchy.locate(sub)
        assert (place.project, place.paths) == ("proj", [f"/{root_uuid}/{sheet}"])

    def test_a_file_outside_any_project_is_its_own_root(self, tmp_path):
        alone = tmp_path / "alone.kicad_sch"
        SchematicBuilder("x").write(alone)
        place = hierarchy.locate(alone)
        assert place.project == "alone"
        assert place.root is None


class TestInstancePaths:
    def test_symbol_on_a_sub_sheet_is_annotated_under_the_project(self, install, index, tmp_path):
        root, sub, (sheet,), root_uuid = _project(tmp_path)
        _edit(sub, lambda e: e.place_symbol(index, RES, {}, reference="R7"))

        assert _instances(sub, "R7") == ("proj", [(f"/{root_uuid}/{sheet}", "R7")])
        netlist, _ = _nets(install, root)
        assert [c["reference"] for c in netlist["components"]] == ["R7"]

    def test_reused_sheet_gets_a_distinct_reference_per_use(self, install, index, tmp_path):
        root, sub, sheets, root_uuid = _project(tmp_path, uses=2)
        res = _edit(sub, lambda e: e.place_symbol(index, RES, {}))

        project, paths = _instances(sub, res["reference"])
        assert project == "proj"
        assert [p for p, _ in paths] == [f"/{root_uuid}/{s}" for s in sheets]
        assert len({r for _, r in paths}) == 2
        assert res["instances"] == [{"path": p, "reference": r} for p, r in paths]
        netlist, _ = _nets(install, root)
        refs = [c["reference"] for c in netlist["components"]]
        assert sorted(refs) == sorted(r for _, r in paths), "each use must be its own part"

    def test_new_references_are_free_across_the_whole_project(self, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(root, lambda e: e.place_symbol(index, RES, {}, reference="R1"))
        res = _edit(sub, lambda e: e.place_symbol(index, RES, {}))
        assert res["reference"] == "R2"

    def test_explicit_reference_taken_on_another_sheet_is_warned(self, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(root, lambda e: e.place_symbol(index, RES, {}, reference="R1"))
        res = _edit(sub, lambda e: e.place_symbol(index, RES, {}, reference="R1"))
        assert any("R1" in w and "already" in w for w in res["warnings"])

    def test_renaming_one_use_of_a_reused_sheet_leaves_the_other(self, index, tmp_path):
        _, sub, _, _ = _project(tmp_path, uses=2)
        res = _edit(sub, lambda e: e.place_symbol(index, RES, {}))
        first, second = (i["reference"] for i in res["instances"])
        _edit(sub, lambda e: e.set_fields(first, {"Reference": "R50"}))
        assert [r for _, r in _instances(sub, "R50")[1]] == ["R50", second]

    def test_removing_from_a_reused_sheet_removes_every_use_and_says_so(self, install, index, tmp_path):
        root, sub, _, _ = _project(tmp_path, uses=2)
        res = _edit(sub, lambda e: e.place_symbol(index, RES, {}))
        first, second = (i["reference"] for i in res["instances"])

        out = _edit(sub, lambda e: e.remove_symbols(index, [second]))

        assert any(first in w and second in w for w in out["warnings"])
        netlist, _ = _nets(install, root)
        assert netlist["components"] == []

    def test_standalone_file_keeps_its_own_uuid_path(self, index, tmp_path):
        alone = tmp_path / "alone.kicad_sch"
        SchematicBuilder("x").write(alone)
        own = sexpr.value(sexpr.parse(alone.read_text(encoding="utf-8")), "uuid")
        _edit(alone, lambda e: e.place_symbol(index, RES, {}, reference="R1"))
        assert _instances(alone, "R1") == ("alone", [(f"/{own}", "R1")])


# --- power symbols -----------------------------------------------------------


class TestPowerNets:
    def test_rail_names_become_power_symbols_not_labels(self, index, tmp_path):
        _, sub, _, _ = _project(tmp_path)
        _edit(sub, lambda e: e.place_symbol(index, CONN, {}, reference="J1"))
        res = _edit(sub, lambda e: e.label_pins(index, "J1", {"1": "GND", "2": "+3V3"}))

        assert [(c["net"], c["via"], c.get("symbol")) for c in res["connected"]] == [
            ("GND", "power_symbol", "power:GND"), ("+3V3", "power_symbol", "power:+3V3"),
        ]
        tree = sexpr.parse(sub.read_text(encoding="utf-8"))
        assert not list(sexpr.children(tree, "label"))

    def test_rails_connect_across_sheets(self, install, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(root, lambda e: e.place_symbol(index, RES, {"1": "+3V3", "2": "GND"}, reference="R1"))
        _edit(sub, lambda e: e.place_symbol(index, CONN, {"1": "+3V3", "3": "GND"}, reference="J1"))

        _, nets = _nets(install, root)
        assert nets["+3V3"] == {("R1", "1"), ("J1", "1")}
        assert nets["GND"] == {("R1", "2"), ("J1", "3")}

    def test_explicit_power_lib_id(self, install, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        res = _edit(sub, lambda e: e.place_symbol(index, CONN, {"1": "power:+5V"}, reference="J1"))
        assert res["connected"][0]["net"] == "+5V"
        _, nets = _nets(install, root)
        assert nets["+5V"] == {("J1", "1")}

    def test_lib_id_that_is_not_a_power_symbol_is_refused(self, index, tmp_path):
        _, sub, _, _ = _project(tmp_path)
        editor = SchematicEditor.load(sub)
        with pytest.raises(ToolInputError):
            editor.place_symbol(index, CONN, {"1": "Device:R"}, reference="J1")
        with pytest.raises(ToolInputError):
            editor.place_symbol(index, CONN, {"1": "power:NO_SUCH_RAIL"}, reference="J1")

    def test_power_references_are_unique_across_the_project(self, install, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(root, lambda e: e.place_symbol(index, RES, {"1": "+3V3", "2": "GND"}, reference="R1"))
        _edit(sub, lambda e: e.place_symbol(index, RES, {"1": "+3V3", "2": "GND"}, reference="R2"))
        refs = set()
        for path in (root, sub):
            tree = sexpr.parse(path.read_text(encoding="utf-8"))
            for symbol in sexpr.children(tree, "symbol"):
                ref = next(p[2] for p in sexpr.children(symbol, "property") if p[1] == "Reference")
                if ref.startswith("#PWR"):
                    assert ref not in refs, f"{ref} used twice"
                    refs.add(ref)
        assert len(refs) == 4

    @pytest.mark.parametrize("angle", [0, 90, 180, 270])
    def test_power_symbol_points_away_from_its_pin(self, index, tmp_path, angle):
        """Body and Value text lie beyond the stub end, never back over the part."""
        _, sub, _, _ = _project(tmp_path)
        _edit(sub, lambda e: e.place_symbol(index, CONN, {}, reference="J1", at=(101.6, 101.6)))
        _rotate(sub, "J1", angle)
        editor = SchematicEditor.load(sub)
        editor.label_pins(index, "J1", {"2": "GND"})
        editor.save()

        (sym,) = editor.placements(index, "J1")
        pin = next(p for p in sym.pins if p.number == "2")
        (px, py), (dx, dy) = sym.pin_point(pin), sym.pin_outward(pin)
        tree = sexpr.parse(sub.read_text(encoding="utf-8"))
        gnd = next(s for s in sexpr.children(tree, "symbol") if sexpr.value(s, "lib_id") == "power:GND")
        at = sexpr.values(gnd, "at")
        placed = PlacedSymbol("", "", "power:GND", x=at[0], y=at[1], angle=at[2], unit=1, uuid="")
        body = [placed.to_canvas(x, y) for x, y in _local_points(index.definition("power:GND"))]
        assert min((x - px) * dx + (y - py) * dy for x, y in body) >= 5.08 - 1e-6
        value = next(p for p in sexpr.children(gnd, "property") if p[1] == "Value")
        vx, vy, v_angle = sexpr.values(value, "at")[:3]
        tip = max((x - px) * dx + (y - py) * dy for x, y in body)
        assert (vx - px) * dx + (vy - py) * dy > tip
        # Centred, so KiCad cannot flip it back over the symbol, and reading
        # along the stub: a field's angle is toggled by a 90/270 symbol turn.
        assert sexpr.child(sexpr.child(value, "effects"), "justify") is None
        shown_vertical = (int(v_angle) % 180 == 90) != (int(at[2]) % 180 == 90)
        assert shown_vertical == (round(dx) == 0)


# --- global labels -----------------------------------------------------------


class TestGlobalLabels:
    def test_global_nets_connect_across_sheets(self, install, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(root, lambda e: e.place_symbol(index, RES, {"1": "SDA"}, reference="R1", global_nets=["SDA"]))
        res = _edit(sub, lambda e: e.place_symbol(index, CONN, {"1": "SDA"}, reference="J1", global_nets=["SDA"]))

        assert res["connected"][0]["via"] == "global_label"
        _, nets = _nets(install, root)
        assert nets["SDA"] == {("R1", "1"), ("J1", "1")}

    def test_a_name_already_global_in_the_project_stays_global(self, install, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(root, lambda e: e.place_symbol(index, RES, {"1": "SCL"}, reference="R1", global_nets=["SCL"]))
        _edit(sub, lambda e: e.place_symbol(index, CONN, {}, reference="J1"))
        res = _edit(sub, lambda e: e.label_pins(index, "J1", {"2": "SCL"}))

        assert res["connected"][0]["via"] == "global_label"
        _, nets = _nets(install, root)
        assert nets["SCL"] == {("R1", "1"), ("J1", "2")}

    def test_global_label_shape_follows_the_pin(self, index, tmp_path):
        _, sub, _, _ = _project(tmp_path)
        _edit(sub, lambda e: e.place_symbol(index, CONN, {"1": "X"}, reference="J1", global_nets=["X"]))
        tree = sexpr.parse(sub.read_text(encoding="utf-8"))
        (label,) = sexpr.children(tree, "global_label")
        assert sexpr.value(label, "shape") == "passive"  # connector pins are passive

    def test_same_name_as_a_local_label_elsewhere_is_warned(self, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(root, lambda e: e.place_symbol(index, RES, {"1": "DATA"}, reference="R1"))
        res = _edit(sub, lambda e: e.place_symbol(index, CONN, {"1": "DATA"}, reference="J1"))
        assert any("DATA" in w and "proj.kicad_sch" in w for w in res["warnings"])

    def test_local_nets_stay_local(self, install, index, tmp_path):
        root, sub, _, _ = _project(tmp_path)
        _edit(sub, lambda e: e.place_symbol(index, CONN, {"1": "LOCAL"}, reference="J1"))
        res = _edit(sub, lambda e: e.place_symbol(index, CONN, {"1": "LOCAL"}, reference="J2"))
        assert res["connected"][0]["via"] == "label"
        assert res["warnings"] == []
        _, nets = _nets(install, root)
        assert nets["/S0/LOCAL"] == {("J1", "1"), ("J2", "1")}


# --- labels read away from their pin -------------------------------------------


def _reads_toward(angle: float, justify: list[str]) -> tuple[int, int]:
    """Which way KiCad lays a label's text from its anchor.

    KiCad keeps label text upright (270 loads as 90, 180 as 0), so direction
    comes from the justification: left reads right/up, right reads left/down.
    """
    vertical = int(angle) % 180 == 90
    forward = "right" not in justify
    if vertical:
        return (0, -1) if forward else (0, 1)
    return (1, 0) if forward else (-1, 0)


@pytest.mark.parametrize("kind,net_args", [
    ("label", {}), ("global_label", {"global_nets": ["N1", "N2", "N3"]}),
])
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_labels_read_away_from_their_pin(index, tmp_path, kind, net_args, angle):
    _, sub, _, _ = _project(tmp_path)
    _edit(sub, lambda e: e.place_symbol(index, CONN, {}, reference="J1", at=(101.6, 101.6)))
    _rotate(sub, "J1", angle)
    editor = SchematicEditor.load(sub)
    editor.label_pins(index, "J1", {"1": "N1", "2": "N2", "3": "N3"}, **net_args)
    editor.save()

    (sym,) = editor.placements(index, "J1")
    outward = {(round(sym.pin_outward(p)[0]), round(sym.pin_outward(p)[1])) for p in sym.pins}
    tree = sexpr.parse(sub.read_text(encoding="utf-8"))
    labels = list(sexpr.children(tree, kind))
    assert len(labels) == 3
    for label in labels:
        at = sexpr.values(label, "at")
        justify = [str(j) for j in sexpr.values(sexpr.child(label, "effects"), "justify")]
        assert {_reads_toward(at[2], justify)} == outward, f"{label[1]} reads back over its pin"


# --- generation into a project -------------------------------------------------


class TestGeneration:
    def test_generated_sub_sheet_is_annotated_under_the_project(self, install, index, tmp_path):
        root, sub, (sheet,), root_uuid = _project(tmp_path)
        res = generate_pinout_schematic(
            index, sub, "RF_Module:ESP32-S3-MINI-1", {"IO4": "SDA", "IO5": "LED"}, global_nets=["SDA"],
        )
        assert _instances(sub, "U1") == ("proj", [(f"/{root_uuid}/{sheet}", "U1")])
        assert {c["net"]: c["via"] for c in res["connected"]} == {"SDA": "global_label", "LED": "label"}
        _, nets = _nets(install, root)
        assert ("U1", "8") in nets["SDA"]  # IO4 is pin 8
        assert any(name == "GND" for name in nets)

    def test_power_pins_get_symbols_facing_away_from_the_mcu(self, index, tmp_path):
        out = tmp_path / "mcu.kicad_sch"
        generate_pinout_schematic(index, out, "RF_Module:ESP32-S3-MINI-1", {})
        editor = SchematicEditor.load(out)
        (mcu,) = editor.placements(index, "U1")
        tree = sexpr.parse(out.read_text(encoding="utf-8"))
        for symbol in sexpr.children(tree, "symbol"):
            lib_id = sexpr.value(symbol, "lib_id")
            if lib_id not in ("power:GND", "power:+3V3"):
                continue
            at = sexpr.values(symbol, "at")
            placed = PlacedSymbol("", "", lib_id, x=at[0], y=at[1], angle=at[2], unit=1, uuid="")
            body = [placed.to_canvas(x, y) for x, y in _local_points(index.definition(lib_id), include_pins=False)]
            cx = sum(x for x, _ in body) / len(body)
            cy = sum(y for _, y in body) / len(body)
            pin = min(mcu.pins, key=lambda p: abs(mcu.pin_point(p)[0] - at[0]) + abs(mcu.pin_point(p)[1] - at[1]))
            (px, py), (dx, dy) = mcu.pin_point(pin), mcu.pin_outward(pin)
            assert (cx - px) * dx + (cy - py) * dy > 0, f"{lib_id} at {at} points back at U1"


def _rotate(path, reference, angle):
    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    for symbol in sexpr.children(tree, "symbol"):
        if any(p[1] == "Reference" and p[2] == reference for p in sexpr.children(symbol, "property")):
            sexpr.child(symbol, "at")[3] = angle
    path.write_text(sexpr.dumps(tree), encoding="utf-8")
