"""Locate the KiCad installation and its bundled interpreter.

The interpreter is always derived from the discovered install rather than
hardcoded: KiCad 10 bundles CPython 3.11, but a future release bundling a
different version must not require a code change.

`InstallResolver` is the single platform-aware seam: where KiCad installs, how
an install is laid out, and where it keeps its per-user files. Supporting a
platform means adding a resolver, not threading sys.platform checks through the
package.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import sys
from dataclasses import dataclass, asdict
from functools import lru_cache
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
    #: The stock symbols, footprints, 3D models and templates.
    share_dir: Path

    @property
    def major(self) -> int:
        m = re.match(r"(\d+)", self.version)
        return int(m.group(1)) if m else 0

    def to_dict(self) -> dict:
        d = {k: str(v) for k, v in asdict(self).items()}
        d["major"] = self.major
        return d


def _version_key(install: KiCadInstall) -> tuple:
    parts = re.findall(r"\d+", install.version)
    return tuple(int(p) for p in parts) if parts else (0,)


def _dedupe(paths: list[Path]) -> list[Path]:
    """Preserve order while removing duplicates."""
    seen: set[Path] = set()
    unique = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


class InstallResolver(Protocol):
    def candidates(self) -> list[Path]: ...

    def install_from(self, root: Path) -> KiCadInstall | None:
        """An install record for a candidate root, or None if it isn't one."""
        ...

    def config_root(self) -> Path | None:
        """Where KiCad keeps its settings, one `<major>.<minor>` folder per version."""
        ...

    def documents_root(self) -> Path:
        """Where KiCad keeps user content, such as `<major>.0/3rdparty`."""
        ...

    def cache_root(self) -> Path:
        """Where this server may keep caches of its own."""
        ...


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
        return _dedupe([*self._from_registry(), *self._from_program_files()])

    def install_from(self, root: Path) -> KiCadInstall | None:
        bin_dir = root / "bin"
        python_path = bin_dir / "python.exe"
        cli_path = bin_dir / "kicad-cli.exe"
        if not (python_path.is_file() and cli_path.is_file()):
            return None
        return KiCadInstall(
            root=root,
            python_path=python_path,
            cli_path=cli_path,
            version=self._version_from_root(root),
            share_dir=root / "share" / "kicad",
        )

    @staticmethod
    def _version_from_root(root: Path) -> str:
        """Prefer the directory name (e.g. '10.0'); it is cheap and always present.

        Running kicad-cli --version would be exact but costs a subprocess on every
        discovery; the precise build is available from the live API when connected.
        """
        name = root.name
        return name if re.match(r"^\d+\.\d+", name) else "unknown"

    def config_root(self) -> Path | None:
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "kicad" if appdata else None

    def documents_root(self) -> Path:
        home = os.environ.get("USERPROFILE")
        return (Path(home) if home else Path.home()) / "Documents" / "KiCad"

    def cache_root(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".")

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


class LinuxResolver:
    """Distribution packages and source builds: a prefix such as /usr.

    KiCad's Python module goes into the system interpreter's site-packages, so
    that interpreter is the one to build the venv from. The prefix carries no
    version in its name, so kicad-cli is asked once and the answer cached.
    Flatpak and AppImage builds keep kicad-cli and pcbnew inside their sandbox
    and are not found; KICAD_MCP_KICAD_ROOT can still name an extracted prefix.
    """

    PREFIXES = (Path("/usr"), Path("/usr/local"))

    def candidates(self) -> list[Path]:
        found: list[Path] = []
        on_path = shutil.which("kicad-cli")
        if on_path:
            found.append(Path(on_path).parent.parent)
        found.extend(self.PREFIXES)
        return _dedupe(found)

    def install_from(self, root: Path) -> KiCadInstall | None:
        cli_path = root / "bin" / "kicad-cli"
        if not cli_path.is_file():
            return None
        python_path = root / "bin" / "python3"
        return KiCadInstall(
            root=root,
            python_path=python_path if python_path.is_file() else Path("/usr/bin/python3"),
            cli_path=cli_path,
            version=cli_version(cli_path),
            share_dir=root / "share" / "kicad",
        )

    def config_root(self) -> Path | None:
        return _xdg("XDG_CONFIG_HOME", ".config") / "kicad"

    def documents_root(self) -> Path:
        return _xdg("XDG_DATA_HOME", ".local/share") / "kicad"

    def cache_root(self) -> Path:
        return _xdg("XDG_CACHE_HOME", ".cache")


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    # The spec says a relative value is invalid and must be ignored.
    return Path(value) if value and os.path.isabs(value) else Path.home() / default


