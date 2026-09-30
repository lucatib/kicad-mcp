"""Locate the KiCad installation and its bundled interpreter.

The interpreter is always derived from the discovered install rather than
hardcoded: KiCad 10 bundles CPython 3.11, but a future release bundling a
different version must not require a code change.

Windows only by design. `InstallResolver` is the single platform-aware seam --
widening scope later means adding a resolver, not threading sys.platform checks
through the package.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Protocol

from .errors import KicadNotFoundError, UnsupportedPlatformError

ENV_ROOT = "KICAD_MCP_KICAD_ROOT"


@dataclass(frozen=True)
class KiCadInstall:
    root: Path
    python_path: Path
    cli_path: Path
    version: str

    @property
    def major(self) -> int:
        m = re.match(r"(\d+)", self.version)
        return int(m.group(1)) if m else 0

    @property
    def share_dir(self) -> Path:
        return self.root / "share" / "kicad"

    def to_dict(self) -> dict:
        d = {k: str(v) for k, v in asdict(self).items()}
        d["major"] = self.major
        return d


def _install_from_root(root: Path) -> KiCadInstall | None:
    """Build an install record from a candidate root, or None if it isn't one."""
    bin_dir = root / "bin"
    python_path = bin_dir / "python.exe"
    cli_path = bin_dir / "kicad-cli.exe"
    if not (python_path.is_file() and cli_path.is_file()):
        return None
    return KiCadInstall(
        root=root,
        python_path=python_path,
        cli_path=cli_path,
        version=_version_from_root(root),
    )


def _version_from_root(root: Path) -> str:
    """Prefer the directory name (e.g. '10.0'); it is cheap and always present.

    Running kicad-cli --version would be exact but costs a subprocess on every
    discovery; the precise build is available from the live API when connected.
    """
    name = root.name
    return name if re.match(r"^\d+\.\d+", name) else "unknown"


def _version_key(install: KiCadInstall) -> tuple:
    parts = re.findall(r"\d+", install.version)
    return tuple(int(p) for p in parts) if parts else (0,)


class InstallResolver(Protocol):
    def candidates(self) -> list[Path]: ...


class WindowsResolver:
    """Registry first, then the conventional install folders.

    Both are consulted because a user may have installed KiCad without an
    uninstall entry (portable/extracted installs are common for side-by-side
    version testing). The folders are Program Files for an all-users install
    and %LOCALAPPDATA%\\Programs for "install for me only".
    """

    UNINSTALL_KEYS = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )

    def candidates(self) -> list[Path]:
        found: list[Path] = []
        found.extend(self._from_registry())
        found.extend(self._from_program_files())
        # Preserve order while removing duplicates.
        seen: set[Path] = set()
        unique = []
        for p in found:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    def _from_registry(self) -> list[Path]:
        try:
            import winreg  # noqa: PLC0415 - Windows-only import
        except ImportError:
            return []

        out: list[Path] = []
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for keypath in self.UNINSTALL_KEYS:
                try:
                    key = winreg.OpenKey(hive, keypath)
                except OSError:
                    continue
                with key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            sub = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, sub) as sk:
                                name, _ = winreg.QueryValueEx(sk, "DisplayName")
                                if "kicad" not in str(name).lower():
                                    continue
                                loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                        except OSError:
                            continue
                        if loc:
                            out.append(Path(loc))
        return out

    def _from_program_files(self) -> list[Path]:
        out: list[Path] = []
        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ]
        if os.environ.get("LOCALAPPDATA"):
            roots.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs"))
        for r in roots:
            base = Path(r) / "KiCad"
            if not base.is_dir():
                continue
            for child in base.iterdir():
                if child.is_dir():
                    out.append(child)
        return out


def default_resolver() -> InstallResolver:
    if sys.platform != "win32":
        raise UnsupportedPlatformError(
            f"Unsupported platform: {sys.platform}."
        )
    return WindowsResolver()


def find_installs(resolver: InstallResolver | None = None) -> list[KiCadInstall]:
    """All usable installs, highest version first."""
    resolver = resolver or default_resolver()
    installs = [
        inst
        for path in resolver.candidates()
        if (inst := _install_from_root(path)) is not None
    ]
    return sorted(installs, key=_version_key, reverse=True)


def find_install(resolver: InstallResolver | None = None) -> KiCadInstall:
    """The install to use: the env-var pin if set, else the highest version.

    The env var wins unconditionally so a user with several versions installed
    can pin one without uninstalling the others.
    """
    pinned = os.environ.get(ENV_ROOT)
    if pinned:
        inst = _install_from_root(Path(pinned))
        if inst is None:
            raise KicadNotFoundError(
                f"{ENV_ROOT} is set to {pinned!r}, but that directory does not "
                r"contain bin\python.exe and bin\kicad-cli.exe.",
                remedy=f"Correct {ENV_ROOT} or unset it to auto-detect.",
            )
        return inst

    installs = find_installs(resolver)
    if not installs:
        raise KicadNotFoundError("No KiCad installation found.")
    return installs[0]
