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

        assert res["connected"] == [{"pin": "1", "pin_name": "Pin_1", "net": "LED", "via": "label"}]
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
        editor.place_symbol(index, CONN, {}, reference="J1")
        editor.save()
        boxes = _pin_boxes(SchematicEditor.load(base_sch), index)
        assert not _intersects(boxes["U1"], boxes["J1"])

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


def _counts(path):
    """How many of each kind of item the file holds, embedded library copies included."""
    from kicad_mcp import sexpr

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    kinds = ("symbol", "wire", "label", "global_label", "hierarchical_label", "junction", "no_connect")
    counts = {kind: len(list(sexpr.children(tree, kind))) for kind in kinds}
    counts["lib_symbols"] = len(list(sexpr.children(sexpr.child(tree, "lib_symbols") or [], "symbol")))
    return counts


def _connectivity(install, path):
    return {
        net.get("name"): frozenset((n.get("ref"), n.get("pin")) for n in net.findall("node"))
        for net in _netlist(install, path).findall("./nets/net")
    }


def _draw_wires(path, *points):
    """A hand-drawn wire through `points`, one segment per pair, as a user draws in KiCad."""
    editor = SchematicEditor.load(path)
    builder = editor._builder()
    for a, b in zip(points, points[1:]):
        builder.add_wire(a, b)
    editor._merge(builder)
    editor.save()


def _pin(editor, index, reference, number):
    sym, = editor.placements(index, reference)
    return sym.pin_point(next(p for p in sym.pins if p.number == number))


def _relabel(path, text, kind=None, alias=None):
    """Turn the label `text` into another `kind`, or put an `alias` label on its point."""
    import copy

    from kicad_mcp import sexpr
    from kicad_mcp.sexpr import Sym

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    label = next(n for n in sexpr.children(tree, "label") if n[1] == text)
    if kind:
        label[0] = Sym(kind)
        label.insert(2, [Sym("shape"), Sym("input")])
    if alias:
        twin = copy.deepcopy(label)
        twin[1] = alias
        sexpr.child(twin, "uuid")[1] = "00000000-0000-0000-0000-00000000a11a"
        tree.insert(tree.index(label) + 1, twin)
    path.write_text(sexpr.dumps(tree), encoding="utf-8")


def _labels(path, kind="label"):
    from kicad_mcp import sexpr

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    return sorted(str(n[1]) for n in sexpr.children(tree, kind))


