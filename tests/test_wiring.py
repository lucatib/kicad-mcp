"""route_nets: labels become wires, and KiCad sees exactly the same circuit.

Every routing test compares kicad-cli's netlist before and after -- the
router agreeing with its own geometry proves nothing, while an unchanged
netlist is the definition of "did not break the sheet".
"""

from __future__ import annotations

import pytest

from kicad_mcp import cli as kcli
from kicad_mcp import sexpr, wiring
from kicad_mcp.discovery import find_install
from kicad_mcp.editor import SchematicEditor
from kicad_mcp.generator import generate_pinout_schematic
from kicad_mcp.sexpr import Sym
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
def sch(index, tmp_path):
    out = tmp_path / "w.kicad_sch"
    generate_pinout_schematic(index, out, MCU, assignments={"IO4": "LED"})
    return out


def _netlist(install, path):
    return kcli.parse_kicadxml_netlist(kcli.export_netlist(install, path)["output_file"])


def _connectivity(install, path):
    return {
        n["name"]: frozenset((x["reference"], x["pin"]) for x in n["nodes"])
        for n in _netlist(install, path)["nets"]
    }


def _place(index, path, lib_id, assignments, reference):
    editor = SchematicEditor.load(path)
    editor.place_symbol(index, lib_id, assignments, reference=reference)
    editor.save()


def _label(index, path, reference, assignments):
    editor = SchematicEditor.load(path)
    editor.label_pins(index, reference, assignments)
    editor.save()


def _route(install, index, path, **kwargs):
    editor = SchematicEditor.load(path)
    result = wiring.route_nets(editor, index, _netlist(install, path), **kwargs)
    editor.save()
    return result


def _labels(path, text):
    tree = sexpr.parse(path.read_text(encoding="utf-8"))
    return [n for n in sexpr.children(tree, "label") if n[1] == text]


