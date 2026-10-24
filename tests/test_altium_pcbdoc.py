"""Board components as Altium stores them: designator, id path, position.

SOURCEUNIQUEID is the path of UniqueIds from the root sheet down to the part
(`\\SHEETSYM\\PART`); it is what links a footprint to its symbol, channel by
channel, where the designator alone cannot.
"""

from __future__ import annotations

import pytest

from altium_fixtures import altium_fixture
from kicad_mcp.altium import pcbdoc


def test_edp_board_parts():
    parts = pcbdoc.read_board_parts(altium_fixture("eDP/eDP_adapter_dvt1.PcbDoc"))
    with_ids = [p for p in parts if p.id_path]
    assert len(with_ids) == 112
    (c10p,) = [p for p in parts if p.source_designator == "C10P"]
    assert c10p.id_path == ("GHHNYTHT",)
    assert c10p.sheet_path == "power"
    assert c10p.pattern == "CAPC1005N" or c10p.pattern
    assert c10p.x_mm != 0 and c10p.y_mm != 0


@pytest.mark.slow
def test_sampler_channel_paths():
    parts = pcbdoc.read_board_parts(altium_fixture("sampler/Sampler.PCBDOC"))
    c10 = [p for p in parts if p.source_designator == "C10"]
    assert len(c10) == 8
    assert {p.id_path[-1] for p in c10} == {"JSBUTAQD"}
    assert len({p.id_path[0] for p in c10}) == 8
