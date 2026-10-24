"""import_altium: loose Altium files to a KiCad project whose sides already agree.

The pipeline reads every .SchDoc and the .PcbDoc, rebuilds the sheet
hierarchy, has kicad-cli convert the board, re-links each footprint to its
symbol by Altium UniqueId, writes the schematic, renames board nets to the
schematic's names, and then lets KiCad itself judge the result: the netlist
compared with the board's pads, and DRC's schematic parity. The project is
always written; what the Altium files disagree on is reported, never fixed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .. import cli
from ..errors import ToolInputError
from . import board as bd
from . import design as dz
from . import model, pcbdoc, sheets, symbols, variants, verify

SUMMARY_CAP = 50


def _gather(sources) -> tuple[list[Path], Path | None, Path | None]:
    items = [sources] if isinstance(sources, (str, Path)) else list(sources or [])
    files: list[Path] = []
    for raw in items:
        p = Path(raw)
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.is_file())
        elif p.is_file():
            files.append(p)
        else:
            raise ToolInputError(f"Not found: {p}", remedy="Pass a folder or Altium files that exist.")
    by_kind: dict[str, list[Path]] = {".schdoc": [], ".pcbdoc": [], ".prjpcb": []}
    for f in files:
        if f.suffix.lower() in by_kind:
            by_kind[f.suffix.lower()].append(f)
    if not by_kind[".schdoc"] and not by_kind[".pcbdoc"]:
        raise ToolInputError("No .SchDoc or .PcbDoc among the sources.",
                             remedy="Pass the folder holding the Altium files, or the files themselves.")
    for kind, label in ((".pcbdoc", "board"), (".prjpcb", "project")):
        if len(by_kind[kind]) > 1:
            names = ", ".join(f.name for f in by_kind[kind])
            raise ToolInputError(f"More than one {label} file: {names}.",
                                 remedy=f"Pass only the {label} to import (e.g. list the files instead "
                                        "of the folder); panels and variants often sit beside the board.")
    board = by_kind[".pcbdoc"][0] if by_kind[".pcbdoc"] else None
    prj = by_kind[".prjpcb"][0] if by_kind[".prjpcb"] else None
    return by_kind[".schdoc"], board, prj


def _default_name(sheet_list: list[model.Sheet], board: Path | None, prj: Path | None) -> str:
    if prj is not None:
        return prj.stem
    if board is not None:
        return board.stem
    referenced = {dz._key(s.file_name) for sh in sheet_list for s in sh.sheet_symbols}
    roots = sorted(s.stem for s in sheet_list if dz._key(s.path.name) not in referenced)
    return (roots or sorted(s.stem for s in sheet_list))[0]


def _cap(items: list) -> dict:
    out = {"count": len(items), "items": items[:SUMMARY_CAP]}
    if len(items) > SUMMARY_CAP:
        out["hint"] = f"{len(items) - SUMMARY_CAP} more in import_report.json"
    return out


def _project_file(path: Path, design: dz.Design, variant_names: list[tuple[str, str]]) -> None:
    sheets_list = [[n.kicad_uuid, n.name or (n.sheet.stem if n.sheet else "")]
                   for n in design.root.walk() if n is not design.root]
    data = {"meta": {"filename": path.name, "version": 3},
            "schematic": {"page_layout_descr_file": sheets.DRAWING_SHEET,
                          "variants": [{"name": n, **({"description": d} if d and d != n else {})}
                                       for n, d in variant_names]},
            "sheets": [[design.root_uuid, "Root"]] + sheets_list}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _disagreements(design: dz.Design, refs: dict, board_issues: list[dict], footprints_by_ref: dict,
                   joins, has_board: bool) -> list[dict]:
    """What the Altium schematic and board already disagree on."""
    out: list[dict] = []
    sch_by_ref: dict[str, tuple[dz.SheetNode, model.Part]] = {}
    for node in design.nodes():
        for part in node.sheet.parts:
            ref = refs.get((node.kicad_path, part.uid), part.designator)
            sch_by_ref.setdefault(ref, (node, part))
    moved: set[str] = set()
    for issue in board_issues:
        ref = issue["ref"]
        if issue["kind"] == "no_schematic_part" and ref in sch_by_ref:
            node, part = sch_by_ref[ref]
            out.append({"kind": "moved_sheet", "ref": ref,
                        "board": {"sheet": issue["board_sheet"], "id": issue["id"]},
                        "schematic": {"sheet": node.human_path, "id": part.uid}})
            moved.add(ref)
        else:
            entry = {"kind": "board_only", "ref": ref}
            if "id" in issue:
                entry["id"] = issue["id"]
            out.append(entry)
    if has_board:
        linked = {p.id_path for _, p in joins if p is not None}
        for node in design.nodes():
            for part in node.sheet.parts:
                key = (part.uid,) if design.flat else node.id_path + (part.uid,)
                ref = refs.get((node.kicad_path, part.uid), part.designator)
                if key in linked or ref in moved or not ref or ref.startswith("#"):
                    continue
                if part.part_count > 1 and any(
                        ((u.uid,) if design.flat else node.id_path + (u.uid,)) in linked
                        for u in node.sheet.parts
                        if u.designator == part.designator and u.libref == part.libref):
                    continue  # another unit of the same part carries the footprint
                out.append({"kind": "schematic_only", "ref": ref, "sheet": node.human_path})
        for fp, part in joins:
            if part is None:
                continue
            found = design.part_paths.get(part.id_path)
            if found is None:
                continue
            _, sch_part = found
            if sch_part.value and fp.value and sch_part.value != fp.value:
                out.append({"kind": "value_differs", "ref": fp.ref, "board": fp.value,
                            "schematic": sch_part.value})
            sch_fp = sch_part.footprints[0] if sch_part.footprints else ""
            if sch_fp and part.pattern and sch_fp.lower() != part.pattern.lower():
                out.append({"kind": "footprint_differs", "ref": fp.ref, "board": part.pattern,
                            "schematic": sch_fp})
    # A reference is duplicated when two different components show it; the
    # units of one multi-part component share theirs by design.
    owners: dict[str, set] = {}
    for node in design.nodes():
        for part in node.sheet.parts:
            ref = refs.get((node.kicad_path, part.uid), part.designator)
            if not ref or ref.startswith("#"):
                continue
            owner = (node.kicad_path, part.designator, part.libref) if part.part_count > 1 \
                else (node.kicad_path, part.uid)
            owners.setdefault(ref, set()).add(owner)
    for ref, who in sorted(owners.items()):
        if len(who) > 1:
            out.append({"kind": "duplicate_reference", "ref": ref, "count": len(who)})
    return out


@dataclass(frozen=True)
class Inputs:
    """Sources read and the output folder approved, before kicad-cli is needed."""

    sch_files: list[Path]
    board_file: Path | None
    prj: Path | None
    sheet_list: list[model.Sheet]
    name: str
    out: Path


def check_inputs(sources, output_dir: str | None = None, project_name: str | None = None,
                 overwrite: bool = False) -> Inputs:
    """Everything that can refuse the import without KiCad, so a bad call says
    what is wrong with it even where KiCad is not installed."""
    sch_files, board_file, prj = _gather(sources)
    sheet_list = []
    for f in sch_files:
        try:
            sheet_list.append(model.load_sheet(f))
        except ToolInputError:
            raise
        except Exception as exc:  # noqa: BLE001 - name the file that broke
            raise ToolInputError(f"Could not read {f.name}: {exc}",
                                 remedy="Check it is a .SchDoc saved by Altium Designer.") from exc
    name = project_name or _default_name(sheet_list, board_file, prj)
    first = (sch_files or [board_file])[0]
    out = Path(output_dir) if output_dir else first.parent / f"{name}_kicad"
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise ToolInputError(f"{out} already exists and is not empty.",
                             remedy="Pass another output_dir, or overwrite=True to write over it.")
    return Inputs(sch_files, board_file, prj, sheet_list, name, out)


def import_altium(install, sources, output_dir: str | None = None, project_name: str | None = None,
                  overwrite: bool = False) -> dict:
    return convert(install, check_inputs(sources, output_dir, project_name, overwrite))


def convert(install, inputs: Inputs) -> dict:
    sch_files, board_file, prj = inputs.sch_files, inputs.board_file, inputs.prj
    sheet_list, name, out = inputs.sheet_list, inputs.name, inputs.out
    out.mkdir(parents=True, exist_ok=True)

    board_parts = pcbdoc.read_board_parts(board_file) if board_file else None
    design = dz.build_design(sheet_list, name, board_parts, prj)

    pcb_path = out / f"{name}.kicad_pcb"
    board_text, board_refs, board_issues, joins, import_report = "", {}, [], [], None
    if board_file:
        import_report = bd.import_board(install, board_file, pcb_path)
        raw = pcb_path.read_text(encoding="utf-8")
        joins = bd.join_footprints(raw, board_parts)
        board_text, board_refs, board_issues = bd.rewrite_board(raw, joins, design)

    refs = dz.instance_references(design, board_refs)
    if board_file:
        fields: dict[str, dict[str, str]] = {}
        for node in design.nodes():
            for part in node.sheet.parts:
                ref = refs.get((node.kicad_path, part.uid), part.designator)
                fields.setdefault(ref, {}).update(
                    {name: text for name, (text, _) in symbols.parameter_fields(part).items()})
        board_text = bd.add_fields(board_text, fields)
    footprints_by_ref = {fp.ref: fp for fp, _ in joins}
    disagreements = _disagreements(design, refs, board_issues, footprints_by_ref, joins, bool(board_file))

    version = cli.schematic_format_version(install)
    board_values = {fp.ref: fp.value for fp in bd.footprints(board_text) if fp.ref and fp.value}
    # Where neither side has a value (an unresolved Altium comment), both show
    # the library reference, as KiCad's first update would make them.
    lone_values = {}
    for node in design.nodes():
        for part in node.sheet.parts:
            ref = refs.get((node.kicad_path, part.uid), part.designator)
            if ref and not part.value and ref not in board_values:
                lone_values.setdefault(ref, part.libref)
    board_text = bd.fill_values(board_text, lone_values)
    # Altium's project variants: on each symbol instance, each footprint and the project.
    prj_variants = variants.read_variants(prj)
    instance_changes, footprint_changes, variant_report = variants.plan(design, refs, prj_variants)
    variant_names = [(v.name, v.description) for v in prj_variants]
    board_text = bd.add_variants(board_text, variant_names, footprint_changes)
    written = sheets.write_sheets(design, out, refs, bd.footprint_fields(board_text), version,
                                  board_values, instance_changes)
    root_sch = out / f"{name}.kicad_sch"
    _project_file(out / f"{name}.kicad_pro", design, variant_names)

    known = {d["ref"] for d in disagreements if d.get("ref")}
    exclude = {d["ref"] for d in disagreements if d["kind"] in ("board_only", "schematic_only", "moved_sheet")}
    net_check, renames, parity = None, {}, None
    if board_file and sheet_list:
        sch_nets = verify.schematic_pin_nets(install, root_sch)
        net_check = verify.compare_nets(bd.board_pad_nets(board_text), sch_nets, exclude)
        renames = {b: s for b, s in net_check.pop("matched").items() if b != s}
        board_text = bd.rename_nets(board_text, renames)
        lone = {next(iter(pads)): net for net, pads in sch_nets.items()
                if len(pads) == 1 and next(iter(pads))[0] not in exclude}
        board_text = bd.assign_pad_nets(board_text, lone)
    if board_file:
        pcb_path.write_text(board_text, encoding="utf-8")
        parity = verify.parity(install, pcb_path, known, output=out / "parity.json")
    erc = cli.erc_violation_count(install, root_sch) if sheet_list else None

    issues = design.issues + written["issues"]
    if board_file and sheet_list:
        aligned = (not net_check["topology_differences"] and not parity["unexplained"]
                   and not parity.get("error")
                   and not any(i["kind"] in ("repeat_channel", "missing_sheet") for i in issues))
    else:
        aligned = None  # nothing to align with only one side
    report = {
        "output_dir": str(out),
        "project": str(out / f"{name}.kicad_pro"),
        "aligned": aligned,
        "sources": {"schematics": [str(f) for f in sch_files],
                    "board": str(board_file) if board_file else None,
                    "project": str(prj) if prj else None},
        "net_scope": design.mode,
        "counts": {"sheets": len({id(n.sheet) for n in design.nodes()}),
                   "sheet_instances": len(design.nodes()),
                   "symbols": sum(len(n.sheet.parts) for n in design.nodes()),
                   "footprints": len(joins)},
        "source_disagreements": disagreements,
        "issues": issues,
        "net_check": net_check,
        "net_renames": [{"board": b, "schematic": s} for b, s in sorted(renames.items())],
        "parity": parity,
        "erc_violations": erc,
        "approximations": written["approximations"],
        "not_converted": written["not_converted"],
        "inferred": design.inferred,
        "variants": variant_report,
        "unreferenced_sheets": design.unreferenced,
        "board_import": import_report and {k: import_report.get(k) for k in ("statistics", "errors", "warnings")},
        "files": written["files"] + ([str(pcb_path)] if board_file else []),
    }
    (out / "import_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                            encoding="utf-8")
    summary = dict(report)
    for key in ("source_disagreements", "issues", "net_renames", "approximations", "not_converted",
                "inferred", "files"):
        summary[key] = _cap(report[key])
    if net_check:
        summary["net_check"] = dict(net_check, topology_differences=_cap(net_check["topology_differences"]))
    if parity:
        summary["parity"] = dict(parity, unexplained=_cap(parity["unexplained"]))
    summary["report_file"] = str(out / "import_report.json")
    return summary
