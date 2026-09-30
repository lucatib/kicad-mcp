"""Wrapper around `kicad-cli`.

This is the whole of KiCad 10's supported headless schematic capability: ERC,
netlist export in several formats, BOM, and plotting. `netlist --format
kicadxml` is the most useful of them -- it yields structured components, nets,
and footprints, which is enough to reason about a design without a GUI.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from .errors import CliError, ToolInputError

DEFAULT_TIMEOUT = 180


def run_cli(install, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run kicad-cli and return its outcome without raising on non-zero exit.

    ERC and DRC use exit codes to report violations found, not just failure, so
    callers need the code and the output rather than an exception.
    """
    cmd = [str(install.cli_path), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # The MCP server's own stdin is the client's pipe (stdio transport),
            # which has no OS handle a child process can duplicate. Same failure
            # mode as running under pytest's captured stdin -- WinError 6.
            stdin=subprocess.DEVNULL,
            # Clients launch the server without a console, so Windows would
            # give kicad-cli a fresh visible one -- stealing focus every call.
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            f"kicad-cli timed out after {timeout}s: {' '.join(args)}",
            remedy="Increase the timeout, or check the file is not corrupt.",
        ) from exc
    except OSError as exc:
        raise CliError(f"Could not run kicad-cli: {exc}") from exc

    return {
        "command": " ".join(args),
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _require_file(path: str | Path, suffix: str) -> Path:
    p = Path(path)
    if not p.is_file():
        raise ToolInputError(
            f"File not found: {p}", remedy=f"Pass a full path to a {suffix} file."
        )
    if p.suffix != suffix:
        raise ToolInputError(
            f"Expected a {suffix} file, got {p.name}",
            remedy=f"Pass a {suffix} file.",
        )
    return p


def export_netlist(
    install, schematic: str | Path, output: str | Path | None = None, fmt: str = "kicadxml"
) -> dict:
    src = _require_file(schematic, ".kicad_sch")
    out = Path(output) if output else src.with_suffix(".net.xml" if fmt == "kicadxml" else ".net")
    result = run_cli(
        install, ["sch", "export", "netlist", "--format", fmt, "-o", str(out), str(src)]
    )
    result["output_file"] = str(out) if out.is_file() else None
    return result


def parse_kicadxml_netlist(path: str | Path) -> dict:
    """Reduce a kicadxml netlist to components and nets.

    The raw XML is verbose; a model reasoning about connectivity needs the
    component list and the net-to-pin mapping, not the full document.
    """
    p = Path(path)
    if not p.is_file():
        raise ToolInputError(f"Netlist not found: {p}")
    root = ET.parse(p).getroot()

    components = []
    for comp in root.findall("./components/comp"):
        fields = {
            f.get("name", ""): (f.text or "")
            for f in comp.findall("./fields/field")
        }
        components.append(
            {
                "reference": comp.get("ref", ""),
                "value": (comp.findtext("value") or "").strip(),
                "footprint": (comp.findtext("footprint") or "").strip(),
                "datasheet": (comp.findtext("datasheet") or "").strip(),
                "library": (comp.findtext("./libsource") is not None)
                and comp.find("./libsource").get("lib", "")
                or "",
                "part": (comp.find("./libsource").get("part", "") if comp.find("./libsource") is not None else ""),
                "fields": fields,
            }
        )

    nets = []
    for net in root.findall("./nets/net"):
        nodes = [
            {"reference": n.get("ref", ""), "pin": n.get("pin", ""), "function": n.get("pinfunction", "")}
            for n in net.findall("node")
        ]
        nets.append(
            {
                "name": net.get("name", ""),
                "code": net.get("code", ""),
                "node_count": len(nodes),
                "nodes": nodes,
            }
        )

    return {
        "source": str(p),
        "component_count": len(components),
        "net_count": len(nets),
        "components": components,
        "nets": nets,
    }


def run_erc(install, schematic: str | Path, output: str | Path | None = None,
            fmt: str = "report", severity_all: bool = False) -> dict:
    src = _require_file(schematic, ".kicad_sch")
    out = Path(output) if output else src.with_suffix(".erc.rpt" if fmt == "report" else ".erc.json")
    args = ["sch", "erc", "--format", fmt, "-o", str(out), str(src)]
    if severity_all:
        args.insert(2, "--severity-all")
    result = run_cli(install, args)
    result["report_file"] = str(out) if out.is_file() else None
    if out.is_file():
        try:
            result["report"] = out.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:  # pragma: no cover
            pass
    return result


def export_bom(install, schematic: str | Path, output: str | Path | None = None) -> dict:
    src = _require_file(schematic, ".kicad_sch")
    out = Path(output) if output else src.with_suffix(".bom.csv")
    result = run_cli(install, ["sch", "export", "bom", "-o", str(out), str(src)])
    result["output_file"] = str(out) if out.is_file() else None
    if out.is_file():
        try:
            result["preview"] = out.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:  # pragma: no cover
            pass
    return result


def export_plot(install, schematic: str | Path, fmt: str = "pdf",
                output: str | Path | None = None) -> dict:
    if fmt not in ("pdf", "svg", "dxf", "ps", "hpgl"):
        raise ToolInputError(
            f"Unsupported schematic plot format {fmt!r}.",
            remedy="Use one of: pdf, svg, dxf, ps, hpgl.",
        )
    src = _require_file(schematic, ".kicad_sch")
    out = Path(output) if output else src.with_suffix(f".{fmt}")
    result = run_cli(install, ["sch", "export", fmt, "-o", str(out), str(src)])
    result["output_file"] = str(out) if out.exists() else None
    return result


def pcb_drc(install, board: str | Path, output: str | Path | None = None,
            fmt: str = "json") -> dict:
    src = _require_file(board, ".kicad_pcb")
    out = Path(output) if output else src.with_suffix(f".drc.{fmt}")
    result = run_cli(install, ["pcb", "drc", "--format", fmt, "-o", str(out), str(src)])
    result["report_file"] = str(out) if out.is_file() else None
    if out.is_file():
        try:
            result["report"] = out.read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:  # pragma: no cover
            pass
    return result