class TestRemoveSymbols:
    def test_placing_then_removing_restores_the_sheet(self, install, index, base_sch):
        before, nets_before = _counts(base_sch), _connectivity(install, base_sch)
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "LED", "2": "NEW_NET"}, reference="J1")
        editor.save()

        editor = SchematicEditor.load(base_sch)
        res = editor.remove_symbols(index, ["J1"])
        editor.save()

        assert res["removed"] == ["J1"]
        assert sorted(res["cleaned_up"]["labels"]) == ["LED", "NEW_NET"]
        assert _counts(base_sch) == before, "its stubs, labels and library copy go with it"
        assert _connectivity(install, base_sch) == nets_before
        assert _labels(base_sch) == ["LED"], "U1's own LED label still serves U1"

    def test_a_decoupling_cap_takes_its_rail_symbols_with_it(self, install, index, base_sch):
        before = _counts(base_sch)
        cap = add_decoupling_capacitors(index, base_sch, rails=["power:+3V3"])["added"][0]["reference"]

        editor = SchematicEditor.load(base_sch)
        res = editor.remove_symbols(index, [cap])
        editor.save()

        assert len(res["cleaned_up"]["power_symbols"]) == 2
        assert _counts(base_sch) == before
        assert "Failed to load" not in _erc_text(install, base_sch)

    def test_removing_the_only_part_clears_everything_hung_off_it(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.remove_symbols(index, ["U1"])
        editor.save()

        counts = _counts(base_sch)
        assert counts == dict.fromkeys(counts, 0), "power symbols, flags, junctions, wires, labels"
        assert "Failed to load" not in _erc_text(install, base_sch)

    def test_a_hand_drawn_wire_to_the_part_goes_with_it(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {}, reference="J1", at=(38.1, 38.1))
        editor.place_symbol(index, CONN, {}, reference="J2", at=(63.5, 63.5))
        editor.save()
        before = _counts(base_sch)
        a, b = _pin(editor, index, "J1", "1"), _pin(editor, index, "J2", "1")
        _draw_wires(base_sch, a, (a[0] - 7.62, a[1]), (a[0] - 7.62, b[1]), b)

        editor = SchematicEditor.load(base_sch)
        res = editor.remove_symbols(index, ["J1"])
        editor.save()

        assert res["cleaned_up"]["wires"] == 3, "every segment, around both corners"
        assert _counts(base_sch) == {**before, "symbol": before["symbol"] - 1}
        assert "J2" in SchematicEditor.load(base_sch).references()

    def test_a_rail_symbol_still_wired_to_another_part_is_kept(self, install, index, base_sch):
        from kicad_mcp import sexpr

        editor = SchematicEditor.load(base_sch)
        gnd = next(s for s in sexpr.children(editor.tree, "symbol")
                   if sexpr.value(s, "lib_id") == "power:GND")
        gnd_at = tuple(float(v) for v in sexpr.values(gnd, "at")[:2])
        editor.place_symbol(index, CONN, {}, reference="J1", at=(38.1, 38.1))
        editor.save()
        pin = _pin(editor, index, "J1", "1")
        _draw_wires(base_sch, pin, (pin[0] - 7.62, pin[1]), (pin[0] - 7.62, gnd_at[1]), gnd_at)

        editor = SchematicEditor.load(base_sch)
        editor.remove_symbols(index, ["U1"])
        editor.save()

        assert ("J1", "1") in _connectivity(install, base_sch)["GND"]
        assert _counts(base_sch)["junction"] == 0, "no junction is left where only two wires meet"

    def test_wiring_that_joins_two_net_names_is_kept_and_reported(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "NET_A"}, reference="J1")
        editor.save()
        _relabel(base_sch, "NET_A", alias="NET_B")

        editor = SchematicEditor.load(base_sch)
        res = editor.remove_symbols(index, ["J1"])
        editor.save()

        assert {"NET_A", "NET_B"} <= set(_labels(base_sch)), "removing them would split two nets"
        assert any("NET_A" in w and "NET_B" in w for w in res["warnings"])

    def test_a_hierarchical_label_is_kept_and_reported(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "TO_PARENT"}, reference="J1")
        editor.save()
        _relabel(base_sch, "TO_PARENT", kind="hierarchical_label")

        editor = SchematicEditor.load(base_sch)
        res = editor.remove_symbols(index, ["J1"])
        editor.save()

        assert _labels(base_sch, "hierarchical_label") == ["TO_PARENT"], "the parent's sheet pin needs it"
        assert any("TO_PARENT" in w for w in res["warnings"])

    def test_unknown_reference_is_refused_and_nothing_removed(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.remove_symbols(index, ["U1", "J99"])
        assert "J99" in exc.value.message
        assert exc.value.remedy
        assert "U1" in editor.references()


def _add_label(path, text, at):
    """A local label dropped straight on `at`, as a user might in KiCad."""
    editor = SchematicEditor.load(path)
    builder = editor._builder()
    builder.add_label(text, at)
    editor._merge(builder)
    editor.save()


class TestDisconnectPins:
    """U1 is the ESP32-S3-MINI-1: pin 8 IO4 carries the LED label, pin 9 IO5 is free,
    pin 3 3V3 has the +3V3 rail and a PWR_FLAG, and its 24 GND pins share one point."""

    def _disconnect(self, index, path, reference, **select):
        editor = SchematicEditor.load(path)
        res = editor.disconnect_pins(index, reference, **select)
        editor.save()
        return res

    @pytest.mark.parametrize("kind", [None, "global_label"])
    def test_takes_the_label_and_its_stub_off_the_pin(self, install, index, base_sch, kind):
        if kind:
            _relabel(base_sch, "LED", kind=kind)
        before = _counts(base_sch)

        res = self._disconnect(index, base_sch, "U1", pin_numbers=["8"])

        assert res["disconnected"] == [{"pin": "8", "pin_name": "IO4"}]
        assert res["cleaned_up"]["labels"] == ["LED"]
        assert _counts(base_sch) == {**before, "wire": before["wire"] - 1, kind or "label": 0}
        assert "U1" in SchematicEditor.load(base_sch).references(), "the part stays"
        assert not any(("U1", "8") in nodes for net, nodes in _connectivity(install, base_sch).items()
                       if not net.startswith("unconnected-"))

    def test_frees_a_no_connected_pin_so_it_can_be_labelled(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.mark_pins_unused(index, "U1", pin_numbers=["9"])
        editor.save()

        res = self._disconnect(index, base_sch, "U1", pin_numbers=["9"])
        assert res["cleaned_up"]["no_connects"] == 1
        assert _counts(base_sch)["no_connect"] == 0

        editor = SchematicEditor.load(base_sch)
        labelled = editor.label_pins(index, "U1", {"IO5": "SDA"})
        editor.save()
        assert labelled["already_connected"] == []
        assert ("U1", "9") in _connectivity(install, base_sch)["/SDA"]

    def test_a_rail_serving_only_this_pin_goes_with_it(self, install, index, base_sch):
        res = self._disconnect(index, base_sch, "U1", name_contains="3V3")

        assert len(res["cleaned_up"]["power_symbols"]) == 2, "+3V3 and its PWR_FLAG"
        assert "+3V3" not in _connectivity(install, base_sch)
        assert "GND" in _connectivity(install, base_sch), "the other rail is untouched"
        assert "Failed to load" not in _erc_text(install, base_sch)

    def test_a_wire_to_another_part_is_cut_back_to_that_part(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {}, reference="J1", at=(38.1, 38.1))
        editor.save()
        before = _counts(base_sch)
        a, b = _pin(editor, index, "J1", "1"), _pin(editor, index, "U1", "9")
        _draw_wires(base_sch, a, (a[0] - 7.62, a[1]), (a[0] - 7.62, b[1]), b)

        res = self._disconnect(index, base_sch, "U1", pin_numbers=["9"])

        assert res["cleaned_up"]["wires"] == 3, "every segment, around both corners"
        assert _counts(base_sch) == before
        assert "J1" in SchematicEditor.load(base_sch).references()

    def test_a_wire_into_a_branch_is_cut_back_to_the_branch(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {}, reference="J1", at=(38.1, 38.1))
        editor.place_symbol(index, CONN, {}, reference="J2", at=(38.1, 152.4))
        editor.save()
        u1, j1, j2 = (_pin(editor, index, r, n) for r, n in (("U1", "9"), ("J1", "1"), ("J2", "1")))
        tee = (u1[0] - 10.16, u1[1])
        _draw_wires(base_sch, j1, (j1[0] - 7.62, j1[1]), (j1[0] - 7.62, u1[1]), tee, u1)
        _draw_wires(base_sch, tee, (tee[0], j2[1]), j2)
        editor = SchematicEditor.load(base_sch)
        builder = editor._builder()
        builder.add_junction(tee)
        editor._merge(builder)
        editor.save()

        res = self._disconnect(index, base_sch, "U1", pin_numbers=["9"])

        assert res["cleaned_up"]["wires"] == 1
        assert res["cleaned_up"]["junctions"] == 1, "two wires meeting need no junction"
        nets = _connectivity(install, base_sch)
        assert any({("J1", "1"), ("J2", "1")} == nodes for nodes in nets.values()), "J1 and J2 stay joined"

    def test_a_label_on_the_pin_that_carries_on_is_kept_and_reported(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {}, reference="J1", at=(38.1, 38.1))
        editor.place_symbol(index, CONN, {"1": "X"}, reference="J2")
        editor.save()
        a, b = _pin(editor, index, "J1", "1"), _pin(editor, index, "U1", "9")
        _draw_wires(base_sch, a, (a[0] - 7.62, a[1]), (a[0] - 7.62, b[1]), b)
        _add_label(base_sch, "X", b)
        before = _counts(base_sch)

        res = self._disconnect(index, base_sch, "U1", pin_numbers=["9"])

        assert res["disconnected"] == []
        assert [s["pin"] for s in res["still_connected"]] == ["9"]
        assert any("9" in w and "X" in w for w in res["warnings"])
        assert _counts(base_sch) == before, "J1 would lose the net X"
        assert {("J1", "1"), ("J2", "1")} <= _connectivity(install, base_sch)["/X"]

    def test_one_of_several_stacked_pins_cannot_leave_alone(self, index, base_sch):
        before = _counts(base_sch)
        res = self._disconnect(index, base_sch, "U1", pin_numbers=["1"])

        assert [s["pin"] for s in res["still_connected"]] == ["1"]
        assert any("42" in w for w in res["warnings"]), "names the pins it shares its point with"
        assert _counts(base_sch) == before

    def test_all_stacked_pins_leave_together(self, install, index, base_sch):
        res = self._disconnect(index, base_sch, "U1", name_contains="GND")

        assert len(res["disconnected"]) == 24
        assert len(res["cleaned_up"]["power_symbols"]) == 2, "GND and its PWR_FLAG"
        assert "GND" not in _connectivity(install, base_sch)

    def test_a_free_pin_is_reported_and_nothing_changes(self, index, base_sch):
        before = base_sch.read_text(encoding="utf-8")
        res = self._disconnect(index, base_sch, "U1", pin_numbers=["9"])
        assert res["already_free"] == [{"pin": "9", "pin_name": "IO5"}]
        assert res["disconnected"] == []
        assert base_sch.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("select", [{"pin_numbers": ["999"]}, {"name_contains": "NOPE"}, {}])
    def test_no_matching_pin_is_refused(self, index, base_sch, select):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.disconnect_pins(index, "U1", **select)
        assert exc.value.remedy

    def test_unknown_reference_is_refused(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError):
            editor.disconnect_pins(index, "J99", pin_numbers=["1"])


class TestRenameLabels:
    def _place(self, index, path, reference, assignments):
        editor = SchematicEditor.load(path)
        editor.place_symbol(index, CONN, assignments, reference=reference)
        editor.save()

    def _rename(self, path, renames):
        editor = SchematicEditor.load(path)
        res = editor.rename_labels(renames)
        editor.save()
        return res

    def test_renames_every_label_of_the_net(self, install, index, base_sch):
        self._place(index, base_sch, "J1", {"1": "LED"})

        res = self._rename(base_sch, {"LED": "STATUS"})

        assert res["renamed"] == [{"from": "LED", "to": "STATUS", "labels": 2}]
        nets = _connectivity(install, base_sch)
        assert "/LED" not in nets
        assert {r for r, _ in nets["/STATUS"]} == {"U1", "J1"}

    def test_shifts_a_numbered_series_in_one_call(self, install, index, base_sch):
        # The old and new names overlap: IN3 and IN4 are both renamed and renamed to.
        self._place(index, base_sch, "J1", {"1": "IN3", "2": "IN4", "3": "IN5"})
        self._place(index, base_sch, "J2", {"1": "IN6"})

        self._rename(base_sch, {"IN3": "IN1", "IN4": "IN2", "IN5": "IN3", "IN6": "IN4"})

        nets = _connectivity(install, base_sch)
        assert nets["/IN1"] == {("J1", "1")}
        assert nets["/IN2"] == {("J1", "2")}
        assert nets["/IN3"] == {("J1", "3")}
        assert nets["/IN4"] == {("J2", "1")}
        assert "/IN5" not in nets and "/IN6" not in nets

    def test_swaps_two_names(self, install, index, base_sch):
        self._place(index, base_sch, "J1", {"1": "A", "2": "B"})
        self._rename(base_sch, {"A": "B", "B": "A"})
        nets = _connectivity(install, base_sch)
        assert (nets["/A"], nets["/B"]) == ({("J1", "2")}, {("J1", "1")})

    @pytest.mark.parametrize("renames", [
        {"OTHER": "LED"},  # LED is U1's net and is not being renamed away
        {"OTHER": "X", "LED": "X"},  # two nets onto one name
        {"OTHER": "GND"},  # a power net on the sheet
    ])
    def test_a_rename_that_would_join_two_nets_is_refused(self, index, base_sch, renames):
        self._place(index, base_sch, "J1", {"1": "OTHER"})
        before = base_sch.read_text(encoding="utf-8")
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.rename_labels(renames)
        assert exc.value.remedy
        assert _labels(base_sch) == ["LED", "OTHER"]
        assert base_sch.read_text(encoding="utf-8") == before

    def test_unknown_label_is_refused_and_nothing_renamed(self, base_sch):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.rename_labels({"LED": "STATUS", "NOPE": "X"})
        assert "NOPE" in exc.value.message
        assert _labels(base_sch) == ["LED"]
        assert [str(n[1]) for n in _label_nodes(editor)] == ["LED"]

    def test_a_hierarchical_label_rename_warns_about_the_parent_pin(self, index, base_sch):
        self._place(index, base_sch, "J1", {"1": "TO_PARENT"})
        _relabel(base_sch, "TO_PARENT", kind="hierarchical_label")

        res = self._rename(base_sch, {"TO_PARENT": "UP"})

        assert _labels(base_sch, "hierarchical_label") == ["UP"]
        assert any("TO_PARENT" in w and "sheet pin" in w for w in res["warnings"])


TEXT_BOX = """(text_box "X6: 1x4 header to the relay board"
	(exclude_from_sim no)
	(at 50.8 160.02 0)
	(size 40.64 7.62)
	(margins 0.9525 0.9525 0.9525 0.9525)
	(stroke (width 0) (type solid))
	(fill (type none))
	(effects (font (size 1.27 1.27)) (justify left top))
	(uuid "00000000-0000-0000-0000-0000000b0c00")
)"""


def _add_notes(path, *texts, box=False):
    """Free-text notes at made-up spots, plus TEXT_BOX if `box`, as a user writes them."""
    from kicad_mcp import sexpr

    editor = SchematicEditor.load(path)
    builder = editor._builder()
    for i, text in enumerate(texts):
        builder.add_text(text, (25.4, 160.02 + 15.24 * i))
    editor._merge(builder)
    if box:
        editor.tree.insert(-1, sexpr.parse(TEXT_BOX))
    editor.save()


def _notes(path, kind=None):
    from kicad_mcp import sexpr

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    kinds = (kind,) if kind else ("text", "text_box")
    return [n for n in tree if isinstance(n, list) and n and getattr(n[0], "name", "") in kinds]


class TestEditNotes:
    NOTE = "A2.1  ESP32-S3-DevKitC-1\nNot used: RST; spare GPIO8, 9. Every analog input is on ADC1."

    def _edit(self, path, replacements):
        editor = SchematicEditor.load(path)
        res = editor.edit_notes(replacements)
        editor.save()
        return res

    def test_rewrites_a_fragment_and_keeps_the_rest_of_the_note(self, install, base_sch):
        from kicad_mcp import sexpr

        _add_notes(base_sch, self.NOTE, "Sheets: relay.kicad_sch")
        before = _notes(base_sch)

        res = self._edit(base_sch, {"spare GPIO8, 9.": "spare GPIO35-42. GPIO8/9 are I2C SDA/SCL."})

        after = _notes(base_sch)
        expected = self.NOTE.replace("spare GPIO8, 9.", "spare GPIO35-42. GPIO8/9 are I2C SDA/SCL.")
        assert after[0][1] == expected
        assert [n[2:] for n in after] == [n[2:] for n in before], "position, look and uuid stay"
        assert after[1] == before[1], "the other note is untouched"
        assert res["edited"] == [{"kind": "text", "position": {"x": 25.4, "y": 160.02},
                                  "replacements": 1, "text": expected}]
        assert "Failed to load" not in _erc_text(install, base_sch)
        assert "\\n" in base_sch.read_text(encoding="utf-8"), "line breaks stay escaped in the file"
        assert sexpr.parse(base_sch.read_text(encoding="utf-8"))

    def test_replacements_apply_to_the_text_as_it_was(self, base_sch):
        _add_notes(base_sch, "X6 left, X7 right")
        self._edit(base_sch, {"X6": "X7", "X7": "X6"})
        assert _notes(base_sch)[0][1] == "X7 left, X6 right"

    def test_a_text_box_is_a_note_too_and_warns_when_it_grows(self, install, base_sch):
        _add_notes(base_sch, box=True)
        res = self._edit(base_sch, {"1x4 header": "1x4 header (+5V_RLY, GND_E2, SDA_5V, SCL_5V)"})

        box, = _notes(base_sch, "text_box")
        assert box[1] == "X6: 1x4 header (+5V_RLY, GND_E2, SDA_5V, SCL_5V) to the relay board"
        assert res["edited"][0]["kind"] == "text_box"
        assert any("text box" in w and "50.8" in w for w in res["warnings"])
        assert "Failed to load" not in _erc_text(install, base_sch)

    def test_a_note_rewritten_to_nothing_is_removed(self, base_sch):
        _add_notes(base_sch, "obsolete", "keep me")
        res = self._edit(base_sch, {"obsolete": ""})
        assert [n[1] for n in _notes(base_sch)] == ["keep me"]
        assert res["removed"] == [{"kind": "text", "position": {"x": 25.4, "y": 160.02}, "text": "obsolete"}]

    @pytest.mark.parametrize("replacements,needle", [
        ({"spare GPIO7": "x"}, "spare GPIO7"),  # in no note
        ({"Sheets": "x"}, "2 times"),  # in two notes
        ({"used": "x", "1": "y"}, "'1' occurs 3 times"),  # three times in one note
        ({"spare GPIO8": "x", "GPIO8, 9": "y"}, "overlap"),
        ({"": "x"}, "empty"),
        ({"LED": "STATUS"}, "LED"),  # a label is not a note
    ])
    def test_a_bad_fragment_is_refused_and_nothing_changes(self, base_sch, replacements, needle):
        _add_notes(base_sch, self.NOTE, "Sheets: relay.kicad_sch", "Sheets again")
        before = base_sch.read_text(encoding="utf-8")
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.edit_notes(replacements)
        assert needle in exc.value.message
        assert exc.value.remedy
        assert base_sch.read_text(encoding="utf-8") == before

    def test_the_sheet_summary_lists_its_notes(self, base_sch):
        _add_notes(base_sch, self.NOTE, box=True)
        notes = Schematic.load(base_sch).summary()["notes"]
        assert [(n["kind"], n["text"]) for n in notes] == [
            ("text", self.NOTE), ("text_box", "X6: 1x4 header to the relay board"),
        ]


def _symbol_nodes(path, reference):
    from kicad_mcp import sexpr

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    return [s for s in sexpr.children(tree, "symbol")
            if any(p[1:3] == ["Reference", reference] for p in sexpr.children(s, "property"))]


def _field_at(path, reference, name, unit=1):
    from kicad_mcp import sexpr

    node = next(s for s in _symbol_nodes(path, reference) if sexpr.value(s, "unit", 1) == unit)
    prop = next(p for p in sexpr.children(node, "property") if p[1] == name)
    return tuple(float(v) for v in sexpr.values(prop, "at"))


def _edit_symbol(path, reference, fn):
    """Change a placed symbol's node in the file, as a user might in KiCad."""
    from kicad_mcp import sexpr

    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    for symbol in list(sexpr.children(tree, "symbol")):
        if any(p[1:3] == ["Reference", reference] for p in sexpr.children(symbol, "property")):
            fn(tree, symbol)
    path.write_text(sexpr.dumps(tree), encoding="utf-8")


class TestMoveSymbolFields:
    def _move(self, path, reference, fields, unit=None):
        editor = SchematicEditor.load(path)
        res = editor.move_fields(reference, fields, unit=unit)
        editor.save()
        return res

    def test_moves_a_field_to_absolute_coordinates(self, install, base_sch):
        res = self._move(base_sch, "U1", {"Reference": {"x": 120.65, "y": 60.96}})

        assert _field_at(base_sch, "U1", "Reference")[:2] == (120.65, 60.96)
        assert res["moved"][0]["to"] == {"x": 120.65, "y": 60.96}
        assert "Failed to load" not in _erc_text(install, base_sch)

    def test_nudges_a_field_by_an_offset(self, base_sch):
        x, y, _ = _field_at(base_sch, "U1", "Value")
        self._move(base_sch, "U1", {"Value": {"dy": 2.54}})
        assert _field_at(base_sch, "U1", "Value")[:2] == (x, round(y + 2.54, 4))

    def test_a_moved_field_is_no_longer_autoplaced(self, base_sch):
        # KiCad re-places fields of a symbol marked autoplaced, undoing the move.
        from kicad_mcp.sexpr import Sym

        _edit_symbol(base_sch, "U1", lambda _, s: s.append([Sym("fields_autoplaced"), Sym("yes")]))
        self._move(base_sch, "U1", {"Reference": {"dx": 2.54}})
        assert "fields_autoplaced" not in str(_symbol_nodes(base_sch, "U1")[0])

    @pytest.mark.parametrize("angle,orientation,stored", [
        (0, "vertical", 90), (0, "horizontal", 0), (90, "vertical", 0), (90, "horizontal", 90),
    ])
    def test_orientation_is_how_it_reads_whatever_the_symbol_rotation(
        self, index, base_sch, angle, orientation, stored
    ):
        # KiCad stores a field's angle relative to its symbol's rotation.
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {}, reference="J1")
        editor.save()
        _orient(base_sch, "J1", angle)

        self._move(base_sch, "J1", {"Reference": {"orientation": orientation}})

        assert _field_at(base_sch, "J1", "Reference")[2] == stored

    def test_absolute_position_on_a_multi_unit_part_needs_a_unit(self, base_sch):
        import copy

        from kicad_mcp import sexpr

        def add_unit_2(tree, symbol):
            twin = copy.deepcopy(symbol)
            sexpr.child(twin, "unit")[1] = 2
            sexpr.child(twin, "uuid")[1] = "00000000-0000-0000-0000-0000000000u2"
            tree.insert(tree.index(symbol) + 1, twin)

        _edit_symbol(base_sch, "U1", add_unit_2)
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.move_fields("U1", {"Reference": {"x": 100.0, "y": 50.0}})
        assert "unit" in exc.value.remedy

        self._move(base_sch, "U1", {"Reference": {"x": 100.0, "y": 50.0}}, unit=2)
        assert _field_at(base_sch, "U1", "Reference", unit=2)[:2] == (100.0, 50.0)
        assert _field_at(base_sch, "U1", "Reference", unit=1)[:2] != (100.0, 50.0)

    @pytest.mark.parametrize("reference,fields,needle", [
        ("U99", {"Reference": {"dx": 1}}, "U99"),
        ("U1", {"MPN": {"dx": 1}}, "MPN"),
        ("U1", {"Reference": {"x": 1, "dx": 1}}, "dx"),
        ("U1", {"Reference": {"left": 1}}, "left"),
        ("U1", {"Reference": {"orientation": "diagonal"}}, "diagonal"),
    ])
    def test_bad_requests_are_refused_and_nothing_moves(self, base_sch, reference, fields, needle):
        before = base_sch.read_text(encoding="utf-8")
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError) as exc:
            editor.move_fields(reference, fields)
        assert needle in exc.value.message
        editor.save()
        assert base_sch.read_text(encoding="utf-8") == before

    def test_a_field_moved_off_the_page_is_written_but_warned(self, base_sch):
        res = self._move(base_sch, "U1", {"Value": {"x": 1000.0, "y": 50.8}})
        assert _field_at(base_sch, "U1", "Value")[:2] == (1000.0, 50.8)
        assert res["warnings"]


