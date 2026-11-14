"""The router with a board's own rules, kept copper, and tracks that follow a moved footprint."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp import autoroute, drc_rules

S = autoroute.Shape
DRU = """(version 1)
# a comment, with a ' quote
(rule "HV to ground"
  (constraint clearance (min 5mm))
  (condition "A.NetName == 'HV*' && B.NetName == 'GND'"))
(rule "Ground on the back"
  (layer "F.Cu")
  (constraint disallow track)
  (condition "A.NetName == 'GND'"))
(rule "No vias" (constraint disallow via) (condition "A.Type == 'Via'"))
(rule "Socket" (constraint clearance (min 1.2mm))
  (condition "A.Type == 'Pad' && B.Type == 'Pad' && A.memberOfFootprint('V?') && B.memberOfFootprint('V?')"))
(rule "Odd" (constraint clearance (min 9mm)) (condition "A.intersectsCourtyard('X')"))
"""


class TestRules:
    rules = drc_rules.parse(DRU)

    def test_pairwise_clearance_either_way_round(self):
        hv = drc_rules.Item("Track", "HV1", "HV", "F.Cu")
        gnd = drc_rules.Item("Pad", "GND", "GND", None, "J1", "Through-hole")
        assert self.rules.value("clearance", hv, gnd) == 5.0
        assert self.rules.value("clearance", gnd, hv) == 5.0
        assert self.rules.value("clearance", hv, drc_rules.Item("Pad", "SIG")) is None

    def test_later_rule_wins(self):
        a = drc_rules.Item("Pad", "HV1", "HV", None, "V1", "Through-hole")
        b = drc_rules.Item("Pad", "GND", "GND", None, "V1", "Through-hole")
        assert self.rules.value("clearance", a, b) == 1.2

    def test_layer_and_disallow(self):
        assert self.rules.disallowed("track", drc_rules.Item("Track", "GND", "GND", "F.Cu"))
        assert not self.rules.disallowed("track", drc_rules.Item("Track", "GND", "GND", "B.Cu"))
        assert self.rules.disallowed("via", drc_rules.Item("Via", "SIG"))

    def test_unknown_function_is_skipped_and_reported(self):
        rules = drc_rules.parse(DRU)
        rules.value("clearance", drc_rules.Item("Track", "SIG"), drc_rules.Item("Pad", "X"))
        assert any("intersectsCourtyard" in s for s in rules.skipped)

    def test_unknown_side_of_an_or_does_not_sink_a_true_one(self):
        r = drc_rules.parse("(rule x (constraint clearance (min 3mm)) "
                            "(condition \"A.intersectsCourtyard('X') || A.NetName == 'N'\"))")
        assert r.value("clearance", drc_rules.Item("Track", "N"), drc_rules.Item("Pad", "M")) == 3.0


def _pad(net, name, x, y, r=0.5, group=None):
    return autoroute.Pad(net, name, x, y, S("circle", (x, y, r), (0, 1), net), group)


class TestGroups:
    def test_joined_pads_need_no_track_between_them(self):
        # A.1 and A.2 are joined by kept copper; only B.1 needs a track.
        pads = [_pad("N", "A.1", 2, 5, group=1), _pad("N", "A.2", 6, 5, group=1), _pad("N", "B.1", 14, 5)]
        r = autoroute.route((0, 0, 16, 10), pads, grid=0.5)
        assert not r.failed
        xs = sorted(x for t in r.tracks for x in (t[2], t[4]))
        assert xs[0] >= 6  # starts from A.2, the nearer end of the group

    def test_net_already_joined_is_left_alone(self):
        pads = [_pad("N", "A.1", 2, 5, group=1), _pad("N", "A.2", 6, 5, group=1)]
        assert autoroute.route((0, 0, 16, 10), pads, grid=0.5).tracks == []

    def test_a_walled_in_pad_does_not_stop_the_others(self):
        wall = [S("circle", (12, 5, 3.0), (0, 1), "W")]
        pads = [_pad("N", "A.1", 2, 2), _pad("N", "B.1", 2, 8), _pad("N", "C.1", 12, 5, r=0.3)]
        r = autoroute.route((0, 0, 20, 10), pads, wall, grid=0.5)
        assert r.failed == [("N", "C.1")]
        assert r.tracks


class TestPolicy:
    def test_layer_and_via_rules_are_kept(self):
        class FrontOnly(autoroute.Policy):
            def layer_ok(self, net, layer):
                return layer == 0

        pads = [_pad("N", "A.1", 2, 5), _pad("N", "B.1", 14, 5)]
        r = autoroute.route((0, 0, 16, 10), pads, grid=0.5, policy=FrontOnly(allow_vias=False))
        assert r.tracks and all(t[1] == 0 for t in r.tracks) and not r.vias

    def test_pairwise_clearance_detours(self):
        class Wide(autoroute.Policy):
            def clearance(self, net, shape):
                return 3.0 if shape.net == "GND" else 0.2

        gnd = S("circle", (8, 5, 0.5), (0, 1), "GND")
        pads = [_pad("N", "A.1", 2, 5), _pad("N", "B.1", 14, 5)]
        r = autoroute.route((0, 0, 16, 12), pads, [gnd], grid=0.5, layers=1, policy=Wide())
        assert not r.failed
        near = min(gnd.distance(x, y) for t in r.tracks for x, y in ((t[2], t[3]), (t[4], t[5])))
        assert near >= 3.0 - 1e-6


# --- on a real board ---------------------------------------------------------

@pytest.fixture
def board(tmp_path: Path):
    pcbnew = pytest.importorskip("pcbnew")
    mm = pcbnew.FromMM
    pcb = pcbnew.BOARD()
    for (x1, y1), (x2, y2) in (((0, 0), (40, 0)), ((40, 0), (40, 20)), ((40, 20), (0, 20)), ((0, 20), (0, 0))):
        s = pcbnew.PCB_SHAPE(pcb)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetStart(pcbnew.VECTOR2I(mm(x1), mm(y1)))
        s.SetEnd(pcbnew.VECTOR2I(mm(x2), mm(y2)))
        pcb.Add(s)
    net = pcbnew.NETINFO_ITEM(pcb, "N")
    pcb.Add(net)
    for ref, x in (("J1", 5), ("J2", 35)):
        fp = pcbnew.FOOTPRINT(pcb)
        fp.SetReference(ref)
        pcb.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(mm(2), mm(2)))
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(1), mm(1)))
        pad.SetLayerSet(pcbnew.PAD.PTHMask())
        fp.Add(pad)
        fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(10)))
        pad.SetNet(net)
    t = pcbnew.PCB_TRACK(pcb)
    t.SetStart(pcbnew.VECTOR2I(mm(5), mm(10)))
    t.SetEnd(pcbnew.VECTOR2I(mm(15), mm(10)))
    t.SetWidth(mm(0.5))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(net)
    pcb.Add(t)
    path = tmp_path / "t.kicad_pcb"
    (tmp_path / "t.kicad_pro").write_text("{}")
    pcbnew.SaveBoard(str(path), pcb)
    return path


@pytest.mark.requires_kicad
def test_keep_existing_adds_only_what_is_missing(board):
    from kicad_mcp import board_edit
    out = board_edit.route(board, keep_existing=True, grid=0.5)
    assert out["taken_up"] == 0 and out["nets_failed"] == 0 and out["tracks"] >= 1
    pcbnew = pytest.importorskip("pcbnew")
    spans = [tuple(sorted((pcbnew.ToMM(t.GetStart().x), pcbnew.ToMM(t.GetEnd().x))))
             for t in pcbnew.LoadBoard(str(board)).GetTracks()]
    assert (5.0, 15.0) in spans  # the old track is still there
    # The new copper starts on the kept track, not back at J1.
    new = [s for s in spans if s != (5.0, 15.0)]
    assert new and min(s[0] for s in new) > 5.0


@pytest.mark.requires_kicad
def test_rules_file_keeps_tracks_off_a_layer(board):
    from kicad_mcp import board_edit
    board.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "front free" (layer "F.Cu") (constraint disallow track) (condition "A.NetName == \'N\'"))\n')
    out = board_edit.route(board, grid=0.5)
    assert out["taken_up"] == 1 and out["nets_failed"] == 0 and out["custom_rules"] == 1
    pcbnew = pytest.importorskip("pcbnew")
    assert {t.GetLayer() for t in pcbnew.LoadBoard(str(board)).GetTracks()} == {pcbnew.B_Cu}


@pytest.mark.requires_kicad
def test_dry_run_writes_nothing(board):
    from kicad_mcp import board_edit
    before = board.read_bytes()
    out = board_edit.route(board, grid=0.5, dry_run=True)
    assert out["dry_run"] and out["tracks"] >= 1
    assert board.read_bytes() == before


@pytest.mark.requires_kicad
def test_drag_tracks_follows_the_pad(board):
    from kicad_mcp import board_edit
    out = board_edit.place_footprints(board, [{"reference": "J1", "x": 5, "y": 6}], drag_tracks=True)
    assert out["tracks_dragged"] == 1
    pcbnew = pytest.importorskip("pcbnew")
    (t,) = list(pcbnew.LoadBoard(str(board)).GetTracks())
    assert (pcbnew.ToMM(t.GetStart().x), pcbnew.ToMM(t.GetStart().y)) == (5.0, 6.0)
    assert (pcbnew.ToMM(t.GetEnd().x), pcbnew.ToMM(t.GetEnd().y)) == (15.0, 10.0)
