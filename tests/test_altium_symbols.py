"""Altium parts rebuilt as KiCad symbols from the graphics placed on the sheet.

A .SchDoc carries every placed part's pins and drawing, already rotated and
mirrored into sheet coordinates. Baking that geometry into the library symbol
(relative to the part's anchor) puts every pin exactly where Altium drew it.
"""

from __future__ import annotations

import math

import pytest

from altium_fixtures import altium_fixture
from kicad_mcp import sexpr
from kicad_mcp.altium import model, symbols
from kicad_mcp.altium.model import Pin


def _pin(orientation, x=10.0, y=20.0, length=2.54):
    return Pin(name="A", number="1", electrical=4, orientation=orientation, x=x, y=y,
               length=length, hidden=False, show_name=True, show_number=True, part=1, mode=0,
               rec=None)


@pytest.mark.parametrize("orientation,end,angle", [
    (0, (12.54, 20.0), 180),   # points right: hot end to the right, body to the left
    (1, (10.0, 22.54), 270),   # points up (Altium Y up)
    (2, (7.46, 20.0), 0),
    (3, (10.0, 17.46), 90),
])
def test_pin_endpoint_and_angle(orientation, end, angle):
    p = _pin(orientation)
    assert symbols.pin_endpoint(p) == pytest.approx(end)
    assert symbols.pin_angle(p) == angle


@pytest.fixture(scope="module")
def power():
    return model.load_sheet(altium_fixture("eDP/power.SchDoc"))


@pytest.fixture(scope="module")
def core():
    return model.load_sheet(altium_fixture("eDP/it6251_core.SchDoc"))


def test_identical_parts_share_symbol(power):
    caps = [p for p in power.parts if p.libref == "Cap Semi" and p.orientation == 3]
    assert len(caps) >= 2
    assert symbols.symbol_key([caps[0]]) == symbols.symbol_key([caps[1]])


def test_different_rotation_differs(power):
    caps = [p for p in power.parts if p.libref == "Cap Semi"]
    keys = {symbols.symbol_key([c]) for c in caps}
    assert len({c.orientation for c in caps}) == 1 or len(keys) > 1


def _touches(point, sheet):
    x, y = point
    eps = 1e-6

    def on_segment(a, b):
        (ax, ay), (bx, by) = a, b
        if abs((bx - ax) * (y - ay) - (by - ay) * (x - ax)) > eps:
            return False
        return min(ax, bx) - eps <= x <= max(ax, bx) + eps and min(ay, by) - eps <= y <= max(ay, by) + eps

    for wire in sheet.wires:
        if any(on_segment(a, b) for a, b in zip(wire, wire[1:])):
            return True
    spots = list(sheet.junctions)
    spots += [model.location(r) for r in sheet.power_ports + sheet.net_labels + sheet.ports]
    return any(math.dist(point, s) < 1e-6 for s in spots)


def test_rotated_component_pins_hit_wire_ends(power, core):
    for sheet in (power, core):
        pins = [p for part in sheet.parts for p in part.pins]
        touching = sum(1 for p in pins if _touches(symbols.pin_endpoint(p), sheet))
        assert touching / len(pins) >= 0.9, (sheet.stem, touching, len(pins))


def test_multi_unit_symbol(core):
    u10 = sorted([p for p in core.parts if p.designator == "U10C"], key=lambda p: p.part_id)
    node = symbols.lib_symbol("IT6251", u10, core.fonts)
    names = [n[1] for n in sexpr.children(node, "symbol")]
    assert node[1] == "altium:IT6251"
    assert {"IT6251_1_1", "IT6251_2_1"} <= set(names)
    pins = list(sexpr.find_all(node, "pin"))
    assert len(pins) == sum(len(p.pins) for p in u10)


def test_lib_symbol_round_trips_through_sexpr(power):
    part = power.parts[0]
    text = sexpr.dumps(symbols.lib_symbol("X", [part], power.fonts))
    assert sexpr.parse(text)[0] == sexpr.sym("symbol")


def test_placed_symbol_fields_and_instances(power):
    part = next(p for p in power.parts if p.designator == "C13P")
    flip = symbols.Flip(210.0)
    node = symbols.placed_symbol("altium:Cap_Semi", part, 1, "u-1",
                                 [("eDP", "/root/sheet", "C13P")], footprint="Lib:CAP",
                                 fonts=power.fonts, flip=flip)
    props = {p[1]: p[2] for p in sexpr.children(node, "property")}
    assert props["Reference"] == "C13P"
    assert props["Footprint"] == "Lib:CAP"
    assert props["Value"] == part.value
    assert "Altium_UniqueId" not in props
    at = sexpr.child(node, "at")
    assert at[1:3] == [pytest.approx(part.x), pytest.approx(210.0 // 1.27 * 1.27 - part.y)]
    inst = sexpr.child(sexpr.child(sexpr.child(node, "instances"), "project"), "path")
    assert inst[1] == "/root/sheet" and sexpr.value(inst, "reference") == "C13P"


def test_altium_footprint_field_only_without_board(power):
    part = next(p for p in power.parts if p.designator == "C13P")
    flip = symbols.Flip(210.0)

    def fields(fp):
        node = symbols.placed_symbol("altium:Cap_Semi", part, 1, "u", [("p", "/r", "C13P")],
                                     footprint=fp, fonts=power.fonts, flip=flip)
        return {p[1] for p in sexpr.children(node, "property")}

    assert "Altium_Footprint" not in fields("Lib:CAP")
    assert "Altium_Footprint" in fields("")


def test_unresolved_comment_takes_board_value(tmp_path):
    from kicad_mcp.altium.model import Param
    sheet = model.load_sheet(altium_fixture("eDP/power.SchDoc"))
    part = next(p for p in sheet.parts if p.designator == "C13P")
    part.params["Comment"] = Param("Comment", "=Missing", 0, 0, False, 1, 0, 0, None)

    def value(board_value):
        node = symbols.placed_symbol("altium:X", part, 1, "u", [("p", "/r", "C13P")], footprint="",
                                     fonts=sheet.fonts, flip=symbols.Flip(210), value=board_value)
        return {p[1]: p[2] for p in sexpr.children(node, "property")}["Value"]

    assert value("") == ""
    assert value(None) == part.libref