class TestRouting:
    def test_two_pin_net_becomes_wires_and_keeps_its_name(self, install, index, sch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        before = _connectivity(install, sch)

        res = _route(install, index, sch)

        assert [r["net"] for r in res["routed"]] == ["LED"]
        assert _connectivity(install, sch) == before
        assert len(_labels(sch, "LED")) == 1, "exactly one label keeps the net's name"

    def test_four_pin_net_is_one_tree_with_a_junction(self, install, index, sch):
        for ref in ("J1", "J2", "J3"):
            _place(index, sch, CONN, {"1": "SIG"}, ref)
        _label(index, sch, "U1", {"IO5": "SIG"})
        before = _connectivity(install, sch)

        res = _route(install, index, sch, nets=["SIG"])

        assert _connectivity(install, sch) == before
        sig = next(r for r in res["routed"] if r["net"] == "SIG")
        assert sig["pins"] == 4
        assert sig["junctions"] >= 1

    def test_adjacent_pins_on_different_nets_stay_separate(self, install, index, sch):
        # IO5 and IO6 are 2.54 mm apart on the MCU edge.
        _label(index, sch, "U1", {"IO5": "NA", "IO6": "NB"})
        _place(index, sch, CONN, {"1": "NA", "2": "NB"}, "J1")
        before = _connectivity(install, sch)

        res = _route(install, index, sch)

        assert {"NA", "NB"} <= {r["net"] for r in res["routed"]}
        assert _connectivity(install, sch) == before

    def test_rotated_and_mirrored_symbol(self, install, index, sch):
        _place(index, sch, CONN, {}, "J1")
        tree = sexpr.parse(sch.read_text(encoding="utf-8"))
        j1 = next(s for s in sexpr.children(tree, "symbol")
                  if any(p[1:3] == ["Reference", "J1"] for p in sexpr.children(s, "property")))
        at = sexpr.child(j1, "at")
        at[3] = 90
        j1.insert(j1.index(at) + 1, [Sym("mirror"), Sym("y")])
        sch.write_text(sexpr.dumps(tree), encoding="utf-8")
        _label(index, sch, "J1", {"1": "ROT"})
        _label(index, sch, "U1", {"IO5": "ROT"})
        before = _connectivity(install, sch)

        res = _route(install, index, sch)

        assert "ROT" in {r["net"] for r in res["routed"]}
        assert _connectivity(install, sch) == before

    def test_labels_sitting_directly_on_pins_are_accepted(self, install, index, sch):
        # How a previous session wired a sheet by hand: no stub wire at all.
        _place(index, sch, CONN, {}, "J1")
        editor = SchematicEditor.load(sch)
        builder = editor._builder()
        j1, = editor.placements(index, "J1")
        builder.add_label("DIRECT", j1.pin_point(next(p for p in j1.pins if p.number == "1")))
        editor._merge(builder)
        editor.save()
        _label(index, sch, "U1", {"IO5": "DIRECT"})
        before = _connectivity(install, sch)

        res = _route(install, index, sch)

        assert "DIRECT" in {r["net"] for r in res["routed"]}
        assert _connectivity(install, sch) == before


class TestLeftAlone:
    def test_power_nets_are_skipped(self, install, index, sch):
        # A local GND label merges into the power net: KiCad names it GND.
        _place(index, sch, CONN, {"1": "GND"}, "J1")
        before = sch.read_text(encoding="utf-8")

        res = _route(install, index, sch, nets=["GND"])

        assert res["routed"] == []
        assert any(s["net"] == "GND" for s in res["skipped"])
        assert sch.read_text(encoding="utf-8") == before

    def test_net_with_hand_drawn_wiring_is_skipped(self, install, index, sch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        editor = SchematicEditor.load(sch)
        label = next(n for n in sexpr.children(editor.tree, "label") if n[1] == "LED")
        x, y = float(label[2][1]), float(label[2][2])
        builder = editor._builder()
        builder.add_wire((x, y), (x, y + 5.08))  # the user extended it by hand
        editor._merge(builder)
        editor.save()

        res = _route(install, index, sch)

        assert all(r["net"] != "LED" for r in res["routed"])
        assert any(s["net"] == "LED" and "wiring" in s["reason"] for s in res["skipped"])

    def test_unreachable_net_is_left_exactly_as_it_was(self, install, index, sch, monkeypatch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        before = sch.read_text(encoding="utf-8")
        monkeypatch.setattr(wiring, "route_tree", lambda *a, **k: None)

        res = _route(install, index, sch)

        assert [u["net"] for u in res["unrouted"]] == ["LED"]
        assert sch.read_text(encoding="utf-8") == before

    def test_max_length_keeps_long_nets_labelled(self, install, index, sch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        before = sch.read_text(encoding="utf-8")

        res = _route(install, index, sch, max_length=1.0)

        assert res["routed"] == []
        assert "max_length" in res["unrouted"][0]["reason"]
        assert sch.read_text(encoding="utf-8") == before

    def test_nets_filter_limits_what_is_routed(self, install, index, sch):
        _place(index, sch, CONN, {"1": "LED", "2": "OTHER"}, "J1")
        _label(index, sch, "U1", {"IO5": "OTHER"})

        res = _route(install, index, sch, nets=["OTHER"])

        assert [r["net"] for r in res["routed"]] == ["OTHER"]
        assert len(_labels(sch, "LED")) == 2


class TestVerifiedTool:
    """The MCP tool: route on a copy, prove it with kicad-cli, then write."""

    def _tool(self, sch, **kwargs):
        from kicad_mcp import server

        return server.route_nets(str(sch), **kwargs)

    def test_routes_verifies_and_writes(self, install, index, sch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        before = _connectivity(install, sch)

        res = self._tool(sch)

        assert "error" not in res, res
        assert res["written"] is True
        assert res["verification"].startswith("passed")
        assert res["erc_violations"]["after"] <= res["erc_violations"]["before"]
        assert _connectivity(install, sch) == before
        assert len(_labels(sch, "LED")) == 1

    def test_dry_run_verifies_but_never_writes(self, index, sch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        before = sch.read_bytes()

        res = self._tool(sch, dry_run=True)

        assert res["written"] is False
        assert res["verification"].startswith("passed")
        assert sch.read_bytes() == before

    def test_changed_connectivity_is_refused_and_file_untouched(self, index, sch, monkeypatch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        before = sch.read_bytes()
        real = wiring.route_nets

        def shorting(editor, *a, **k):
            # Route for real, then "misplace" the kept label onto GND's name:
            # exactly the kind of mistake verification exists to catch.
            result = real(editor, *a, **k)
            for label in sexpr.children(editor.tree, "label"):
                if label[1] == "LED":
                    label[1] = "GND"
            return result

        monkeypatch.setattr(wiring, "route_nets", shorting)
        res = self._tool(sch)

        assert res["written"] is False
        assert "connectivity" in res["verification"]
        assert sch.read_bytes() == before

    def test_more_erc_violations_is_refused_and_file_untouched(self, index, sch, monkeypatch):
        _place(index, sch, CONN, {"1": "LED"}, "J1")
        before = sch.read_bytes()
        real = wiring.route_nets

        def dangling(editor, *a, **k):
            result = real(editor, *a, **k)
            builder = editor._builder()
            builder.add_wire((25.4, 25.4), (30.48, 25.4))  # connects nothing: ERC warns
            editor._merge(builder)
            return result

        monkeypatch.setattr(wiring, "route_nets", dangling)
        res = self._tool(sch)

        assert res["written"] is False
        assert "ERC" in res["verification"]
        assert sch.read_bytes() == before

    def test_nothing_to_route_leaves_file_untouched(self, index, sch):
        before = sch.read_bytes()
        res = self._tool(sch)
        assert res["written"] is False
        assert res["routed"] == []
        assert sch.read_bytes() == before
