"""Altium record streams: the framing every .SchDoc and .PcbDoc is built from.

A schematic's `FileHeader` stream is a run of records, each a 3-byte length,
a flag byte, and `|KEY=VALUE|...` text. ASCII .SchDoc files hold the same
records one per line. Everything else in the importer reads these records.
"""

from __future__ import annotations

import pytest

from kicad_mcp.altium import records
from kicad_mcp.altium.records import AltiumFormatError, Record


def _frame(body: bytes, flag: int = 0) -> bytes:
    return len(body).to_bytes(3, "little") + bytes([flag]) + body


def _stream(*bodies: str) -> bytes:
    return b"".join(_frame(b.encode("latin-1") + b"\0") for b in bodies)


def test_frames_text_records():
    data = _stream("|HEADER=Protel for Windows - Schematic Capture Binary File Version 5.0",
                   "|RECORD=1|UNIQUEID=AAAA", "|RECORD=2|NAME=1")
    recs, binary = records.parse_record_stream(data)
    assert binary == 0
    assert recs[0].type == -1
    assert [(r.index, r.type) for r in recs[1:]] == [(0, 1), (1, 2)]
    assert recs[1].text("UNIQUEID") == "AAAA"


def test_binary_record_counted():
    data = _stream("|HEADER=x", "|RECORD=1") + _frame(b"\x01\x02\x03", flag=1) + _stream("|RECORD=2")
    recs, binary = records.parse_record_stream(data)
    assert binary == 1
    # The binary record stays as an empty placeholder, so OWNERINDEX values
    # still point at the right records.
    assert [(r.index, r.type) for r in recs[1:]] == [(0, 1), (1, -1), (2, 2)]


def test_keys_case_insensitive():
    a = Record.from_text("|RECORD=4|Location.X=5|OwnerIndex=3")
    assert a.int("LOCATION.X") == 5
    assert a.owner == 3
    assert Record.from_text("|RECORD=4").owner is None


def test_frac_units():
    r = Record.from_text("|X=364|X_FRAC=1576|DistanceFromTop=3|DistanceFromTop_Frac1=937008")
    assert r.mm("X") == pytest.approx(364.01576 * 0.254, abs=1e-9)
    assert r.mm1("DISTANCEFROMTOP") == pytest.approx(10.0, abs=1e-6)


def test_bool_and_defaults():
    r = Record.from_text("|ISHIDDEN=T|ISSOLID=F")
    assert r.bool("ISHIDDEN") and not r.bool("ISSOLID") and not r.bool("MISSING")
    assert r.int("MISSING", 7) == 7 and r.text("MISSING", "d") == "d"


def test_utf8_key_wins():
    body = "|RECORD=4|TEXT=caf?|%UTF8%TEXT=".encode("latin-1") + "café".encode("utf-8")
    recs, _ = records.parse_record_stream(_frame(b"|HEADER=x\0") + _frame(body + b"\0"))
    assert recs[1].text("TEXT") == "café"


def test_ascii_schdoc():
    text = ("|HEADER=Protel for Windows - Schematic Capture Ascii File Version 5.0|WEIGHT=3\r\n"
            "|RECORD=31|FONTIDCOUNT=1\r\n"
            "|RECORD=15|OWNERINDEX=-1|LOCATION.X=100\r\n")
    recs = records.parse_ascii_records(text)
    assert recs[0].type == -1
    assert [(r.index, r.type) for r in recs[1:]] == [(0, 31), (1, 15)]
    assert recs[2].int("LOCATION.X") == 100


def test_not_altium_file(tmp_path):
    bogus = tmp_path / "x.SchDoc"
    bogus.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    with pytest.raises(AltiumFormatError):
        records.read_schdoc_records(bogus)


def test_ascii_file_read_from_disk(tmp_path):
    f = tmp_path / "a.SchDoc"
    f.write_bytes(b"|HEADER=Protel for Windows - Schematic Capture Ascii File Version 5.0\r\n"
                  b"|RECORD=1|UNIQUEID=ZZZZ\r\n")
    header, recs, binary = records.read_schdoc_records(f)
    assert "Ascii" in header.text("HEADER")
    assert recs[0].text("UNIQUEID") == "ZZZZ" and binary == 0