class MacResolver:
    """The KiCad.app bundle, wherever it was dragged.

    The official disk image puts it in /Applications/KiCad/ beside the demos;
    some users drag the app alone, or into ~/Applications. Everything lives in
    the bundle: kicad-cli, the bundled Python framework, and the stock libraries
    under SharedSupport.
    """

    def application_dirs(self) -> list[Path]:
        return [Path("/Applications"), Path.home() / "Applications"]

    def candidates(self) -> list[Path]:
        found: list[Path] = []
        for apps in self.application_dirs():
            found.append(apps / "KiCad.app")
            # KiCad/, and side-by-side copies such as "KiCad 9".
            found.extend(sorted(apps.glob("KiCad*/KiCad.app")))
        return _dedupe(found)

    def install_from(self, root: Path) -> KiCadInstall | None:
        if (root / "KiCad.app").is_dir():
            root = root / "KiCad.app"  # the folder holding the bundle, as a pin
        contents = root / "Contents"
        cli_path = contents / "MacOS" / "kicad-cli"
        if not cli_path.is_file():
            return None
        return KiCadInstall(
            root=root,
            python_path=(contents / "Frameworks" / "Python.framework" / "Versions"
                         / "Current" / "bin" / "python3"),
            cli_path=cli_path,
            version=self._bundle_version(contents / "Info.plist"),
            share_dir=contents / "SharedSupport",
        )

    @staticmethod
    def _bundle_version(plist: Path) -> str:
        try:
            with plist.open("rb") as f:
                version = str(plistlib.load(f).get("CFBundleShortVersionString", ""))
        except (OSError, plistlib.InvalidFileException, ValueError):
            return "unknown"
        return version if re.match(r"^\d+\.\d+", version) else "unknown"

    def config_root(self) -> Path | None:
        return Path.home() / "Library" / "Preferences" / "kicad"

    def documents_root(self) -> Path:
        return Path.home() / "Documents" / "KiCad"

    def cache_root(self) -> Path:
        return Path.home() / "Library" / "Caches"


def cli_version(cli_path: Path) -> str:
    """The version kicad-cli reports, e.g. '10.0.6', or 'unknown'.

    Asked once per binary -- a subprocess costs ~0.1 s -- and cached by its
    modification time so an upgrade in place is noticed.
    """
    try:
        stamp = cli_path.stat().st_mtime_ns
    except OSError:
        return "unknown"
    return _cli_version_cached(str(cli_path), stamp)


@lru_cache(maxsize=4)
def _cli_version_cached(cli_path: str, stamp: int) -> str:
    from types import SimpleNamespace  # noqa: PLC0415

    from .cli import run_cli  # noqa: PLC0415 - cli is heavier; only needed here
    from .errors import CliError  # noqa: PLC0415

    try:
        result = run_cli(SimpleNamespace(cli_path=Path(cli_path)), ["version"], timeout=30)
    except CliError:
        return "unknown"
    match = re.search(r"\d+\.\d+(?:\.\d+)?", result["stdout"])
    return match.group(0) if match else "unknown"


def default_resolver() -> InstallResolver:
    if sys.platform == "win32":
        return WindowsResolver()
    if sys.platform.startswith("linux"):
        return LinuxResolver()
    if sys.platform == "darwin":
        return MacResolver()
    raise UnsupportedPlatformError(f"Unsupported platform: {sys.platform}.")


def user_config_root() -> Path | None:
    return default_resolver().config_root()


def user_documents_root() -> Path:
    return default_resolver().documents_root()


def user_cache_root() -> Path:
    return default_resolver().cache_root()


def find_installs(resolver: InstallResolver | None = None) -> list[KiCadInstall]:
    """All usable installs, highest version first."""
    resolver = resolver or default_resolver()
    installs = [
        inst
        for path in resolver.candidates()
        if (inst := resolver.install_from(path)) is not None
    ]
    return sorted(installs, key=_version_key, reverse=True)


def find_install(resolver: InstallResolver | None = None) -> KiCadInstall:
    """The install to use: the env-var pin if set, else the highest version.

    The env var wins unconditionally so a user with several versions installed
    can pin one without uninstalling the others.
    """
    resolver = resolver or default_resolver()
    pinned = os.environ.get(ENV_ROOT)
    if pinned:
        inst = resolver.install_from(Path(pinned))
        if inst is None:
            raise KicadNotFoundError(
                f"{ENV_ROOT} is set to {pinned!r}, but no KiCad install was found "
                "there (no kicad-cli where this platform puts it).",
                remedy=f"Correct {ENV_ROOT} or unset it to auto-detect.",
            )
        return inst

    installs = find_installs(resolver)
    if not installs:
        raise KicadNotFoundError("No KiCad installation found.")
    return installs[0]
