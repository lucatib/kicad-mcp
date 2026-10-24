"""Wrapper around `kicad-cli`.

This is the whole of KiCad 10's supported headless schematic capability: ERC,
netlist export in several formats, BOM, and plotting. `netlist --format
kicadxml` is the most useful of them -- it yields structured components, nets,
and footprints, which is enough to reason about a design without a GUI.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

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
            # Elsewhere a child gets no window of its own; the flag is absent.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


#: What KiCad 10.0.6 writes; used only when kicad-cli cannot be asked.
FALLBACK_SCH_VERSION = 20260306

_MINIMAL_SCH = (
    '(kicad_sch (version 20231120) (generator "kicad-mcp") '
    '(uuid "00000000-0000-0000-0000-000000000000") (paper "A4") (lib_symbols) '
    '(sheet_instances (path "/" (page "1"))))\n'
)


def schematic_format_version(install) -> int:
    """The `.kicad_sch` format version the installed KiCad writes.

    KiCad asks to re-save any file whose version is older than its own, and
    point releases bump it (10.0.5 wrote 20250610, 10.0.6 writes 20260306), so
    a hardcoded number goes stale with every update. Asked of kicad-cli once
    per install by upgrading a throwaway file -- ~0.3 s -- then cached.
    """
    try:
        stamp = install.cli_path.stat().st_mtime_ns
    except OSError:
        return FALLBACK_SCH_VERSION
    return _format_version_cached(str(install.cli_path), stamp)


@lru_cache(maxsize=4)
def _format_version_cached(cli_path: str, stamp: int) -> int:
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-") as tmp:
        probe = Path(tmp) / "probe.kicad_sch"
        probe.write_text(_MINIMAL_SCH, encoding="utf-8")
        try:
            result = run_cli(SimpleNamespace(cli_path=Path(cli_path)),
                             ["sch", "upgrade", "--force", str(probe)], timeout=60)
        except CliError:
            return FALLBACK_SCH_VERSION
        match = re.search(r"\(version (\d+)\)", probe.read_text(encoding="utf-8"))
    if result["returncode"] != 0 or not match:
        return FALLBACK_SCH_VERSION
    return int(match.group(1))


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


def project_variants(path: str | Path) -> list[dict]:
    """The design variants of the project `path` belongs to: name and description.

    KiCad 10 lists them in the .kicad_pro; each symbol keeps its own
    differences (not fitted, out of the BOM, other field values) per variant.
    """
    from .hierarchy import find_project_dir  # noqa: PLC0415 - avoid a cycle

    folder = find_project_dir(path)
    if folder is None:
        return []
    src = Path(path)
    pros = sorted(folder.glob("*.kicad_pro"))
    pro = next((p for p in pros if p.stem == src.stem), pros[0] if pros else None)
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return []
    entries = (data.get("schematic") or {}).get("variants") or []
    return [{"name": e.get("name", ""), "description": e.get("description", "")}
            for e in entries if isinstance(e, dict) and e.get("name")]


VARIANT_DEFAULT_NOTE = "the design as drawn, used when no variant is given"


def variant_summary(path: str | Path) -> dict:
    """Each variant of the project and what it changes, read from the sheets.

    A variant only records how its parts differ from the drawn design: not
    fitted (dnp), left out of the BOM or the board, or other field values.
    """
    from . import sexpr  # noqa: PLC0415
    from .hierarchy import find_project_dir  # noqa: PLC0415

    variants = project_variants(path)
    changes: dict[str, dict[str, list]] = {
        v["name"]: {"not_fitted": [], "excluded_from_bom": [], "excluded_from_board": [],
                    "changed_fields": []} for v in variants}
    folder = find_project_dir(path) or Path(path).parent
    for sch in sorted(folder.glob("*.kicad_sch")):
        try:
            doc = sexpr.parse(sch.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for path_node in sexpr.find_all(doc, "path"):
            ref = sexpr.value(path_node, "reference")
            if ref is None:
                continue
            ref = sexpr.as_text(ref)
            for var in sexpr.children(path_node, "variant"):
                name = sexpr.as_text(sexpr.value(var, "name", ""))
                entry = changes.setdefault(name, {"not_fitted": [], "excluded_from_bom": [],
                                                  "excluded_from_board": [], "changed_fields": []})

                def flag(key: str) -> str | None:
                    v = sexpr.value(var, key)
                    return None if v is None else sexpr.as_text(v)

                if flag("dnp") == "yes":
                    entry["not_fitted"].append(ref)
                if flag("in_bom") == "no":
                    entry["excluded_from_bom"].append(ref)
                if flag("on_board") == "no":
                    entry["excluded_from_board"].append(ref)
                for f in sexpr.children(var, "field"):
                    entry["changed_fields"].append({
                        "reference": ref, "field": sexpr.as_text(sexpr.value(f, "name", "")),
                        "value": sexpr.as_text(sexpr.value(f, "value", ""))})
    described = {v["name"]: v["description"] for v in variants}
    out = []
    for name, entry in changes.items():
        for key in ("not_fitted", "excluded_from_bom", "excluded_from_board"):
            entry[key] = sorted(set(entry[key]))
        out.append({"name": name, "description": described.get(name, ""),
                    "listed_in_project": name in described, **entry})
    return {"variants": out, "default": VARIANT_DEFAULT_NOTE}


def _variant_args(path: Path, variant: str | None) -> list[str]:
    """`--variant NAME` for kicad-cli, once NAME is known to exist.

    kicad-cli quietly exports the default variant for a name it does not
    know, which would pass a wrong BOM off as the requested one.
    """
    if not variant:
        return []
    names = [v["name"] for v in project_variants(path)]
    if variant not in names:
        have = ", ".join(names) if names else "none"
        raise ToolInputError(f"No variant {variant!r} in this project (variants: {have}).",
                             remedy="Call list_variants for the names, or omit variant "
                                    "for the default.")
    return ["--variant", variant]


def export_netlist(
    install, schematic: str | Path, output: str | Path | None = None, fmt: str = "kicadxml",
    variant: str | None = None,
) -> dict:
    src = _require_file(schematic, ".kicad_sch")
    out = Path(output) if output else src.with_suffix(".net.xml" if fmt == "kicadxml" else ".net")
    result = run_cli(
        install, ["sch", "export", "netlist", "--format", fmt, *_variant_args(src, variant),
                  "-o", str(out), str(src)]
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


def erc_violation_count(install, schematic: str | Path) -> int:
    """Total ERC violations across all sheets, from kicad-cli's JSON report."""
    src = _require_file(schematic, ".kicad_sch")
    out = src.with_suffix(".erc.json")
    result = run_cli(install, ["sch", "erc", "--format", "json", "-o", str(out), str(src)])
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(
            f"ERC produced no readable report for {src.name}: {result.get('stderr') or exc}"
        ) from exc
    return sum(len(sheet.get("violations", [])) for sheet in report.get("sheets", []))


