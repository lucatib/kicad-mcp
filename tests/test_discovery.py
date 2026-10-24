"""Finding KiCad on each platform, on a fake filesystem.

Every resolver is exercised on every host: they only look at files, so a
Windows tree can be checked on Linux and a KiCad.app bundle on Windows. On
Windows the registry is consulted first; these pin the fallback folder scan,
which is all that finds a copy the installer never registered.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from kicad_mcp import discovery
from kicad_mcp.discovery import (
    LinuxResolver,
    MacResolver,
    WindowsResolver,
    find_install,
    find_installs,
)
from kicad_mcp.errors import KicadNotFoundError


def _fake_install(root):
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python.exe").write_text("")
    (root / "bin" / "kicad-cli.exe").write_text("")
    return root


class NoRegistry(WindowsResolver):
    def _from_registry(self):
        return []


def test_finds_a_per_user_install_under_local_appdata(tmp_path, monkeypatch):
    # "Install for me only" puts KiCad here instead of Program Files.
    local = tmp_path / "Local"
    root = _fake_install(local / "Programs" / "KiCad" / "10.0")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "none86"))

    installs = find_installs(NoRegistry())

    assert [i.root for i in installs] == [root]
    assert installs[0].python_path == root / "bin" / "python.exe"
    assert installs[0].share_dir == root / "share" / "kicad"


def test_prefers_the_newest_version_across_both_locations(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    _fake_install(local / "Programs" / "KiCad" / "9.0")
    newest = _fake_install(tmp_path / "PF" / "KiCad" / "10.0")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "none86"))

    assert find_installs(NoRegistry())[0].root == newest


def test_windows_user_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    r = WindowsResolver()

    assert r.config_root() == tmp_path / "Roaming" / "kicad"
    assert r.documents_root() == tmp_path / "me" / "Documents" / "KiCad"
    assert r.cache_root() == tmp_path / "Local"


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows resolver is chosen on Windows")
def test_windows_still_uses_the_windows_resolver():
    assert isinstance(discovery.default_resolver(), WindowsResolver)


# --- Linux ------------------------------------------------------------------


def _fake_prefix(root, python=True):
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "kicad-cli").write_text("")
    if python:
        (root / "bin" / "python3").write_text("")
    (root / "share" / "kicad").mkdir(parents=True)
    return root


@pytest.fixture
def cli_says(monkeypatch):
    """kicad-cli's version answer, without running anything."""
    def set_version(version):
        monkeypatch.setattr(discovery, "cli_version", lambda path: version)
    set_version("10.0.6")
    return set_version


def test_linux_prefix_is_an_install(tmp_path, cli_says):
    root = _fake_prefix(tmp_path / "usr")

    inst = LinuxResolver().install_from(root)

    assert inst.cli_path == root / "bin" / "kicad-cli"
    assert inst.python_path == root / "bin" / "python3"
    assert inst.share_dir == root / "share" / "kicad"
    assert inst.version == "10.0.6" and inst.major == 10


def test_linux_falls_back_to_the_system_python(tmp_path, cli_says):
    # A /usr/local source build installs pcbnew for the system interpreter.
    inst = LinuxResolver().install_from(_fake_prefix(tmp_path / "local", python=False))

    assert inst.python_path == Path("/usr/bin/python3")


def test_linux_prefix_without_kicad_cli_is_not_an_install(tmp_path, cli_says):
    (tmp_path / "usr" / "bin").mkdir(parents=True)

    assert LinuxResolver().install_from(tmp_path / "usr") is None


def test_linux_finds_kicad_cli_on_path(tmp_path, monkeypatch, cli_says):
    root = _fake_prefix(tmp_path / "opt" / "kicad")
    monkeypatch.setattr(discovery.shutil, "which", lambda name: str(root / "bin" / name))

    class NoPrefixes(LinuxResolver):
        PREFIXES = ()

    assert [i.root for i in find_installs(NoPrefixes())] == [root]


def test_linux_user_folders_follow_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    r = LinuxResolver()

    assert r.config_root() == tmp_path / "cfg" / "kicad"
    assert r.documents_root() == tmp_path / "data" / "kicad"
    assert r.cache_root() == tmp_path / "cache"


