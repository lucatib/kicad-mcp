"""Unit conversion and error-translation tests.

Both are small modules where a silent mistake is expensive: a unit error
produces a plausible-looking wrong number, and a bad error translation turns a
one-line fix into a debugging session.
"""

from __future__ import annotations

from kicad_mcp import units
from kicad_mcp.errors import NoEditorOpenError, NotConnectedError
from kicad_mcp.session import translate


class FakePoint:
    def __init__(self, x, y):
        self.x, self.y = x, y


def test_nm_to_mm_and_back():
    assert units.nm_to_mm(1_000_000) == 1.0
    assert units.mm_to_nm(1.0) == 1_000_000
    assert units.mm_to_nm(2.54) == 2_540_000


def test_conversion_handles_none():
    assert units.nm_to_mm(None) is None
    assert units.mm_to_nm(None) is None
    assert units.point_to_mm(None) is None


def test_point_conversion():
    assert units.point_to_mm(FakePoint(152_400_000, -101_600_000)) == {"x": 152.4, "y": -101.6}


def test_mm_to_nm_rounds_rather_than_truncates():
    """Truncation would accumulate sub-nanometre drift across many placements."""
    assert units.mm_to_nm(0.0000009) == 1
    assert units.mm_to_nm(1.9999999) == 2_000_000


def test_no_handler_error_becomes_actionable():
    exc = translate(Exception("no handler available for request of type kiapi.common.commands.X"))
    assert isinstance(exc, NoEditorOpenError)
    assert "PCB editor" in exc.remedy


def test_connection_failure_becomes_actionable():
    exc = translate(Exception("Connection refused"))
    assert isinstance(exc, NotConnectedError)
    assert "IPC API server" in exc.remedy


def test_unknown_errors_pass_through_unchanged():
    original = ValueError("something else entirely")
    assert translate(original) is original


def test_error_serialises_with_remedy():
    d = NoEditorOpenError("nope").as_dict()
    assert d["error"] == "NoEditorOpenError"
    assert d["message"] == "nope"
    assert d["remedy"]
