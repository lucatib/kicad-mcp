"""Diagnose the environment without changing anything.

Every check reports a remedy on failure. For a tool whose most common failure is
environmental rather than logical, this is the difference between working
software and a queue of `ImportError: _pcbnew` reports.
"""

from __future__ import annotations

import sys
from pathlib import Path

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def _line(status: str, label: str, detail: str = "", remedy: str = "") -> str:
    mark = {"PASS": "[ok]", "FAIL": "[!!]", "WARN": "[ ~]"}[status]
    out = f"{mark} {label}"
    if detail:
        out += f": {detail}"
    if remedy and status != PASS:
        out += f"\n     -> {remedy}"
    return out


def run_checks() -> tuple[list[str], bool]:
    lines: list[str] = []
    ok = True

    if sys.platform != "win32":
        lines.append(_line(FAIL, "Platform", sys.platform, "This server supports Windows only."))
        return lines, False
    lines.append(_line(PASS, "Platform", sys.platform))

    lines.append(_line(PASS, "Interpreter", f"{sys.version.split()[0]} at {sys.executable}"))

    try:
        from .discovery import find_install, find_installs

        inst = find_install()
        lines.append(_line(PASS, "KiCad install", f"{inst.version} at {inst.root}"))
        others = [i for i in find_installs() if i.root != inst.root]
        if others:
            lines.append(_line(WARN, "Other installs", ", ".join(i.version for i in others),
                               "Set KICAD_MCP_KICAD_ROOT to pin a specific version."))
    except Exception as exc:  # noqa: BLE001
        lines.append(_line(FAIL, "KiCad install", str(exc),
                           getattr(exc, "remedy", "Install KiCad 10.")))
        return lines, False

    try:
        import pcbnew

        lines.append(_line(PASS, "pcbnew (SWIG)", pcbnew.GetBuildVersion()))
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(_line(
            FAIL, "pcbnew (SWIG)", str(exc),
            'Create the venv from KiCad\'s interpreter: '
            f'"{inst.python_path}" -m venv --system-site-packages .venv',
        ))

    try:
        import kipy
        import importlib.metadata as md

        lines.append(_line(PASS, "kicad-python (kipy)", md.version("kicad-python")))
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(_line(FAIL, "kicad-python (kipy)", str(exc),
                           "pip install kicad-python"))

    try:
        import mcp
        import importlib.metadata as md

        lines.append(_line(PASS, "mcp SDK", md.version("mcp")))
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(_line(FAIL, "mcp SDK", str(exc), "pip install 'mcp>=2.0'"))

    # IPC server setting lives in kicad_common.json; a disabled server is the
    # single most common reason live tools fail on an otherwise healthy install.
    try:
        import json
        import os

        cfg = Path(os.environ.get("APPDATA", "")) / "kicad" / f"{inst.major}.0" / "kicad_common.json"
        if cfg.is_file():
            data = json.loads(cfg.read_text(encoding="utf-8"))
            enabled = bool(data.get("api", {}).get("enable_server"))
            lines.append(_line(
                PASS if enabled else FAIL, "IPC API enabled", str(enabled),
                "Enable Preferences > Plugins > 'Enable IPC API server', then restart KiCad.",
            ))
            ok = ok and enabled
        else:
            lines.append(_line(WARN, "IPC API setting", f"not found at {cfg}",
                               "Launch KiCad once to create its configuration."))
    except Exception as exc:  # noqa: BLE001
        lines.append(_line(WARN, "IPC API setting", str(exc)))

    from .session import SESSION

    status = SESSION.status()
    if status.get("connected"):
        lines.append(_line(PASS, "Live connection",
                           f"KiCad {status.get('kicad_version')} / API {status.get('api_version')}"))
        if status.get("board_open"):
            lines.append(_line(PASS, "PCB editor", f"board '{status.get('board_name')}' open"))
        else:
            lines.append(_line(WARN, "PCB editor", "no board open",
                               "Open a board in the PCB editor to use the live PCB tools."))
    else:
        lines.append(_line(WARN, "Live connection", status.get("error", "not connected"),
                           status.get("remedy", "Start KiCad with the IPC server enabled.")))

    try:
        from .symbols import SymbolIndex

        libs = SymbolIndex(inst).list_libraries()
        lines.append(_line(PASS if libs else FAIL, "Symbol libraries", f"{len(libs)} resolved",
                           "Check sym-lib-table; reinstall KiCad libraries if empty."))
        ok = ok and bool(libs)
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(_line(FAIL, "Symbol libraries", str(exc)))

    lines.append("")
    lines.append("Note: KiCad 10 has no schematic IPC API. Schematic tools are")
    lines.append("file-based (kicad-cli + .kicad_sch parsing) and work with KiCad closed.")
    return lines, ok


def main() -> int:
    lines, ok = run_checks()
    print("kicad-mcp doctor")
    print("=" * 60)
    for line in lines:
        print(line)
    print("=" * 60)
    print("Ready." if ok else "Some checks failed; see remedies above.")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
