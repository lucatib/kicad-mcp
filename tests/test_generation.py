"""Schematic generation tests.

These need a real KiCad install for its symbol libraries, so they are marked
`requires_kicad`. The geometry assertions are the point: pin transforms are the
one place a subtle error produces a file that opens fine but is wired wrong.
"""

from __future__ import annotations

import pytest

from kicad_mcp.discovery import find_install
from kicad_mcp.generator import PlacedSymbol, generate_pinout_schematic
from kicad_mcp.schematic import Schematic
from kicad_mcp.symbols import PinInfo, SymbolIndex

pytestmark = pytest.mark.requires_kicad

MCU = "RF_Module:ESP32-S3-MINI-1"


@pytest.fixture(scope="module")
def index():
    try:
        return SymbolIndex(find_install())
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


def _pin(x, y, angle):
    return PinInfo("1", "P", "bidirectional", x, y, angle, 2.54, 1)


def test_pin_point_flips_y_from_symbol_to_canvas():
    """Symbol libraries are Y-up; the schematic canvas is Y-down."""
    placed = PlacedSymbol("U1", "v", MCU, x=100.0, y=100.0, angle=0, unit=1, uuid="u")
    assert placed.pin_point(_pin(-15.24, 20.32, 0)) == (84.76, 79.68)


@pytest.mark.parametrize(
    "angle,expected",
    [(0, (-1.0, 0.0)), (180, (1.0, 0.0)), (90, (0.0, 1.0)), (270, (0.0, -1.0))],
)
def test_pin_outward_points_away_from_body(angle, expected):
    placed = PlacedSymbol("U1", "v", MCU, x=0, y=0, angle=0, unit=1, uuid="u")
    dx, dy = placed.pin_outward(_pin(0, 0, angle))
    assert (round(dx), round(dy)) == expected


def test_generates_schematic_with_expected_content(index, tmp_path):
    out = tmp_path / "gen.kicad_sch"
    res = generate_pinout_schematic(
        index, out, MCU,
        assignments={"IO4": "LED", "IO5": "BTN"},
        title="test",
    )
    assert res["connected_count"] == 2
    assert not res["unmatched_assignments"]
    assert out.is_file()

    sch = Schematic.load(out)
    from kicad_mcp import cli as kcli

    assert sch.version == kcli.schematic_format_version(index.install)
    labels = {lbl["text"] for lbl in sch.labels()}
    assert {"LED", "BTN"} <= labels
    # The MCU definition must be embedded, or KiCad shows a rescue dialog.
    assert MCU in sch.lib_symbol_ids()


def test_pin_matching_ignores_case_and_separators(index, tmp_path):
    res = generate_pinout_schematic(
        index, tmp_path / "g.kicad_sch", MCU,
        assignments={"GPIO_4": "A", "io5": "B", "IO-6": "C"},
    )
    assert res["connected_count"] == 3
    assert not res["unmatched_assignments"]


def test_unmatched_assignments_are_reported_not_silently_dropped(index, tmp_path):
    res = generate_pinout_schematic(
        index, tmp_path / "g.kicad_sch", MCU,
        assignments={"IO4": "OK", "NOT_A_PIN": "BAD"},
    )
    assert res["connected_count"] == 1
    assert res["unmatched_assignments"] == ["NOT_A_PIN"]


def test_power_pins_get_power_symbols(index, tmp_path):
    res = generate_pinout_schematic(
        index, tmp_path / "g.kicad_sch", MCU, assignments={"IO4": "X"},
        connect_power=True,
    )
    assert res["power_symbols"], "expected GND/3V3 power symbols to be placed"


def test_generated_file_reparses_identically(index, tmp_path):
    from kicad_mcp import sexpr

    out = tmp_path / "g.kicad_sch"
    generate_pinout_schematic(index, out, MCU, assignments={"IO4": "X"})
    text = out.read_text(encoding="utf-8")
    assert sexpr.dumps(sexpr.parse(text)) == text


def test_generated_file_is_already_in_the_installed_kicad_format(index, tmp_path):
    """KiCad prompts to re-save any file whose format version is older than its own.

    The oracle is KiCad itself: upgrading our output must not change the
    version number, i.e. there was nothing to upgrade.
    """
    import shutil

    from kicad_mcp import cli as kcli

    out = tmp_path / "gen.kicad_sch"
    generate_pinout_schematic(index, out, MCU, assignments={"IO4": "LED"})
    ours = Schematic.load(out).version

    upgraded = tmp_path / "upgraded.kicad_sch"
    shutil.copy(out, upgraded)
    kcli.run_cli(index.install, ["sch", "upgrade", "--force", str(upgraded)])
    assert ours == Schematic.load(upgraded).version


def test_format_version_falls_back_when_kicad_cli_cannot_run(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from kicad_mcp import cli as kcli

    broken = SimpleNamespace(cli_path=Path(tmp_path / "missing" / "kicad-cli.exe"))
    assert kcli.schematic_format_version(broken) == kcli.FALLBACK_SCH_VERSION
