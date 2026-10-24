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
    # One-pad nets join nothing, so they are no topology question, but their
    # names still follow the schematic.
    lone_sch = {}
    for name, pads in schematic.items():
        kept = {p for p in pads if p[0] not in exclude_refs}
        if len(kept) == 1:
            lone_sch[next(iter(kept))] = name
    for name, pads in board.items():
        kept = {p for p in pads if p[0] not in exclude_refs}
        if len(kept) == 1 and next(iter(kept)) in lone_sch:
            matched[name] = lone_sch[next(iter(kept))]
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


_AUTO_NET = re.compile(r"((?:Net|unconnected)-\()(.*)(\))$")


def kicad_net_name(name: str) -> str:
    """A netlist name as the board must store it.

    KiCad builds automatic names from pin names, where a "/" would read as a
    sheet separator, so it stores it as {slash}; the XML netlist shows it
    unescaped.
    """
    m = _AUTO_NET.search(name)
    if not m:
        return name
    return name[:m.start()] + m.group(1) + m.group(2).replace("/", "{slash}") + m.group(3)


def schematic_pin_nets(install, root_sch: Path) -> dict[str, PadSet]:
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-") as tmp:
        xml = Path(tmp) / "net.xml"
        result = cli.export_netlist(install, root_sch, xml)
        if result["returncode"] != 0 or not xml.is_file():
            raise cli.CliError(f"kicad-cli could not netlist {root_sch.name}: {result['stderr']}",
                               remedy="Open the converted schematic in KiCad to see why.")
        netlist = cli.parse_kicadxml_netlist(xml)
    return {kicad_net_name(n["name"]): {(node["reference"], node["pin"]) for node in n["nodes"]}
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


#: Every DRC check that looks at copper, holes, silk or courtyards. Parity
#: needs none of them, and on a dense board they take a quarter of an hour.
PHYSICAL_CHECKS = (
    "annular_width assertion_failure clearance connection_width copper_edge_clearance "
    "copper_sliver courtyards_overlap creepage diff_pair_gap_out_of_range "
    "diff_pair_uncoupled_length_too_long drill_out_of_range footprint footprint_filters_mismatch "
    "footprint_type_mismatch generic_error generic_warning hole_clearance hole_to_hole "
    "holes_co_located invalid_outline isolated_copper item_on_disabled_layer items_not_allowed "
    "length_out_of_range lib_footprint_issues lib_footprint_mismatch malformed_courtyard "
    "microvia_drill_out_of_range mirrored_text_on_front_layer missing_courtyard "
    "missing_tuning_profile nonmirrored_text_on_back_layer npth_inside_courtyard padstack "
    "padstack_invalid pth_inside_courtyard shorting_items silk_edge_clearance silk_over_copper "
    "silk_overlap skew_out_of_range solder_mask_bridge starved_thermal text_height "
    "text_on_edge_cuts text_thickness through_hole_pad_without_hole too_many_vias track_angle "
    "track_dangling track_not_centered_on_via track_on_post_machined_layer track_segment_length "
    "track_width tracks_crossing tuning_profile_track_geometries unconnected_items "
    "unresolved_variable via_dangling via_diameter zones_intersect").split()


def parity(install, pcb: Path, known_refs: set[str], output: Path | None = None,
           timeout: int = 900) -> dict:
    """KiCad's schematic parity, run on a copy whose project ignores every physical check."""
    import shutil  # noqa: PLC0415

    out = output or pcb.with_suffix(".parity.json")
    out.unlink(missing_ok=True)  # a report left by an earlier run must not pass for this one
    result: dict = {}
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-") as tmp:
        for f in pcb.parent.iterdir():
            if f.suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_wks"):
                shutil.copy(f, Path(tmp) / f.name)
        pro = Path(tmp) / (pcb.stem + ".kicad_pro")
        data = json.loads(pro.read_text(encoding="utf-8")) if pro.is_file() else {}
        data.setdefault("board", {}).setdefault("design_settings", {})["rule_severities"] = {
            k: "ignore" for k in PHYSICAL_CHECKS}
        pro.write_text(json.dumps(data), encoding="utf-8")
        result = cli.run_cli(install, ["pcb", "drc", "--schematic-parity", "--format", "json",
                                       "--severity-all", "-o", str(out), str(Path(tmp) / pcb.name)],
                             timeout=timeout)
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No verdict is not a clean verdict: it must keep `aligned` false.
        why = (result.get("stderr") or result.get("stdout") or "no report").strip()[-500:]
        return {"error": f"kicad-cli wrote no DRC report: {why}", "missing": None, "extra": None,
                "duplicates": None, "other": {},
                "unexplained": [{"type": "parity_not_run", "description": why, "refs": []}]}
    summary = summarize_parity(report, known_refs)
    summary["unconnected"] = len(report.get("unconnected_items", []))
    summary["report_file"] = str(out)
    return summary
