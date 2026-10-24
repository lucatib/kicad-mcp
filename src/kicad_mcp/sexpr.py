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

import re
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


#: One token per match: "(", ")", a quoted string (escapes kept raw), or a bare
#: atom. Leading whitespace is part of the match, so a gap between matches is
#: a character no token can start with.
_TOKEN = re.compile(r'[ \t\r\n]*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^ \t\r\n()"]+))', re.S)
_TRAILING_WS = re.compile(r"[ \t\r\n]*")
_STRING_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}
_ESCAPE = re.compile(r"\\(.?)", re.S)


def _error(text: str, i: int, msg: str) -> SyntaxError:
    line = text.count("\n", 0, i) + 1
    col = i - (text.rfind("\n", 0, i) + 1) + 1
    return SyntaxError(f"{msg} at line {line} column {col}")


def _unescape(raw: str) -> str:
    if "\\" not in raw:
        return raw
    return _ESCAPE.sub(lambda m: _STRING_ESCAPES.get(m.group(1), m.group(1)), raw)


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
    """Parse a complete KiCad file into a single top-level node.

    One regex scan with an explicit stack; each distinct atom ("yes", "1.27")
    is converted once and shared, since the values are immutable. A large
    library parses several times faster than with a character-by-character
    reader.
    """
    atoms: dict[str, object] = {}
    stack: list[SExpr] = []
    top: object = None
    i, n = 0, len(text)
    match = _TOKEN.match
    while True:
        m = match(text, i)
        if m is None:
            ws = _TRAILING_WS.match(text, i).end()
            if ws >= n:
                if stack:
                    raise _error(text, n, "unterminated list")
                break
            if text[ws] == '"':
                raise _error(text, ws, "unterminated string")
            raise _error(text, ws, "unexpected character")
        i = m.end()
        opener, closer, string, atom = m.groups()
        if opener:
            if top is not None and not stack:
                raise _error(text, m.start(1), "trailing content after top-level expression")
            node: SExpr = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
            continue
        if closer:
            if not stack:
                raise _error(text, m.start(2), "unexpected ')'")
            done = stack.pop()
            if not stack:
                top = done
            continue
        if string is not None:
            value: object = _unescape(string)
        else:
            value = atoms.get(atom)
            if value is None:
                value = atoms[atom] = _coerce_atom(atom)
        if not stack:
            if top is not None:
                raise _error(text, m.start(), "trailing content after top-level expression")
            top = value
            continue
        stack[-1].append(value)
    if top is None:
        raise _error(text, n, "unexpected end of input")
    if not isinstance(top, list):
        raise SyntaxError("top level of a KiCad file must be a list")
    return top


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
