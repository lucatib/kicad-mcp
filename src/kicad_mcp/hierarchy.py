"""Where a sheet sits in its project.

KiCad keys every symbol's annotation by project name and by the path of sheet
symbols leading to its sheet from the root schematic: `/<root uuid>` for the
root itself, `/<root uuid>/<sheet uuid>` one level down, and so on. A sheet
file used twice has one path per use, each with its own references. Writing
a sub-sheet as if it were a project of its own gets both wrong; KiCad papers
over it on load by copying the Reference field into every use, which hides
the mistake until a sheet is reused and both copies come out as the same part.

So before adding a symbol we find the project -- a `.kicad_pro` beside the
file or a few folders above it, whose root schematic's hierarchy includes the
file -- and read the paths from the sheet symbols. The same walk yields what
an edit needs to know about the other sheets: references already taken,
power nets, and labels, since a sheet on its own cannot see its siblings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr

#: How many folders above a sheet to look for its `.kicad_pro`; sub-sheets
#: usually sit beside the root or in a folder of their own under it.
PROJECT_SEARCH_DEPTH = 3


@dataclass
class SheetPlace:
    """A schematic file's project, and the instance paths it is used under."""

    path: Path
    project: str
    #: In hierarchy order. Empty when the file is its own root and has no uuid
    #: yet (a file about to be generated): the caller uses `/<its uuid>`.
    paths: list[str]
    #: The project's root schematic; None when the file belongs to none.
    root: Path | None = None
    #: Every other schematic in the hierarchy, parsed.
    others: dict[Path, sexpr.SExpr] = field(default_factory=dict)

    def taken_references(self) -> set[str]:
        """References used on the other sheets of the project."""
        out: set[str] = set()
        for tree in self.others.values():
            out |= symbol_references(tree, self.project)
        return out

    def power_nets(self) -> dict[str, str]:
        """Net name -> lib_id of the power symbols on the other sheets."""
        out: dict[str, str] = {}
        for tree in self.others.values():
            out.update(power_nets(tree))
        return out

    def global_labels(self) -> set[str]:
        out: set[str] = set()
        for tree in self.others.values():
            out |= label_names(tree, "global_label")
        return out

    def sheets_with_local_label(self, name: str) -> list[str]:
        """Other sheets (by file name) where `name` is a local label."""
        return self.sheets_with_label(name, "label")

    def sheets_with_label(self, name: str, kind: str) -> list[str]:
        """Other sheets (by file name) with a `kind` label named `name`."""
        return sorted(
            path.name for path, tree in self.others.items()
            if name in label_names(tree, kind)
        )


def locate(path: str | Path, tree: sexpr.SExpr | None = None) -> SheetPlace:
    """Find the project `path` belongs to and the paths it is used under.

    `tree` is the file's own content, when the caller already has it; the
    file need not exist yet. Outside any project the file is its own root:
    existing symbols' project and paths are kept, so editing a standalone
    sheet writes what it already holds.
    """
    path = Path(os.path.abspath(path))
    folders = [path.parent, *path.parent.parents][: PROJECT_SEARCH_DEPTH + 1]
    for folder in folders:
        for pro in sorted(folder.glob("*.kicad_pro")):
            root = pro.with_suffix(".kicad_sch")
            if not root.is_file():
                continue
            uses, trees = _walk(root)
            key = _norm(path)
            if key in uses:
                others = {p: t for k, (p, t) in trees.items() if k != key and t is not None}
                return SheetPlace(path, pro.stem, uses[key], root, others)
    return _standalone(path, tree)


def find_project_dir(path: str | Path) -> Path | None:
    """The folder of the `.kicad_pro` that `path` sits in or a few folders under.

    `path` is a file or a folder. This is what KiCad calls ${KIPRJMOD}: where
    the project's library tables live and what their entries are relative to.
    """
    path = Path(os.path.abspath(path))
    start = path if path.is_dir() else path.parent
    for folder in [start, *start.parents][: PROJECT_SEARCH_DEPTH + 1]:
        if any(folder.glob("*.kicad_pro")):
            return folder
    return None


