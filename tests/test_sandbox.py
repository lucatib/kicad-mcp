"""run_kicad_script when the live connection is gone, and its commit helper.

A script that touches `board` while KiCad is unreachable used to die with
"'NoneType' object has no attribute ..." -- true, but it hides the reason the
board is None. The connection error has to reach the caller.
"""

from __future__ import annotations

import pytest

from kicad_mcp import sandbox, session
from kicad_mcp.errors import NotConnectedError


@pytest.fixture
def offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise NotConnectedError("Could not reach KiCad: Timed out")

    monkeypatch.setattr(sandbox.SESSION, "connect", refuse)
    monkeypatch.setattr(sandbox.SESSION, "board", refuse)


def test_a_script_failing_on_a_missing_board_says_why_the_board_is_missing(offline):
    out = sandbox.run_script("board.get_shapes()")

    assert out["ok"] is False
    assert "Timed out" in out["live_connection"]["message"]
    assert out["live_connection"]["remedy"]


def test_the_connection_error_is_visible_to_the_script(offline):
    out = sandbox.run_script("result = connection_error")

    assert "Timed out" in out["result"]


def test_a_script_that_succeeds_offline_carries_no_connection_noise(offline):
    out = sandbox.run_script("result = 1 + 1")

    assert out["ok"] is True
    assert "live_connection" not in out


class FakeBoard:
    def __init__(self):
        self.log = []

    def begin_commit(self):
        self.log.append("begin")
        return "c1"

    def push_commit(self, commit, message=""):
        self.log.append(("push", commit, message))

    def drop_commit(self, commit):
        self.log.append(("drop", commit))


@pytest.fixture
def live(monkeypatch):
    board = FakeBoard()
    monkeypatch.setattr(sandbox.SESSION, "connect", lambda *a, **k: object())
    monkeypatch.setattr(sandbox.SESSION, "board", lambda: board)
    return board


def test_board_commit_pushes_one_undo_step(live):
    out = sandbox.run_script("with board_commit('Move J1'):\n    pass")

    assert out["ok"] is True
    assert live.log == ["begin", ("push", "c1", "Move J1")]


def test_board_commit_drops_the_commit_when_the_script_raises(live):
    out = sandbox.run_script("with board_commit('x'):\n    raise ValueError('bad')")

    assert out["ok"] is False
    assert live.log == ["begin", ("drop", "c1")]


def test_board_commit_without_a_board_names_the_connection_error(offline):
    out = sandbox.run_script("with board_commit('x'):\n    pass")

    assert "NotConnectedError" in out["traceback"]
    assert "Timed out" in out["traceback"]


class FakeKiCad:
    made = 0

    def __init__(self):
        FakeKiCad.made += 1
        self.alive = True

    def get_version(self):
        if not self.alive:
            raise ConnectionError("Error receiving reply from KiCad: Timed out")
        return "10.0.6"


def test_a_dead_cached_client_is_replaced_not_returned(monkeypatch):
    import kipy

    monkeypatch.setattr(kipy, "KiCad", FakeKiCad)
    s = session.Session()
    first = s.connect()
    first.alive = False

    second = s.connect()

    assert second is not first
    assert second.alive