def _label_nodes(editor):
    from kicad_mcp import sexpr

    return [n for kind in ("label", "global_label", "hierarchical_label")
            for n in sexpr.children(editor.tree, kind)]


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

        assert res["connected"] == [{"pin": "1", "pin_name": "Pin_1", "net": "LED", "via": "label"}]
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


class TestSetFields:
    PSU = "Converter_ACDC:HLK-PM01"
    PSU_FP = "Converter_ACDC:Converter_ACDC_Hi-Link_HLK-PMxx"

    @pytest.fixture
    def footprints(self, install):
        from kicad_mcp.footprints import FootprintIndex

        return FootprintIndex(install)

    def _comp(self, install, path, ref):
        root = _netlist(install, path)
        return next((c for c in root.findall("./components/comp") if c.get("ref") == ref), None)

    def test_sets_value_and_footprint_as_kicad_reads_them(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        res = editor.set_fields("U1", {"Value": "ESP32-S3", "Footprint": self.PSU_FP})
        editor.save()

        comp = self._comp(install, base_sch, "U1")
        assert comp.findtext("value") == "ESP32-S3"
        assert comp.findtext("footprint") == self.PSU_FP
        assert res["changed"]["Value"]["new"] == "ESP32-S3"
        assert res["changed"]["Value"]["old"] == MCU.split(":")[-1]

    def test_adds_custom_field_hidden(self, install, index, base_sch):
        from kicad_mcp import sexpr

        editor = SchematicEditor.load(base_sch)
        res = editor.set_fields("U1", {"MPN": "ESP32-S3-MINI-1-N8"})
        editor.save()

        assert res["added"] == ["MPN"]
        comp = self._comp(install, base_sch, "U1")
        fields = {f.get("name"): f.text for f in comp.findall("./fields/field")}
        assert fields.get("MPN") == "ESP32-S3-MINI-1-N8"

        tree = sexpr.parse(base_sch.read_text(encoding="utf-8"))
        prop = next(
            p for s in sexpr.children(tree, "symbol") for p in sexpr.children(s, "property")
            if p[1] == "MPN"
        )
        assert "(hide yes)" in sexpr.dumps(prop)

    def test_renames_reference_everywhere_kicad_looks(self, install, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "LED"}, reference="J1")
        editor.set_fields("J1", {"Reference": "J5"})
        editor.save()

        assert self._comp(install, base_sch, "J1") is None
        assert self._comp(install, base_sch, "J5") is not None
        text = base_sch.read_text(encoding="utf-8")
        assert '(reference "J1")' not in text
        assert ("J5", "1") in _nodes(_netlist(install, base_sch), "/LED")

    def test_rename_to_a_taken_reference_is_refused(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {}, reference="J1")
        with pytest.raises(ToolInputError):
            editor.set_fields("J1", {"Reference": "U1"})

    def test_unknown_reference_raises(self, base_sch):
        editor = SchematicEditor.load(base_sch)
        with pytest.raises(ToolInputError):
            editor.set_fields("U99", {"Value": "x"})

    def test_unresolvable_footprint_is_written_but_warned(self, footprints, base_sch):
        editor = SchematicEditor.load(base_sch)
        res = editor.set_fields("U1", {"Footprint": "PCM_Nope:Converter_ACDC_Hi-Link_HLK-PMxx"},
                                footprints=footprints)
        editor.save()
        assert _footprint_of(base_sch, "U1") == "PCM_Nope:Converter_ACDC_Hi-Link_HLK-PMxx"
        assert len(res["warnings"]) == 1
        assert self.PSU_FP in res["warnings"][0], "should name the same footprint under a real library"

    def test_resolvable_footprint_has_no_warning(self, footprints, base_sch):
        editor = SchematicEditor.load(base_sch)
        res = editor.set_fields("U1", {"Footprint": self.PSU_FP}, footprints=footprints)
        assert res["warnings"] == []


