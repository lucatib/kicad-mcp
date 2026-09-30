"""Footprint library resolution and lookup.

The footprint counterpart of `symbols.py`, resolved through `fp-lib-table`
instead of `sym-lib-table`. A footprint library is a `.pretty` directory with
one `.kicad_mod` file per footprint, so listing names is a directory scan --
no parsing -- and only `info` opens a file.

The job that matters most is `exists`: a schematic's Footprint field is a bare
`Library:Name` string that nothing checks until the PCB is updated, and a
library nickname the project never registered fails silently until then.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from . import sexpr
from .errors import ToolInputError
from .symbols import LibraryEntry, resolve_tables

SUFFIX = ".kicad_mod"


class FootprintIndex:
    """Resolved view of the footprint libraries available to a project."""

    def __init__(self, install, project_dir: Path | None = None) -> None:
        self.install = install
        self.project_dir = Path(project_dir) if project_dir else None

    @property
    def entries(self) -> dict[str, LibraryEntry]:
        # Rebuilt per access, like SymbolIndex.entries: an index outlives the
        # tool call, and a stale table would hide a newly registered library.
        return resolve_tables(self.install, self.project_dir, "fp-lib-table")

    def library(self, name: str, entries: dict[str, LibraryEntry] | None = None) -> LibraryEntry:
        entries = entries if entries is not None else self.entries
        entry = entries.get(name)
        if entry is None:
            close = [n for n in entries if n.lower() == name.lower()]
            if not close:
                raise ToolInputError(
                    f"No footprint library named {name!r}.",
                    remedy="Search without a library filter to see which libraries hold a match.",
                )
            entry = entries[close[0]]
        if not _is_library(entry):
            raise ToolInputError(
                f"Footprint library {entry.name!r} is registered but its folder is missing: {entry.path}",
                remedy="Check the fp-lib-table entry or reinstall the libraries.",
            )
        return entry

    def search(self, query: str, limit: int = 50, library: str | None = None) -> list[dict]:
        """Case-insensitive substring search over footprint names."""
        q = query.lower()
        entries = self.entries
        libs = [self.library(library, entries)] if library else list(entries.values())
        results: list[dict] = []
        for entry in libs:
            if not _is_library(entry):
                continue
            for name in _footprint_names(entry.path):
                if q in name.lower():
                    results.append({
                        "lib_id": f"{entry.name}:{name}",
                        "library": entry.name,
                        "footprint": name,
                    })
                    if len(results) >= limit:
                        return results
        return results

    def exists(self, lib_id: str) -> bool:
        return self._file(lib_id, self.entries) is not None

    def suggest(self, lib_id: str) -> list[str]:
        """The same footprint name under libraries that do resolve."""
        name = lib_id.split(":", 1)[-1]
        out = []
        for entry in self.entries.values():
            if _is_library(entry) and name in _footprint_names(entry.path):
                out.append(f"{entry.name}:{name}")
        return out

    def info(self, lib_id: str) -> dict:
        """Description, tags, attributes and pads of one library footprint."""
        path = self._file(lib_id, self.entries)
        if path is None:
            alternatives = self.suggest(lib_id)
            remedy = (
                f"Same footprint under a registered library: {', '.join(alternatives)}"
                if alternatives else
                "Call search_footprints to find the right Library:Name."
            )
            raise ToolInputError(f"Footprint {lib_id!r} does not resolve.", remedy=remedy)

        tree = sexpr.parse(path.read_text(encoding="utf-8"))
        pads = [str(p[1]) for p in sexpr.children(tree, "pad") if len(p) > 1]
        numbered = [p for p in pads if p]
        return {
            "lib_id": lib_id,
            "path": str(path),
            "description": str(sexpr.value(tree, "descr", "")),
            "tags": str(sexpr.value(tree, "tags", "")),
            "attributes": [sexpr.as_text(a) for a in sexpr.values(tree, "attr")],
            # Pad numbers repeat (thermal vias, split pads): report each once,
            # in file order, since that is what a symbol's pin numbers map to.
            "pads": list(dict.fromkeys(numbered)),
            "pad_count": len(set(numbered)),
            "unnumbered_pads": len(pads) - len(numbered),
        }

    def _file(self, lib_id: str, entries: dict[str, LibraryEntry]) -> Path | None:
        if ":" not in lib_id:
            return None
        lib, name = lib_id.split(":", 1)
        entry = entries.get(lib)
        if entry is None or not _is_library(entry):
            return None
        path = entry.path / f"{name}{SUFFIX}"
        return path if path.is_file() else None


def footprint_warning(footprints: FootprintIndex, footprint: str) -> str | None:
    """Why a Footprint field value will not survive Update PCB, or None if it will."""
    if not footprint or footprints.exists(footprint):
        return None
    alternatives = footprints.suggest(footprint)
    hint = (
        f" Same footprint under a registered library: {', '.join(alternatives)}."
        if alternatives else " Call search_footprints to find the right Library:Name."
    )
    return (
        f"Footprint {footprint!r} does not resolve through this project's fp-lib-table; "
        f"it was written anyway.{hint}"
    )


def _is_library(entry: LibraryEntry) -> bool:
    return entry.path is not None and entry.path.is_dir()


def _footprint_names(path: Path) -> list[str]:
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return []
    return _footprint_names_cached(str(path), stamp)


@lru_cache(maxsize=512)
def _footprint_names_cached(path_str: str, stamp: int) -> list[str]:
    # Keyed on the directory's mtime, which changes when a footprint is added
    # or removed -- the same invalidation idea as symbols._parse_library.
    with os.scandir(path_str) as it:
        return sorted(
            e.name[: -len(SUFFIX)] for e in it if e.is_file() and e.name.endswith(SUFFIX)
        )
