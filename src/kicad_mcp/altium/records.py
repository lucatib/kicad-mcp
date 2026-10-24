"""Altium's record streams, the layer every other importer module reads.

A binary .SchDoc is an OLE compound file. Its `FileHeader` stream is a run of
records: a 3-byte little-endian length, a flag byte (0 = text), then
`|KEY=VALUE|...` text in the Windows codepage, NUL-terminated. The ASCII
variant holds the same records one per line. A record's position in the run
is what other records' OWNERINDEX points at, so binary records we cannot read
still take an index.

Key case varies between Altium versions (`LOCATION.X` vs `Location.X`), so
keys are upper-cased once here. Text that does not fit the codepage is stored
a second time under `%UTF8%KEY`, which wins.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ToolInputError

#: One Altium schematic unit (10 mil) in millimetres.
UNIT_MM = 0.254

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ASCII_MAGIC = b"|HEADER=Protel for Windows"


class AltiumFormatError(ToolInputError):
    """The file is not a schematic or board Altium writes."""


class Record(dict):
    """One record: upper-cased keys, its index in the stream, its RECORD type."""

    def __init__(self, props: dict[str, str], index: int = -1):
        super().__init__(props)
        self.index = index
        try:
            self.type = int(props.get("RECORD", -1))
        except ValueError:
            self.type = -1

    @classmethod
    def from_text(cls, body: str, index: int = -1) -> "Record":
        props: dict[str, str] = {}
        for token in body.split("|"):
            if "=" in token:
                key, value = token.split("=", 1)
                props[key.upper()] = value
        for key in [k for k in props if k.startswith("%UTF8%")]:
            raw = props.pop(key)
            try:
                props[key[6:]] = raw.encode("latin-1").decode("utf-8")
            except UnicodeError:
                props.setdefault(key[6:], raw)
        return cls(props, index)

    def text(self, key: str, default: str = "") -> str:
        return self.get(key.upper(), default)

    def int(self, key: str, default: int = 0) -> int:
        raw = self.get(key.upper())
        if raw is None or raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            try:
                return int(float(raw))
            except ValueError:
                return default

    def float(self, key: str, default: float = 0.0) -> float:
        raw = self.get(key.upper())
        try:
            return float(raw) if raw not in (None, "") else default
        except ValueError:
            return default

    def bool(self, key: str) -> bool:
        return self.get(key.upper(), "").upper() in ("T", "TRUE")

    def mm(self, key: str) -> float:
        """A length stored as KEY (10 mil units) plus KEY_FRAC (1/100000 of one)."""
        return (self.int(key) + self.int(key + "_FRAC") / 100000) * UNIT_MM

    def mm1(self, key: str) -> float:
        """The other encoding: KEY in 100 mil units plus KEY_FRAC1 (1/100000 of 10 mil)."""
        return (self.int(key) * 10 + self.int(key + "_FRAC1") / 100000) * UNIT_MM

    @property
    def owner(self) -> int | None:
        if "OWNERINDEX" not in self:
            return None
        value = self.int("OWNERINDEX", -1)
        return value if value >= 0 else None


def _decode(body: bytes) -> str:
    return body.split(b"\0", 1)[0].decode("latin-1")


def parse_record_stream(data: bytes) -> tuple[list[Record], int]:
    """Records of a framed stream; element 0 is the header. Also the binary count."""
    out: list[Record] = []
    binary = 0
    i = 0
    while i + 4 <= len(data):
        length = int.from_bytes(data[i:i + 3], "little")
        flag = data[i + 3]
        body = data[i + 4:i + 4 + length]
        i += 4 + length
        index = len(out) - 1  # the header takes no index
        if flag != 0:
            binary += 1
            out.append(Record({}, index))
            continue
        out.append(Record.from_text(_decode(body), index))
    return out, binary


def parse_ascii_records(text: str) -> list[Record]:
    """Records of an ASCII .SchDoc; element 0 is the header."""
    out: list[Record] = []
    for line in text.splitlines():
        if line.strip():
            out.append(Record.from_text(line, len(out) - 1))
    return out


def _ole(path: Path):
    import olefile  # noqa: PLC0415 - only needed for binary files

    return olefile.OleFileIO(str(path))


def read_schdoc_records(path: str | Path) -> tuple[Record, list[Record], int]:
    """(header, records, binary records skipped) of a binary or ASCII .SchDoc."""
    p = Path(path)
    head = p.read_bytes()[:64]
    if head.startswith(_ASCII_MAGIC):
        recs = parse_ascii_records(p.read_bytes().decode("latin-1"))
        binary = 0
    elif head.startswith(_OLE_MAGIC):
        ole = _ole(p)
        try:
            if not ole.exists("FileHeader"):
                raise AltiumFormatError(f"{p.name} has no FileHeader stream; not an Altium schematic.",
                                        remedy="Pass a .SchDoc saved by Altium Designer.")
            recs, binary = parse_record_stream(ole.openstream("FileHeader").read())
        finally:
            ole.close()
    else:
        raise AltiumFormatError(f"{p.name} is not an Altium schematic (neither OLE nor ASCII).",
                                remedy="Pass a .SchDoc saved by Altium Designer.")
    if not recs or "HEADER" not in recs[0]:
        raise AltiumFormatError(f"{p.name} has no schematic header record.",
                                remedy="Pass a .SchDoc saved by Altium Designer.")
    return recs[0], recs[1:], binary


def read_storage_images(path: str | Path) -> list[bytes]:
    """Images embedded in a binary .SchDoc's `Storage` stream, in file order.

    The stream is one header record, then per image a framed record whose
    body is a 1-byte name length, the original file name, a 4-byte data
    length and zlib-compressed image data.
    """
    import zlib  # noqa: PLC0415

    p = Path(path)
    if not p.read_bytes()[:8].startswith(_OLE_MAGIC):
        return []
    ole = _ole(p)
    try:
        if not ole.exists("Storage"):
            return []
        data = ole.openstream("Storage").read()
    finally:
        ole.close()
    out: list[bytes] = []
    i = 0
    first = True
    while i + 4 <= len(data):
        length = int.from_bytes(data[i:i + 3], "little")
        body = data[i + 4:i + 4 + length]
        i += 4 + length
        if first:  # |HEADER=Icon storage|...
            first = False
            continue
        try:
            n = body[1]
            j = 2 + n
            size = int.from_bytes(body[j:j + 4], "little")
            out.append(zlib.decompress(body[j + 4:j + 4 + size]))
        except (IndexError, zlib.error):
            out.append(b"")
    return out


def read_pcbdoc_components(path: str | Path) -> list[Record]:
    """Rows of a .PcbDoc's Components6 table, each framed by a 4-byte length."""
    p = Path(path)
    if not p.read_bytes()[:8].startswith(_OLE_MAGIC):
        raise AltiumFormatError(f"{p.name} is not an Altium board (not an OLE file).",
                                remedy="Pass a binary .PcbDoc saved by Altium Designer.")
    ole = _ole(p)
    try:
        if not ole.exists("Components6/Data"):
            return []
        data = ole.openstream("Components6/Data").read()
    finally:
        ole.close()
    rows: list[Record] = []
    i = 0
    while i + 4 <= len(data):
        length = int.from_bytes(data[i:i + 4], "little")
        rows.append(Record.from_text(_decode(data[i + 4:i + 4 + length]), len(rows)))
        i += 4 + length
    return rows
