"""S-expression reader/writer for KiCad files.

KiCad's `.kicad_sch`, `.kicad_sym`, `.kicad_pcb` and library tables all share one
S-expression dialect. This module is the whole of our file-format layer, so it
matters that it round-trips faithfully.

The one subtlety worth stating: bare symbols and quoted strings are distinct in
KiCad's grammar. `(hide yes)` and `(hide "yes")` are not the same token, and
KiCad rejects files that confuse them. `Sym` marks bare tokens so the writer can
reproduce the distinction instead of guessing from content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

__all__ = ["Sym", "SExpr", "parse", "dumps", "children", "child", "value", "values"]


@dataclass(frozen=True)
class Sym:
    """A bare (unquoted) token, e.g. `yes`, `no`, `kicad_sch`."""

    name: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


#: A parsed node: a list whose first element is normally a Sym naming the node.
SExpr = list


_WHITESPACE = " \t\r\n"


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.n = len(text)

    def error(self, msg: str) -> SyntaxError:
        line = self.text.count("\n", 0, self.i) + 1
        col = self.i - (self.text.rfind("\n", 0, self.i) + 1) + 1
        return SyntaxError(f"{msg} at line {line} column {col}")

    def skip_ws(self) -> None:
        while self.i < self.n and self.text[self.i] in _WHITESPACE:
            self.i += 1

    def read(self) -> object:
        self.skip_ws()
        if self.i >= self.n:
            raise self.error("unexpected end of input")
        ch = self.text[self.i]
        if ch == "(":
            return self.read_list()
        if ch == ")":
            raise self.error("unexpected ')'")
        if ch == '"':
            return self.read_string()
        return self.read_atom()

    def read_list(self) -> SExpr:
        self.i += 1  # consume '('
        out: SExpr = []
        while True:
            self.skip_ws()
            if self.i >= self.n:
                raise self.error("unterminated list")
            if self.text[self.i] == ")":
                self.i += 1
                return out
            out.append(self.read())

    def read_string(self) -> str:
        self.i += 1  # consume opening quote
        chunks: list[str] = []
        while True:
            if self.i >= self.n:
                raise self.error("unterminated string")
            ch = self.text[self.i]
            if ch == "\\":
                nxt = self.text[self.i + 1] if self.i + 1 < self.n else ""
                chunks.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.i += 2
                continue
            if ch == '"':
                self.i += 1
                return "".join(chunks)
            chunks.append(ch)
            self.i += 1

    def read_atom(self) -> object:
        start = self.i
        while self.i < self.n and self.text[self.i] not in _WHITESPACE + "()\"":
            self.i += 1
        raw = self.text[start : self.i]
        if not raw:
            raise self.error("empty atom")
        return _coerce_atom(raw)


def _coerce_atom(raw: str) -> object:
    """Numbers become numbers; everything else stays a bare symbol.

    KiCad never uses a bare token that merely looks numeric for a non-numeric
    purpose, so this is safe and it keeps geometry arithmetic natural.
    """
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return Sym(raw)


def parse(text: str) -> SExpr:
    """Parse a complete KiCad file into a single top-level node."""
    reader = _Reader(text)
    node = reader.read()
    reader.skip_ws()
    if reader.i != reader.n:
        raise reader.error("trailing content after top-level expression")
    if not isinstance(node, list):
        raise SyntaxError("top level of a KiCad file must be a list")
    return node


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_number(x: float | int) -> str:
    if isinstance(x, int):
        return str(x)
    # KiCad writes plain decimals; avoid scientific notation and trailing zeros.
    out = f"{x:.10f}".rstrip("0").rstrip(".")
    return out if out not in ("", "-") else "0"


def _dump(node: object, indent: int, out: list[str]) -> None:
    """Emit KiCad's layout: leaf nodes on one line, nested nodes indented.

    KiCad itself writes `(at 1 2 0)` inline but breaks any node containing
    sublists across lines. Matching that keeps our output diffable against
    files KiCad rewrites.
    """
    pad = "\t" * indent
    if not isinstance(node, list):
        out.append(pad + _atom_str(node))
        return

    atoms = [_atom_str(a) for a in node if not isinstance(a, list)]
    sublists = [a for a in node if isinstance(a, list)]
    header = f"{pad}({' '.join(atoms)})" if atoms else f"{pad}()"

    if not sublists:
        out.append(header)
        return

    out.append(header[:-1])  # drop the ')' so children can nest under it
    for sub in sublists:
        _dump(sub, indent + 1, out)
    out.append(f"{pad})")


def _atom_str(a: object) -> str:
    if isinstance(a, Sym):
        return a.name
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, (int, float)):
        return _fmt_number(a)
    if isinstance(a, str):
        return f'"{_escape(a)}"'
    raise TypeError(f"cannot serialise {type(a).__name__} in an S-expression")


def dumps(node: SExpr) -> str:
    """Serialise back to KiCad's tab-indented layout, with trailing newline."""
    out: list[str] = []
    _dump(node, 0, out)
    return "\n".join(out) + "\n"


# --- query helpers -------------------------------------------------------


def _name_of(node: object) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], Sym):
        return node[0].name
    return None


def children(node: SExpr, name: str) -> Iterator[SExpr]:
    """Direct child nodes with the given head symbol."""
    for item in node:
        if _name_of(item) == name:
            yield item  # type: ignore[misc]


def child(node: SExpr, name: str) -> SExpr | None:
    """First direct child with the given head symbol, or None."""
    return next(children(node, name), None)


def values(node: SExpr, name: str) -> list:
    """Payload of the first matching child (everything after the head symbol)."""
    c = child(node, name)
    return list(c[1:]) if c else []


def value(node: SExpr, name: str, default=None):
    """Single payload value of the first matching child.

    Returns `default` when absent, so callers can treat optional fields
    uniformly instead of branching on presence.
    """
    vals = values(node, name)
    if not vals:
        return default
    v = vals[0]
    return v.name if isinstance(v, Sym) else v


def find_all(node: SExpr, name: str) -> Iterator[SExpr]:
    """Recursive search for nodes with the given head symbol."""
    if _name_of(node) == name:
        yield node
    for item in node:
        if isinstance(item, list):
            yield from find_all(item, name)


def make(name: str, *args: object) -> SExpr:
    """Build a node: `make("at", 1, 2, 0)` -> `(at 1 2 0)`."""
    return [Sym(name), *args]


def sym(name: str) -> Sym:
    return Sym(name)


def as_text(v: object) -> str:
    return v.name if isinstance(v, Sym) else str(v)


def flatten_points(seq: Sequence[float]) -> list[float]:  # pragma: no cover
    return [float(x) for x in seq]
