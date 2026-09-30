"""Editing schematics that already exist.

These exercise `SchematicEditor` end to end: build a base file with
`generate_pinout_schematic`, then apply edits on top and check the result both
through our own reader (structure) and through `kicad-cli` (does KiCad itself
accept it -- our parser agreeing with itself proves nothing).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from kicad_mcp import cli as kcli
from kicad_mcp.discovery import find_install
from kicad_mcp.editor import SchematicEditor, add_decoupling_capacitors
from kicad_mcp.errors import ToolInputError
from kicad_mcp.generator import generate_pinout_schematic
from kicad_mcp.schematic import Schematic
from kicad_mcp.symbols import SymbolIndex

pytestmark = pytest.mark.requires_kicad

MCU = "RF_Module:ESP32-S3-MINI-1"
CONN = "Connector_Generic:Conn_01x03"


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


@pytest.fixture(scope="module")
def index(install):
    return SymbolIndex(install)


@pytest.fixture
def base_sch(index, tmp_path):
    out = tmp_path / "base.kicad_sch"
    generate_pinout_schematic(index, out, MCU, assignments={"IO4": "LED"})
    return out


def _netlist(install, path):
    result = kcli.export_netlist(install, path)
    return ET.parse(result["output_file"]).getroot()


def _erc_text(install, path):
    result = kcli.run_erc(install, path)
    return result["stdout"] + result.get("report", "")


class TestMarkPinsUnused:
    def test_matches_by_name_substring(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        marked = editor.mark_pins_unused(index, "U1", name_contains="GND")
        editor.save()
        assert marked
        assert all("GND" in m["name"] for m in marked)

    def test_matches_by_exact_pin_number(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        marked = editor.mark_pins_unused(index, "U1", pin_numbers=["2"])
        assert len(marked) == 1
        assert marked[0]["number"] == "2"

    def test_no_match_raises_with_remedy(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.mark_pins_unused(index, "U1", name_contains="NOT_A_REAL_PIN_SUBSTRING")
        assert exc.value.remedy

    def test_unknown_reference_raises(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError):
            editor.mark_pins_unused(index, "U99", pin_numbers=["1"])

    def test_annotation_adds_visible_text(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.mark_pins_unused(index, "U1", pin_numbers=["2"], annotate="/RES")
        editor.save()
        sch = Schematic.load(base_sch)
        # add_text writes a `text` node; Schematic doesn't parse those into a
        # dedicated accessor, so check the raw tree for the literal string.
        assert "/RES" in base_sch.read_text(encoding="utf-8")

    def test_no_connect_silences_erc(self, install, index, base_sch):
        # Pick a pin ERC actually flags -- GND-named pins on this symbol share
        # a coordinate with each other and are never reported unconnected, so
        # they can't demonstrate that no-connect silences anything.
        before = _erc_text(install, base_sch)
        pin_num = next(
            p.number for p in index.pins(MCU)
            if f"Pin {p.number} [{p.name}" in before
        )
        needle = next(line for line in before.splitlines() if f"Pin {pin_num} [" in line)

        editor = SchematicEditor.load(base_sch)
        editor.mark_pins_unused(index, "U1", pin_numbers=[pin_num])
        editor.save()

        after = _erc_text(install, base_sch)
        assert needle not in after


class TestPlaceSymbol:
    def test_wires_to_existing_net_by_matching_label(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        res = editor.place_symbol(index, CONN, {"1": "LED"}, reference="J1", value="HDR")
        editor.save()

        assert res["connected"] == [{"pin": "1", "pin_name": "Pin_1", "net": "LED"}]
        assert res["unmatched_assignments"] == []

        root = _netlist(install, base_sch)
        led_net = next(n for n in root.findall("./nets/net") if n.get("name") == "/LED")
        refs = {node.get("ref") for node in led_net.findall("node")}
        assert refs == {"U1", "J1"}, "new symbol should join the SAME net as the original label"

    def test_unmatched_assignment_reported_not_dropped(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        res = editor.place_symbol(index, CONN, {"1": "LED", "NOPE": "X"}, reference="J1")
        assert res["unmatched_assignments"] == ["NOPE"]
        assert len(res["connected"]) == 1

    def test_does_not_overlap_existing_symbols(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        u1_x = editor.placement("U1")[1]
        res = editor.place_symbol(index, CONN, {}, reference="J1")
        editor.save()
        gap = res["position"]["x"] - u1_x
        assert gap >= editor.half_width(index, MCU) + editor.half_width(index, CONN)

    def test_preserves_existing_content(self, index, base_sch):
        before = Schematic.load(base_sch).summary()["counts"]
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "LED"}, reference="J1")
        editor.save()
        after = Schematic.load(base_sch).summary()["counts"]
        assert after["symbols"] == before["symbols"] + 1
        assert after["wires"] >= before["wires"]

    def test_result_reparses_and_kicad_accepts_it(self, install, index, base_sch):
        from kicad_mcp import sexpr

        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "LED"}, reference="J1")
        editor.save()

        text = base_sch.read_text(encoding="utf-8")
        assert sexpr.dumps(sexpr.parse(text)) == text
        erc = _erc_text(install, base_sch)
        assert "Failed to load" not in erc


class TestSwapPowerSymbol:
    def test_rejects_mismatched_pin_geometry(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        # Device:C is not a power symbol and its pin sits nowhere near a power
        # symbol's (0,0) -- this must be rejected, not silently break the wire.
        with pytest.raises(ToolInputError):
            editor.swap_power_symbol(index, "power:GND", "Device:C")

    def test_swaps_identically_placed_symbol(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        changed = editor.swap_power_symbol(index, "power:GND", "power:GNDPWR")
        editor.save()
        assert changed >= 1
        root = _netlist(install, base_sch)
        names = {n.get("name") for n in root.findall("./nets/net")}
        assert "GND" not in names or any("GNDPWR" in n for n in names)


class TestAddDecouplingCapacitors:
    def test_adds_one_capacitor_per_rail_tied_to_ground(self, install, index, base_sch):
        res = add_decoupling_capacitors(index, base_sch, rails=["power:+3V3"])
        assert res["count"] == 1

        root = _netlist(install, base_sch)
        gnd = next(n for n in root.findall("./nets/net") if n.get("name") == "GND")
        refs = {node.get("ref") for node in gnd.findall("node")}
        assert res["added"][0]["reference"] in refs

    def test_multiple_rails_do_not_overlap(self, index, base_sch):
        res = add_decoupling_capacitors(index, base_sch, rails=["power:+3V3", "power:+5V"])
        assert res["count"] == 2
        xs = [c["position"]["x"] for c in res["added"]]
        assert len(set(xs)) == 2


def _footprint_of(path, reference):
    from kicad_mcp import sexpr

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    for symbol in sexpr.children(tree, "symbol"):
        props = {p[1]: p[2] for p in sexpr.children(symbol, "property") if len(p) >= 3}
        if props.get("Reference") == reference:
            return props.get("Footprint")
    raise AssertionError(f"{reference} not in {path}")


def _orient(path, reference, angle=0, mirror=None):
    """Rotate/mirror a placed symbol in the file, as a user might in KiCad."""
    from kicad_mcp import sexpr
    from kicad_mcp.sexpr import Sym

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    for symbol in sexpr.children(tree, "symbol"):
        props = {p[1]: p[2] for p in sexpr.children(symbol, "property") if len(p) >= 3}
        if props.get("Reference") != reference:
            continue
        at = sexpr.child(symbol, "at")
        at[3] = angle
        if mirror:
            symbol.insert(symbol.index(at) + 1, [Sym("mirror"), Sym(mirror)])
    path.write_text(sexpr.dumps(tree), encoding="utf-8")


def _nodes(root, net_name):
    net = next((n for n in root.findall("./nets/net") if n.get("name") == net_name), None)
    if net is None:
        return set()
    return {(node.get("ref"), node.get("pin")) for node in net.findall("node")}


class TestLibraryDefaultFootprint:
    PSU = "Converter_ACDC:HLK-PM01"
    PSU_FP = "Converter_ACDC:Converter_ACDC_Hi-Link_HLK-PMxx"

    def test_placed_symbol_inherits_library_footprint(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        res = editor.place_symbol(index, self.PSU, {}, reference="PS1")
        editor.save()
        assert _footprint_of(base_sch, "PS1") == self.PSU_FP
        assert res["footprint"] == self.PSU_FP

    def test_explicit_footprint_overrides_library_default(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, self.PSU, {}, reference="PS1", footprint="Lib:Other")
        editor.save()
        assert _footprint_of(base_sch, "PS1") == "Lib:Other"

    def test_generated_mcu_inherits_library_footprint(self, index, base_sch):
        expected = next(
            p[2] for p in index.definition(MCU) if isinstance(p, list) and p[1:2] == ["Footprint"]
        )
        assert expected, "fixture assumption: the MCU symbol ships a default footprint"
        assert _footprint_of(base_sch, "U1") == expected

    def test_netlist_carries_the_footprint(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, self.PSU, {}, reference="PS1")
        editor.save()
        root = _netlist(install, base_sch)
        comp = next(c for c in root.findall("./components/comp") if c.get("ref") == "PS1")
        assert comp.findtext("footprint") == self.PSU_FP


class TestLabelPins:
    def _place_conn(self, index, path):
        editor = SchematicEditor.load(path)
        editor.place_symbol(index, CONN, {}, reference="J1")
        editor.save()

    def test_joins_existing_net(self, install, index, base_sch):
        self._place_conn(index, base_sch)
        editor = SchematicEditor.load(base_sch)
        res = editor.label_pins(index, "J1", {"1": "LED"})
        editor.save()

        assert res["connected"] == [{"pin": "1", "pin_name": "Pin_1", "net": "LED"}]
        refs = {ref for ref, _ in _nodes(_netlist(install, base_sch), "/LED")}
        assert refs == {"U1", "J1"}

    @pytest.mark.parametrize("angle,mirror", [
        (0, None), (90, None), (180, None), (270, None),
        (0, "x"), (0, "y"), (90, "x"), (90, "y"),
    ])
    def test_lands_on_pins_of_rotated_and_mirrored_symbols(
        self, install, index, base_sch, angle, mirror
    ):
        self._place_conn(index, base_sch)
        _orient(base_sch, "J1", angle, mirror)

        editor = SchematicEditor.load(base_sch)
        editor.label_pins(index, "J1", {"1": "NA", "2": "NB", "3": "NC"})
        editor.save()

        root = _netlist(install, base_sch)
        for pin, net in (("1", "/NA"), ("2", "/NB"), ("3", "/NC")):
            assert ("J1", pin) in _nodes(root, net), f"label {net} missed J1 pin {pin}"

    def test_skips_pin_that_is_already_connected(self, install, index, base_sch):
        # U1's IO4 already carries the LED label from generation. A second label
        # would silently short LED to the new net, so it must be refused.
        editor = SchematicEditor.load(base_sch)
        res = editor.label_pins(index, "U1", {"IO4": "OTHER"})
        editor.save()

        assert res["connected"] == []
        assert [s["pin_name"] for s in res["already_connected"]] == ["IO4"]
        root = _netlist(install, base_sch)
        assert _nodes(root, "/OTHER") == set()

    def test_unmatched_assignment_reported(self, index, base_sch):
        self._place_conn(index, base_sch)
        editor = SchematicEditor.load(base_sch)
        res = editor.label_pins(index, "J1", {"1": "A", "NOPE": "B"})
        assert res["unmatched_assignments"] == ["NOPE"]
        assert len(res["connected"]) == 1

    def test_unknown_reference_raises(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError):
            editor.label_pins(index, "J99", {"1": "A"})