def _fix_bom_references(install, schematic: Path, bom: Path, variant: str | None) -> int:
    """Take the "?" off references KiCad's BOM wrongly shows as unannotated.

    The BOM splits each reference into prefix and number, so one ending in a
    letter (C10A, Altium's channel style) prints as "C10A?". A reference the
    netlist really has loses the "?"; a truly unannotated "R?" keeps it.
    Returns how many references were fixed.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    text = bom.read_text(encoding="utf-8")
    if "?" not in text:
        return 0
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-") as tmp:
        xml = Path(tmp) / "net.xml"
        res = export_netlist(install, schematic, xml, variant=variant)
        if res["returncode"] != 0 or not xml.is_file():
            return 0
        known = {c["reference"] for c in parse_kicadxml_netlist(xml)["components"]}
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return 0
    cols = [i for i, h in enumerate(rows[0]) if h.strip().lower() in ("refs", "reference", "references")]
    fixed = 0
    for row in rows[1:]:
        for i in cols:
            if i >= len(row):
                continue
            tokens = row[i].split(",")
            for j, tok in enumerate(tokens):
                stem = tok.strip()
                if stem.endswith("?") and stem[:-1] in known:
                    tokens[j] = tok.replace(stem, stem[:-1])
                    fixed += 1
            row[i] = ",".join(tokens)
    if fixed:
        buf = io.StringIO()
        csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)
        bom.write_text(buf.getvalue(), encoding="utf-8")
    return fixed


def export_bom(install, schematic: str | Path, output: str | Path | None = None,
               variant: str | None = None) -> dict:
    src = _require_file(schematic, ".kicad_sch")
    out = Path(output) if output else src.with_suffix(f".{variant}.bom.csv" if variant else ".bom.csv")
    result = run_cli(install, ["sch", "export", "bom", *_variant_args(src, variant),
                               "-o", str(out), str(src)])
    result["variant"] = variant
    result["output_file"] = str(out) if out.is_file() else None
    if out.is_file():
        result["references_fixed"] = _fix_bom_references(install, src, out, variant)
        try:
            result["preview"] = out.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:  # pragma: no cover
            pass
    return result


def export_plot(install, schematic: str | Path, fmt: str = "pdf",
                output: str | Path | None = None, variant: str | None = None) -> dict:
    if fmt not in ("pdf", "svg", "dxf", "ps", "hpgl"):
        raise ToolInputError(
            f"Unsupported schematic plot format {fmt!r}.",
            remedy="Use one of: pdf, svg, dxf, ps, hpgl.",
        )
    src = _require_file(schematic, ".kicad_sch")
    out = Path(output) if output else src.with_suffix(f".{fmt}")
    result = run_cli(install, ["sch", "export", fmt, *_variant_args(src, variant),
                               "-o", str(out), str(src)])
    result["output_file"] = str(out) if out.exists() else None
    return result


RENDER_SIDES = ("top", "bottom", "left", "right", "front", "back")
RENDER_QUALITIES = ("basic", "high", "user", "job_settings")


def render_pcb(install, board: str | Path, output: str | Path | None = None,
               side: str = "top", width: int = 1600, height: int = 1200,
               quality: str = "basic", rotate: str | None = None,
               zoom: float = 1.0, perspective: bool = False, variant: str | None = None) -> dict:
    """Render the board in 3D to PNG or JPEG, as the 3D viewer would show it.

    The default output goes to the temp folder, not the project: renders are
    for looking at, and a project should not fill up with them.
    """
    src = _require_file(board, ".kicad_pcb")
    if side not in RENDER_SIDES:
        raise ToolInputError(f"Unknown side {side!r}.",
                             remedy=f"Use one of: {', '.join(RENDER_SIDES)}.")
    if quality not in RENDER_QUALITIES:
        raise ToolInputError(f"Unknown quality {quality!r}.",
                             remedy=f"Use one of: {', '.join(RENDER_QUALITIES)}.")
    if width <= 0 or height <= 0:
        raise ToolInputError("width and height must be positive pixels.")
    if output:
        out = Path(output)
    else:
        out = Path(tempfile.gettempdir()) / "kicad-mcp-renders" / f"{src.stem}-{side}.png"
    if out.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        raise ToolInputError(f"Cannot render to {out.name}.",
                             remedy="Use a .png, .jpg or .jpeg output file.")
    out.parent.mkdir(parents=True, exist_ok=True)

    args = ["pcb", "render", "-o", str(out), "--side", side, "--width", str(width),
            "--height", str(height), "--quality", quality, "--zoom", str(zoom),
            *_variant_args(src, variant)]
    if rotate:
        args += ["--rotate", rotate]
    if perspective:
        args.append("--perspective")
    args.append(str(src))
    result = run_cli(install, args)
    # kicad-cli prints a line per layer it builds; only the verdict matters.
    result["stdout"] = "\n".join(result["stdout"].splitlines()[-3:])
    result["output_file"] = str(out) if out.is_file() else None
    return result


def summarize_drc_report(report: dict, violation_type: str | None = None,
                         severity: str | None = None, limit: int = 100) -> dict:
    """Counts over every violation, plus a filtered, capped list of them.

    A dense board's report runs to megabytes; a character slice of it keeps
    only whichever violation kind KiCad happened to list first and breaks the
    JSON. Counts by type and severity span the whole report, so a rare but
    serious kind (invalid_outline among a thousand clearances) still shows,
    and the filters fetch exactly those.
    """
    violations = report.get("violations", [])
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for v in violations:
        by_type[v.get("type", "")] = by_type.get(v.get("type", ""), 0) + 1
        by_severity[v.get("severity", "")] = by_severity.get(v.get("severity", ""), 0) + 1

    matching = [
        v for v in violations
        if (not violation_type or fnmatch.fnmatch(v.get("type", ""), violation_type))
        and (not severity or v.get("severity", "") == severity)
    ]
    out = {
        "kicad_version": report.get("kicad_version"),
        "coordinate_units": report.get("coordinate_units", "mm"),
        "violation_count": len(violations),
        "counts_by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "counts_by_severity": by_severity,
        "unconnected_count": len(report.get("unconnected_items", [])),
        "schematic_parity_count": len(report.get("schematic_parity", [])),
        "matching": len(matching),
        "violations": matching[:limit],
    }
    if len(matching) > limit:
        out["truncated"] = True
        out["hint"] = (f"{len(matching) - limit} more; filter by violation_type "
                       "or severity, or raise limit.")
    return out


def pcb_drc(install, board: str | Path, output: str | Path | None = None,
            violation_type: str | None = None, severity: str | None = None,
            limit: int = 100) -> dict:
    src = _require_file(board, ".kicad_pcb")
    out = Path(output) if output else src.with_suffix(".drc.json")
    result = run_cli(install, ["pcb", "drc", "--format", "json", "-o", str(out), str(src)])
    result["report_file"] = str(out) if out.is_file() else None
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result  # kicad-cli's stderr says why there is no report
    result.update(summarize_drc_report(report, violation_type, severity, limit))
    return result
