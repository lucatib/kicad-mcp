"""Is the converted project aligned? Nets compared as pad partitions, plus parity.

Net names differ between the two sides by construction (KiCad prefixes a
sheet-local net with its sheet path, Altium does not), so nets are matched by
the set of pads they join. A board net with no equally-joined schematic net
is a topology difference: either a conversion error or a change made in
Altium and never pushed to the board -- this check cannot tell which.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from .. import cli

PadSet = set[tuple[str, str]]

_REF_PATTERNS = (re.compile(r"\bof (\S+) on\b"), re.compile(r"^Footprint (\S+)"),
                 re.compile(r"^Symbol (\S+)"), re.compile(r"\bfootprint (\S+) \("))


def _fmt(pads) -> list[str]:
    return sorted(f"{r}.{p}" for r, p in pads)


def compare_nets(board: dict[str, PadSet], schematic: dict[str, PadSet],
                 exclude_refs: set[str], cap: int = 20) -> dict:
    """Match nets by pad set; report what joins differently on each side."""
    def clean(nets):
        out = {}
        for name, pads in nets.items():
            kept = frozenset(p for p in pads if p[0] not in exclude_refs)
            if len(kept) > 1:  # one pad joins nothing
                out[name] = kept
        return out

    b, s = clean(board), clean(schematic)
    by_set = {pads: name for name, pads in s.items()}
    matched: dict[str, str] = {}
    diffs: list[dict] = []
    for name, pads in sorted(b.items()):
        if pads in by_set:
            matched[name] = by_set[pads]
            continue
        closest = max(s.items(), key=lambda kv: len(kv[1] & pads), default=(None, frozenset()))
        overlap = closest[1] if closest[0] and closest[1] & pads else frozenset()
        diffs.append({"board_net": name,
                      "closest_schematic_net": closest[0] if overlap else None,
                      "only_on_board": _fmt(pads - overlap)[:cap],
                      "only_in_schematic": _fmt(overlap - pads)[:cap]})
    matched_sch = set(matched.values())
    board_pads = set().union(*b.values()) if b else set()
    for name, pads in sorted(s.items()):
        if name in matched_sch or name in {d["closest_schematic_net"] for d in diffs}:
            continue
        if pads & board_pads:
            continue  # already part of a reported difference
        diffs.append({"board_net": None, "schematic_net": name,
                      "only_in_schematic": _fmt(pads)[:cap], "only_on_board": []})
    return {"matched": matched, "topology_differences": diffs,
            "excluded_parts": sorted(exclude_refs)}


def schematic_pin_nets(install, root_sch: Path) -> dict[str, PadSet]:
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-") as tmp:
        xml = Path(tmp) / "net.xml"
        result = cli.export_netlist(install, root_sch, xml)
        if result["returncode"] != 0 or not xml.is_file():
            raise cli.CliError(f"kicad-cli could not netlist {root_sch.name}: {result['stderr']}",
                               remedy="Open the converted schematic in KiCad to see why.")
        netlist = cli.parse_kicadxml_netlist(xml)
    return {n["name"]: {(node["reference"], node["pin"]) for node in n["nodes"]}
            for n in netlist["nets"]}


def refs_in(item: dict) -> set[str]:
    text = item.get("description", "")
    return {m.group(1) for pat in _REF_PATTERNS for m in [pat.search(text)] if m}


def summarize_parity(report: dict, known_refs: set[str]) -> dict:
    """Counts by kind, and the entries no known source disagreement explains."""
    entries = report.get("schematic_parity", [])
    counts = {"missing_footprint": 0, "extra_footprint": 0, "duplicate_footprints": 0}
    other: dict[str, int] = {}
    unexplained = []
    for e in entries:
        kind = e.get("type", "")
        if kind in counts:
            counts[kind] += 1
        else:
            other[kind] = other.get(kind, 0) + 1
        refs = set().union(*(refs_in(i) for i in e.get("items", []))) | refs_in(e)
        if not refs or not refs <= known_refs:
            unexplained.append({"type": kind, "description": e.get("description", ""),
                                "refs": sorted(refs - known_refs) or sorted(refs)})
    return {"missing": counts["missing_footprint"], "extra": counts["extra_footprint"],
            "duplicates": counts["duplicate_footprints"], "other": other,
            "unexplained": unexplained}


def parity(install, pcb: Path, known_refs: set[str], output: Path | None = None,
           timeout: int = 900) -> dict:
    out = output or pcb.with_suffix(".parity.json")
    cli.run_cli(install, ["pcb", "drc", "--schematic-parity", "--format", "json", "--severity-all",
                          "-o", str(out), str(pcb)], timeout=timeout)
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"error": "kicad-cli wrote no DRC report", "missing": None, "extra": None,
                "duplicates": None, "other": {}, "unexplained": []}
    summary = summarize_parity(report, known_refs)
    summary["unconnected"] = len(report.get("unconnected_items", []))
    summary["report_file"] = str(out)
    return summary