def test_linux_user_folders_default_under_home(monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    r = LinuxResolver()

    assert r.config_root() == Path.home() / ".config" / "kicad"
    assert r.documents_root() == Path.home() / ".local" / "share" / "kicad"
    assert r.cache_root() == Path.home() / ".cache"


def test_kicad_cli_version_is_parsed(tmp_path, monkeypatch):
    cli = tmp_path / "kicad-cli"
    cli.write_text("")
    monkeypatch.setattr("kicad_mcp.cli.run_cli",
                        lambda install, args, timeout: {"stdout": "10.0.6\n", "returncode": 0})
    discovery._cli_version_cached.cache_clear()

    assert discovery.cli_version(cli) == "10.0.6"
    assert discovery.cli_version(tmp_path / "missing") == "unknown"


# --- macOS ------------------------------------------------------------------


def _fake_bundle(app, version="10.0.6"):
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "MacOS" / "kicad-cli").write_text("")
    (contents / "SharedSupport").mkdir()
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version}, f)
    return app


class FakeApplications(MacResolver):
    def __init__(self, *dirs):
        self.dirs = list(dirs)

    def application_dirs(self):
        return self.dirs


def test_mac_bundle_is_an_install(tmp_path):
    app = _fake_bundle(tmp_path / "KiCad" / "KiCad.app")

    inst = MacResolver().install_from(app)

    assert inst.root == app
    assert inst.cli_path == app / "Contents" / "MacOS" / "kicad-cli"
    assert inst.share_dir == app / "Contents" / "SharedSupport"
    assert inst.python_path.parts[-6:] == (
        "Frameworks", "Python.framework", "Versions", "Current", "bin", "python3")
    assert inst.version == "10.0.6"


def test_mac_folder_holding_the_bundle_is_accepted(tmp_path):
    # KICAD_MCP_KICAD_ROOT=/Applications/KiCad, as the disk image lays it out.
    app = _fake_bundle(tmp_path / "KiCad" / "KiCad.app")

    assert MacResolver().install_from(tmp_path / "KiCad").root == app


def test_mac_finds_bundles_side_by_side_newest_first(tmp_path):
    apps, user_apps = tmp_path / "Applications", tmp_path / "home" / "Applications"
    _fake_bundle(apps / "KiCad 9" / "KiCad.app", "9.0.4")
    newest = _fake_bundle(apps / "KiCad" / "KiCad.app", "10.0.6")
    loose = _fake_bundle(user_apps / "KiCad.app", "10.0.1")

    installs = find_installs(FakeApplications(apps, user_apps))

    assert [i.version for i in installs] == ["10.0.6", "10.0.1", "9.0.4"]
    assert installs[0].root == newest and installs[1].root == loose


def test_mac_unreadable_plist_gives_unknown_version(tmp_path):
    app = _fake_bundle(tmp_path / "KiCad.app")
    (app / "Contents" / "Info.plist").write_text("not a plist")

    assert MacResolver().install_from(app).version == "unknown"


def test_mac_user_folders():
    r = MacResolver()

    assert r.config_root() == Path.home() / "Library" / "Preferences" / "kicad"
    assert r.documents_root() == Path.home() / "Documents" / "KiCad"
    assert r.cache_root() == Path.home() / "Library" / "Caches"


# --- the pin ----------------------------------------------------------------


def test_the_env_var_pin_wins(tmp_path, monkeypatch):
    app = _fake_bundle(tmp_path / "Pinned" / "KiCad.app", "9.0.4")
    monkeypatch.setenv(discovery.ENV_ROOT, str(tmp_path / "Pinned"))

    assert find_install(FakeApplications()).root == app


def test_a_pin_to_nothing_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(discovery.ENV_ROOT, str(tmp_path))

    with pytest.raises(KicadNotFoundError, match="does not contain KiCad.app"):
        find_install(FakeApplications())


def test_a_windows_pin_to_nothing_keeps_its_message(tmp_path, monkeypatch):
    monkeypatch.setenv(discovery.ENV_ROOT, str(tmp_path))

    with pytest.raises(KicadNotFoundError,
                       match=r"does not contain bin\\python\.exe and bin\\kicad-cli\.exe\.$"):
        find_install(NoRegistry())
