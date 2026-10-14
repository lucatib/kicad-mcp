"""Library tables resolve KiCad's path variables the way KiCad does.

Third-party libraries -- Würth Elektronik's, for one -- ship tables whose URIs
use variables of their own, `${WE_SYMBOL_DIR}` and so on, defined by the user
in KiCad's Preferences > Configure Paths. KiCad stores those in
kicad_common.json; an operating-system environment variable of the same name
overrides them. A table that names one we cannot resolve hides every library
in it, so these must work without a KiCad install.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_mcp.symbols import resolve_tables


@dataclass
class FakeInstall:
    share_dir: Path
    major: int = 10


def _table(kind: str, *rows: tuple[str, str, str]) -> str:
    lines = [f"({kind}", "\t(version 7)"]
    for name, type_, uri in rows:
        lines.append(f'\t(lib (name "{name}") (type "{type_}") (uri "{uri}") (options "") (descr ""))')
    return "\n".join(lines + [")", ""])


@pytest.fixture
def kicad(tmp_path, monkeypatch):
    """A config dir with a global table, a stock table, and a vendor library tree."""
    appdata = tmp_path / "appdata"
    config = appdata / "kicad" / "10.0"
    config.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("WE_SYMBOL_DIR", raising=False)

    share = tmp_path / "share"
    (share / "template").mkdir(parents=True)
    (share / "symbols").mkdir()
    (share / "symbols" / "Device.kicad_sym").write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    (share / "symbols" / "Inductor.kicad_sym").write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    (share / "template" / "sym-lib-table").write_text(_table(
        "sym_lib_table",
        ("Device", "KiCad", "${KICAD10_SYMBOL_DIR}/Device.kicad_sym"),
        ("Inductor", "KiCad", "${KICAD10_SYMBOL_DIR}/Inductor.kicad_sym"),
    ), encoding="utf-8")

    vendor = tmp_path / "vendor"
    (vendor / "symbols").mkdir(parents=True)
    for name in ("Relay_Vendor", "Inductor"):
        (vendor / "symbols" / f"{name}.kicad_sym").write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    (vendor / "sym-lib-table").write_text(_table(
        "sym_lib_table",
        ("Relay_Vendor", "KiCad", "${WE_SYMBOL_DIR}/Relay_Vendor.kicad_sym"),
        ("Inductor", "KiCad", "${WE_SYMBOL_DIR}/Inductor.kicad_sym"),
    ), encoding="utf-8")

    # As KiCad 10 writes it: the stock table nested first, then the vendor's.
    (config / "sym-lib-table").write_text(_table(
        "sym_lib_table",
        ("KiCad", "Table", str(share / "template" / "sym-lib-table").replace("\\", "/")),
        ("Vendor", "Table", str(vendor / "sym-lib-table").replace("\\", "/")),
    ), encoding="utf-8")

    def configure(variables):
        common = {"environment": {"vars": variables}}
        (config / "kicad_common.json").write_text(json.dumps(common), encoding="utf-8")

    return FakeInstall(share), vendor, configure


def test_a_configured_path_variable_resolves_a_nested_vendor_table(kicad):
    install, vendor, configure = kicad
    configure({"WE_SYMBOL_DIR": str(vendor / "symbols")})

    entries = resolve_tables(install, None, "sym-lib-table")

    assert entries["Relay_Vendor"].exists()
    assert entries["Relay_Vendor"].path == vendor / "symbols" / "Relay_Vendor.kicad_sym"
    assert entries["Device"].exists(), "the stock libraries are still there"


def test_an_environment_variable_overrides_the_configured_one(kicad, monkeypatch, tmp_path):
    install, vendor, configure = kicad
    configure({"WE_SYMBOL_DIR": str(tmp_path / "nowhere")})
    monkeypatch.setenv("WE_SYMBOL_DIR", str(vendor / "symbols"))

    entries = resolve_tables(install, None, "sym-lib-table")

    assert entries["Relay_Vendor"].exists()


def test_a_later_row_of_the_same_name_wins_as_in_kicad(kicad):
    install, vendor, configure = kicad
    configure({"WE_SYMBOL_DIR": str(vendor / "symbols")})

    entries = resolve_tables(install, None, "sym-lib-table")

    assert entries["Inductor"].path == vendor / "symbols" / "Inductor.kicad_sym"


@pytest.mark.parametrize("common", [
    {"environment": {"vars": None}},  # what KiCad writes when no path is configured
    {},
    "not json",
])
def test_no_configured_variables_is_not_an_error(kicad, common):
    install, vendor, _ = kicad
    config = Path(install.share_dir).parent / "appdata" / "kicad" / "10.0"
    text = common if isinstance(common, str) else json.dumps(common)
    (config / "kicad_common.json").write_text(text, encoding="utf-8")

    entries = resolve_tables(install, None, "sym-lib-table")

    assert entries["Device"].exists()
    assert not entries["Relay_Vendor"].exists(), "unresolved, so the library is missing, not a crash"