def _walk(root: Path) -> tuple[dict[str, list[str]], dict[str, tuple[Path, sexpr.SExpr | None]]]:
    """Instance paths of every file under `root`, and each file parsed.

    Sheet file names are relative to the schematic holding the sheet symbol,
    as KiCad resolves them. Keys are case-folded, as Windows paths compare.
    """
    uses: dict[str, list[str]] = {}
    trees: dict[str, tuple[Path, sexpr.SExpr | None]] = {}

    def load(file: Path) -> sexpr.SExpr | None:
        key = _norm(file)
        if key not in trees:
            try:
                trees[key] = (file, sexpr.parse(file.read_text(encoding="utf-8")))
            except (OSError, SyntaxError, UnicodeDecodeError):
                trees[key] = (file, None)  # a missing or broken sheet: KiCad reports it
        return trees[key][1]

    def visit(file: Path, prefix: str, chain: tuple[str, ...]) -> None:
        tree = load(file)
        for sheet in sexpr.children(tree or [], "sheet"):
            name = _property(sheet, "Sheetfile") or _property(sheet, "Sheet file")
            uid = sexpr.value(sheet, "uuid")
            if not name or not uid:
                continue
            child = Path(os.path.abspath(file.parent / name))
            if _norm(child) in chain:
                continue  # a sheet that contains itself; KiCad refuses to load it
            sheet_path = f"{prefix}/{uid}"
            uses.setdefault(_norm(child), []).append(sheet_path)
            visit(child, sheet_path, chain + (_norm(child),))

    root_tree = load(root)
    if root_tree is None:
        return {}, {}
    root_path = f"/{sexpr.value(root_tree, 'uuid', '')}"
    uses[_norm(root)] = [root_path]
    visit(root, root_path, (_norm(root),))
    return uses, trees


def _standalone(path: Path, tree: sexpr.SExpr | None) -> SheetPlace:
    project, paths = None, []
    for symbol in sexpr.children(tree or [], "symbol"):
        for node in sexpr.children(sexpr.child(symbol, "instances") or [], "project"):
            if len(node) < 2:
                continue
            project = project or str(node[1])
            if node[1] != project:
                continue
            for p in sexpr.children(node, "path"):
                if len(p) > 1 and p[1] not in paths:
                    paths.append(str(p[1]))
    if project is None:
        # A project file beside it names the project it will join.
        pros = list(path.parent.glob("*.kicad_pro"))
        project = pros[0].stem if len(pros) == 1 else path.stem
    if not paths and tree is not None:
        paths = [f"/{sexpr.value(tree, 'uuid', '')}"]
    return SheetPlace(path, project, paths)


# --- facts about one sheet ------------------------------------------------


def symbol_references(tree: sexpr.SExpr, project: str) -> set[str]:
    """Every reference a sheet's symbols carry: fields and per-use annotation."""
    out: set[str] = set()
    for symbol in sexpr.children(tree, "symbol"):
        ref = _property(symbol, "Reference")
        if ref:
            out.add(ref)
        for node in sexpr.children(sexpr.child(symbol, "instances") or [], "project"):
            if len(node) > 1 and node[1] == project:
                for p in sexpr.children(node, "path"):
                    value = sexpr.value(p, "reference")
                    if value is not None:
                        out.add(str(value))
    return out


def power_nets(tree: sexpr.SExpr) -> dict[str, str]:
    """Net name (the Value) -> lib_id, for a sheet's global power symbols.

    KiCad 10 marks them `(power global)`, older files plain `(power)`;
    `(power local)` symbols connect within one sheet only, like a label.
    """
    definitions = {
        str(node[1]): node
        for node in sexpr.children(sexpr.child(tree, "lib_symbols") or [], "symbol")
        if len(node) > 1
    }
    out: dict[str, str] = {}
    for symbol in sexpr.children(tree, "symbol"):
        lib_id = str(sexpr.value(symbol, "lib_id", ""))
        definition = definitions.get(lib_id)
        if definition is not None and is_global_power(definition):
            value = _property(symbol, "Value")
            if value and value != "PWR_FLAG":
                out.setdefault(value, lib_id)
    return out


def is_global_power(definition: sexpr.SExpr) -> bool:
    flag = sexpr.child(definition, "power")
    return flag is not None and not (len(flag) > 1 and sexpr.as_text(flag[1]) == "local")


def label_names(tree: sexpr.SExpr, kind: str) -> set[str]:
    return {str(n[1]) for n in sexpr.children(tree, kind) if len(n) > 1}


def _property(node: sexpr.SExpr, name: str) -> str:
    for prop in sexpr.children(node, "property"):
        if len(prop) >= 3 and prop[1] == name:
            return str(prop[2])
    return ""


def _norm(path: Path) -> str:
    return os.path.normcase(str(path))
