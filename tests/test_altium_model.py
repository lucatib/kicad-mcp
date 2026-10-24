"""Altium sheets as typed objects: parts with their pins, wiring, sheet symbols.

Records point at their owner by index; the model resolves that tree once so
the writers see a part with its pins and graphics, a sheet symbol with its
entries, and the loose wiring and notes of the sheet.
"""

from __future__ import annotations

import pytest

from altium_fixtures import altium_fixture
from kicad_mcp.altium import model


@pytest.fixture(scope="module")
def power():
    return model.load_sheet(altium_fixture("eDP/power.SchDoc"))


@pytest.fixture(scope="module")
def core():
    return model.load_sheet(altium_fixture("eDP/it6251_core.SchDoc"))


@pytest.fixture(scope="module")
def sampler_root():
    return model.load_sheet(altium_fixture("sampler/Sampler.schdoc"))


def _part(sheet, designator):
    return [p for p in sheet.parts if p.designator == designator]


def test_power_sheet_parts(power):
    assert len(power.parts) == 30
    (c13,) = _part(power, "C13P")
    assert c13.uid == "RKDRJMCU"
    assert c13.libref == "Cap Semi"
    assert c13.footprints and c13.footprints[0]


def test_pins_attach_to_owner(power):
    (c13,) = _part(power, "C13P")
    assert {p.number for p in c13.pins} == {"1", "2"}
    assert all(p.length == pytest.approx(10 * 0.254) for p in c13.pins)


def test_multipart_separate_records(core):
    parts = sorted(_part(core, "U10C"), key=lambda p: p.part_id)
    assert [(p.part_id, p.uid) for p in parts] == [(1, "PLEULKAG"), (2, "GRKVCHXY")]
    # Each placed part carries only its own pins.
    assert not {p.number for p in parts[0].pins} & {p.number for p in parts[1].pins}


def test_value_from_comment(power):
    (c13,) = _part(power, "C13P")
    assert "Comment" in c13.params and c13.params["Comment"].text


def test_wiring_and_ports(power):
    assert power.wires and all(len(w) >= 2 for w in power.wires)
    assert power.junctions
    assert {r.text("TEXT") for r in power.power_ports} >= {"GND"}
    assert power.net_labels


def test_sheet_size_and_fonts(power):
    assert power.width_mm > 200 and power.height_mm > 150
    assert 1 in power.fonts and power.fonts[1].size_pt > 0


def test_sheet_symbols_and_entries(sampler_root):
    syms = sampler_root.sheet_symbols
    assert len(syms) == 12
    assert sum(1 for s in syms if s.file_name == "Input_channel.SchDoc") == 8
    (adc,) = [s for s in syms if s.uid == "UMAXLLEH"]
    assert adc.name == "U_ADC_8CH" and len(adc.entries) == 31


def test_notes_and_frames_are_sheet_graphics(sampler_root):
    kinds = {g.kind for g in sampler_root.graphics}
    assert "textframe" in kinds
    assert any("CERN OHL" in g.rec.text("TEXT") for g in sampler_root.graphics if g.kind == "textframe")


def test_unsupported_counted(core):
    # Directives (DIFFPAIR, net classes) have no KiCad equivalent here.
    assert core.unsupported["PARAMETER_SET"] == 38


def test_harness_ports_flagged():
    sheet = model.load_sheet(altium_fixture("qa/issue16903/harness_subscheet.SchDoc"))
    assert len(sheet.ports) == 1  # still converted as a port
    assert sheet.unsupported["HARNESS_PORT"] == 1


def test_title_block(power):
    assert set(power.title) <= {"Title", "Revision", "Date", "DocumentNumber", "CompanyName",
                                "SheetNumber", "SheetTotal"}
    assert "*" not in power.title.values()


def test_ascii_ticket1303_loads():
    sheet = model.load_sheet(altium_fixture("qa/ticket1303/overview.SchDoc"))
    assert len(sheet.sheet_symbols) == 1


def test_page_is_altiums_own_size(power):
    # Altium's "A" is 9.5 x 7.5 in, not KiCad's 11 x 8.5 in "A": the page keeps
    # Altium's size so the content sits where Altium drew it.
    assert power.paper == "User"
    assert (power.width_mm, power.height_mm) == pytest.approx((950 * 0.254, 750 * 0.254))
    assert power.title_block_on and power.border_on and power.style_name == "A"
