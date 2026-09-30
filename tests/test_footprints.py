"""Footprint library resolution and lookup.

Checked against the real stock libraries plus a throwaway project-local one,
because the failure this exists to prevent is a footprint string that looks
right but names a library nickname the project never registered.
"""

from __future__ import annotations

import shutil

import pytest

from kicad_mcp.discovery import find_install
from kicad_mcp.errors import ToolInputError
from kicad_mcp.footprints import FootprintIndex

pytestmark = pytest.mark.requires_kicad

HLK = "Converter_ACDC:Converter_ACDC_Hi-Link_HLK-PMxx"


@pytest.fixture(scope="module")
def install():
    try:
        return find_install()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"KiCad not available: {exc}")


@pytest.fixture(scope="module")
def index(install):
    return FootprintIndex(install)


@pytest.fixture
def project(install, tmp_path):
    """A project registering one local library, `Local`, via ${KIPRJMOD}."""
    pretty = tmp_path / "libs" / "Mine.pretty"
    pretty.mkdir(parents=True)
    src = install.share_dir / "footprints" / "Converter_ACDC.pretty" / "Converter_ACDC_Hi-Link_HLK-PMxx.kicad_mod"
    shutil.copy(src, pretty / "My_PSU.kicad_mod")
    (tmp_path / "fp-lib-table").write_text(
        '(fp_lib_table\n  (version 7)\n'
        '  (lib (name "Local") (type "KiCad") (uri "${KIPRJMOD}/libs/Mine.pretty") (options "") (descr ""))\n)\n',
        encoding="utf-8",
    )
    return tmp_path


class TestSearch:
    def test_finds_stock_footprint_by_name_substring(self, index):
        hits = index.search("HLK-PM")
        assert HLK in [h["lib_id"] for h in hits]

    def test_search_is_case_insensitive(self, index):
        assert HLK in [h["lib_id"] for h in index.search("hlk-pm")]

    def test_library_filter_restricts_results(self, index):
        hits = index.search("SOIC-8", library="Package_SO")
        assert hits
        assert {h["library"] for h in hits} == {"Package_SO"}

    def test_limit_is_respected(self, index):
        assert len(index.search("0603", limit=3)) == 3

    def test_unknown_library_filter_raises_with_remedy(self, index):
        with pytest.raises(ToolInputError) as exc:
            index.search("x", library="No_Such_Library")
        assert exc.value.remedy

    def test_project_local_library_is_searchable(self, install, project):
        hits = FootprintIndex(install, project).search("My_PSU")
        assert [h["lib_id"] for h in hits] == ["Local:My_PSU"]


class TestExists:
    def test_stock_footprint_exists(self, index):
        assert index.exists(HLK)

    def test_missing_footprint_name(self, index):
        assert not index.exists("Converter_ACDC:Not_A_Real_Footprint")

    def test_unregistered_library_nickname(self, index):
        assert not index.exists("PCM_Nope:Converter_ACDC_Hi-Link_HLK-PMxx")

    def test_malformed_id(self, index):
        assert not index.exists("no-colon-here")

    def test_suggests_same_footprint_under_registered_nickname(self, install, project):
        # The real-world slip: the right footprint name under a library
        # nickname this project does not register.
        idx = FootprintIndex(install, project)
        assert idx.suggest("PCM_Espressif:My_PSU") == ["Local:My_PSU"]


class TestInfo:
    def test_reports_pads_description_and_tags(self, index):
        info = index.info(HLK)
        assert info["lib_id"] == HLK
        assert info["pad_count"] == 4
        assert sorted(info["pads"]) == ["1", "2", "3", "4"]
        assert info["description"]
        assert "path" in info

    def test_missing_footprint_raises_with_suggestions(self, install, project):
        idx = FootprintIndex(install, project)
        with pytest.raises(ToolInputError) as exc:
            idx.info("PCM_Espressif:My_PSU")
        assert "Local:My_PSU" in exc.value.remedy
