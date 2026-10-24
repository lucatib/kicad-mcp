"""Finding KiCad without a registry entry, on a fake filesystem.

The registry is consulted first; these pin the fallback folder scan, which
is all that finds a copy the installer never registered.
"""

from __future__ import annotations

from kicad_mcp.discovery import WindowsResolver, find_installs


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


def test_prefers_the_newest_version_across_both_locations(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    _fake_install(local / "Programs" / "KiCad" / "9.0")
    newest = _fake_install(tmp_path / "PF" / "KiCad" / "10.0")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "none86"))

    assert find_installs(NoRegistry())[0].root == newest