# --- page-aware placement -------------------------------------------------

A4 = (297.0, 210.0)
INSET = 12.0  # KiCad's default page layout: 10 mm margin + 2 mm border band
TITLE_BLOCK = (A4[0] - 10 - 110, A4[1] - 10 - 34, A4[0] - 10, A4[1] - 10)
PSU = "Converter_ACDC:HLK-PM01"


@pytest.fixture
def a4_sch(index, tmp_path):
    out = tmp_path / "a4.kicad_sch"
    generate_pinout_schematic(index, out, MCU, assignments={"IO4": "LED"}, paper="A4")
    return out


def _pin_boxes(editor, index):
    """Per-reference bounding box of connection points, straight from geometry.

    Deliberately not the placement code's own box maths: pins are the part of
    a symbol that must certainly be on the page and must never touch another
    symbol's.
    """
    boxes = {}
    for ref in editor.references():
        pts = [s.pin_point(p) for s in editor.placements(index, ref) for p in s.pins]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            boxes[ref] = (min(xs), min(ys), max(xs), max(ys))
    return boxes


def _intersects(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


class TestPagePlacement:
    def _fill(self, index, path, count, lib_id=PSU, prefix="PS"):
        refs = []
        for i in range(count):
            editor = SchematicEditor.load(path)
            ref = f"{prefix}{i + 1}"
            editor.place_symbol(index, lib_id, {}, reference=ref)
            editor.save()
            refs.append(ref)
        return refs

    def test_wraps_to_a_new_row_instead_of_leaving_the_page(self, index, a4_sch):
        # The old rule -- always right of everything -- put the 4th part here
        # past x=297 on A4.
        self._fill(index, a4_sch, 5)
        boxes = _pin_boxes(SchematicEditor.load(a4_sch), index)
        for ref, (x0, y0, x1, y1) in boxes.items():
            assert INSET <= x0 and x1 <= A4[0] - INSET, f"{ref} off the page horizontally"
            assert INSET <= y0 and y1 <= A4[1] - INSET, f"{ref} off the page vertically"

    def test_placed_symbols_never_touch(self, index, a4_sch):
        self._fill(index, a4_sch, 5)
        boxes = list(_pin_boxes(SchematicEditor.load(a4_sch), index).items())
        for i, (ra, a) in enumerate(boxes):
            for rb, b in boxes[i + 1:]:
                if ra.startswith("#") or rb.startswith("#"):
                    continue  # power symbols sit on their MCU's pin stubs by design
                assert not _intersects(a, b), f"{ra} overlaps {rb}"

    def test_keeps_clear_of_the_title_block(self, index, a4_sch):
        try:
            self._fill(index, a4_sch, 40)
        except ToolInputError:
            pass  # a full sheet is fine here; only what did get placed matters
        boxes = _pin_boxes(SchematicEditor.load(a4_sch), index)
        for ref, box in boxes.items():
            assert not _intersects(box, TITLE_BLOCK), f"{ref} sits on the title block"

    def test_full_sheet_refuses_with_remedy_and_writes_nothing(self, index, a4_sch):
        with pytest.raises(ToolInputError) as exc:
            self._fill(index, a4_sch, 40)
        assert exc.value.remedy
        placed = SchematicEditor.load(a4_sch).references()
        editor = SchematicEditor.load(a4_sch)
        with pytest.raises(ToolInputError):
            editor.place_symbol(index, PSU, {}, reference="PSX")
        assert "PSX" not in editor.references()
        assert SchematicEditor.load(a4_sch).references() == placed

    def test_explicit_position_off_the_page_is_placed_but_warned(self, index, a4_sch):
        editor = SchematicEditor.load(a4_sch)
        res = editor.place_symbol(index, CONN, {}, reference="J1", at=(401.32, 101.6))
        assert res["position"]["x"] == 401.32
        assert res["warnings"]

    def test_decoupling_capacitors_stay_on_the_page(self, index, a4_sch):
        # Caps added once the row is already full must wrap, not run off the edge.
        self._fill(index, a4_sch, 3)
        rails = ["power:+3V3", "power:+5V", "power:+1V8", "power:+12V"]
        res = add_decoupling_capacitors(index, a4_sch, rails=rails)
        assert res["count"] == len(rails)
        for ref, (x0, y0, x1, y1) in _pin_boxes(SchematicEditor.load(a4_sch), index).items():
            assert INSET <= x0 and x1 <= A4[0] - INSET, f"{ref} off the page"
            assert INSET <= y0 and y1 <= A4[1] - INSET, f"{ref} off the page"


class TestPageSize:
    @pytest.mark.parametrize("paper,expected", [
        ('(paper "A4")', (297.0, 210.0)),
        ('(paper "A3")', (420.0, 297.0)),
        ('(paper "A4" portrait)', (210.0, 297.0)),
        ('(paper "USLetter")', (279.4, 215.9)),
        ('(paper "User" 500 300)', (500.0, 300.0)),
    ])
    def test_reads_paper_sizes(self, tmp_path, paper, expected):
        from kicad_mcp import sexpr

        editor = SchematicEditor(tmp_path / "x.kicad_sch", sexpr.parse(f"(kicad_sch {paper})"))
        assert editor.page_size() == expected


# --- connection-aware placement --------------------------------------------


def _pin_outward(editor, index, ref, number):
    sym, = editor.placements(index, ref)
    pin = next(p for p in sym.pins if p.number == number)
    return sym.pin_outward(pin), sym.pin_point(pin)


class TestConnectionAwarePlacement:
    """IO4 sits on U1's left edge; IO21 on its right (ESP32-S3-MINI-1)."""

    def test_goes_on_the_side_its_partners_face(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        res = editor.place_symbol(index, CONN, {"1": "LED"}, reference="J1")
        assert res["position"]["x"] < editor.placement("U1")[1]

    def test_mirrors_so_its_connected_pin_faces_the_partner(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "LED"}, reference="J1")
        (dx, _), _ = _pin_outward(editor, index, "J1", "1")
        assert dx > 0, "J1 sits left of U1, so its pin must face right, toward U1"

    def test_not_mirrored_when_already_facing_the_partner(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.label_pins(index, "U1", {"IO21": "RIGHTNET"})
        res = editor.place_symbol(index, CONN, {"1": "RIGHTNET"}, reference="J1")
        assert res["position"]["x"] > editor.placement("U1")[1]
        assert res["mirrored"] is False
        (dx, _), _ = _pin_outward(editor, index, "J1", "1")
        assert dx < 0

    def test_lines_up_with_the_partner_pins(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        editor.place_symbol(index, CONN, {"1": "LED"}, reference="J1")
        _, (_, j1_y) = _pin_outward(editor, index, "J1", "1")
        _, (_, u1_y) = _pin_outward(editor, index, "U1", "8")  # IO4
        assert abs(j1_y - u1_y) <= 5.08

    def test_unknown_nets_place_exactly_as_before(self, index, base_sch):
        editor = SchematicEditor.load(base_sch)
        longest = len("BRAND_NEW")
        expected = editor.free_spot(index, CONN, inflate=5.08 + longest * 1.27)
        res = editor.place_symbol(index, CONN, {"1": "BRAND_NEW"}, reference="J1")
        assert (res["position"]["x"], res["position"]["y"]) == expected
        assert res["mirrored"] is False

    def test_page_rules_still_hold(self, index, a4_sch):
        editor = SchematicEditor.load(a4_sch)
        for i in range(6):
            editor.place_symbol(index, CONN, {"1": "LED"}, reference=f"J{i + 1}")
        editor.save()
        boxes = _pin_boxes(SchematicEditor.load(a4_sch), index)
        for ref, (x0, y0, x1, y1) in boxes.items():
            assert INSET <= x0 and x1 <= A4[0] - INSET, f"{ref} off the page"
            assert INSET <= y0 and y1 <= A4[1] - INSET, f"{ref} off the page"
            assert not _intersects((x0, y0, x1, y1), TITLE_BLOCK), f"{ref} on the title block"
        refs = [r for r in boxes if not r.startswith("#")]
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                assert not _intersects(boxes[a], boxes[b]), f"{a} overlaps {b}"
