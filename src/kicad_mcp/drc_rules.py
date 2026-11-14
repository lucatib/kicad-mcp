"""Read a board's custom design rules (.kicad_dru) for the router.

KiCad's Python API has no access to its DRC engine, so the router could only
keep each net class's clearance, and a board whose rules ask more -- 8 mm from
a valve anode to ground, ground only on the back, no vias -- came back full of
violations. This reads the rules file and answers what the router needs to
know about a track it is about to lay:

- the clearance to another item (`clearance`, `physical_clearance`, and
  `hole_clearance` towards holes);
- the clearance to the board edge (`edge_clearance`);
- its width (`track_width` min or opt);
- whether a track, or a via, of that net may go on a layer (`disallow`).

Conditions are KiCad's expression language over the two items `A` and `B`:
`||`, `&&`, `!`, comparisons (string `==` takes `*` and `?` wildcards), and
the properties and functions below. A rule whose condition uses anything else
is skipped and reported in `skipped`, rather than guessed. As in KiCad, a
later rule wins over an earlier one, and a matching rule replaces the net
class value instead of adding to it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr

#: Constraint types the router honours.
CLEARANCES = ("clearance", "physical_clearance")
KNOWN = CLEARANCES + ("hole_clearance", "edge_clearance", "track_width", "disallow")


class Unknown(Exception):
    """The condition uses something this reader does not model."""


@dataclass(frozen=True)
class Item:
    """What a condition can ask of one item.

    `type` is KiCad's name for it: "Track", "Via", "Pad", "Zone", "Edge".
    `pad_type` follows KiCad's Pad_Type values: "Through-hole", "SMD",
    "NPTH, mechanical". `layer` is a layer name, or None for all layers.
    """
    type: str
    net: str = ""
    netclass: str = ""
    layer: str | None = None
    reference: str = ""
    pad_type: str = ""


@dataclass
class Rule:
    name: str
    condition: str | None
    layer: str | None
    constraints: dict = field(default_factory=dict)   # type -> {"min": mm, "opt": mm, "max": mm}
    disallow: frozenset = frozenset()
    ignored: bool = False                             # (severity ignore)


@dataclass
class Rules:
    rules: list[Rule] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    # -- queries --------------------------------------------------------

    def value(self, kind: str, a: Item, b: Item | None = None, bound: str = "min") -> float | None:
        """The value the last matching rule of `kind` sets for A (against B), or None."""
        found = None
        for rule in self.rules:
            if rule.ignored or kind not in rule.constraints or bound not in rule.constraints[kind]:
                continue
            if self._applies(rule, a, b):
                found = rule.constraints[kind][bound]
        return found

    def disallowed(self, what: str, a: Item) -> bool:
        """Whether a rule disallows `what` ("track", "via") for item A."""
        return any(not r.ignored and what in r.disallow and self._applies(r, a, None)
                   for r in self.rules)

    def _applies(self, rule: Rule, a: Item, b: Item | None) -> bool:
        if rule.layer is not None and not _on_layer(rule.layer, a.layer):
            return False
        if rule.condition is None:
            return True
        try:
            if _evaluate(rule.condition, a, b):
                return True
            # Two-item constraints are tried both ways round, as KiCad does.
            return b is not None and bool(_evaluate(rule.condition, b, a))
        except Unknown as exc:
            note = f"{rule.name}: {exc}"
            if note not in self.skipped:
                self.skipped.append(note)
            return False


def _on_layer(spec: str, layer: str | None) -> bool:
    if layer is None:
        return True
    if spec == "outer":
        return layer in ("F.Cu", "B.Cu")
    if spec == "inner":
        return layer.startswith("In") and layer.endswith(".Cu")
    return fnmatch.fnmatchcase(layer, spec)


# --- file ----------------------------------------------------------------

_UNITS = {"mm": 1.0, "mil": 0.0254, "in": 25.4, "um": 0.001}


def _length(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*([-+]?[0-9.]+)\s*([a-z]*)\s*", str(v))
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        raise ValueError(f"not a length: {v!r}")
    return float(m.group(1)) * _UNITS.get(m.group(2) or "mm", 1.0)


def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        quoted = False
        for i, ch in enumerate(line):
            if ch == '"' and (i == 0 or line[i - 1] != "\\"):
                quoted = not quoted
            elif ch == "#" and not quoted:
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def parse(text: str) -> Rules:
    top = sexpr.parse("(rules\n" + _strip_comments(text) + "\n)")
    out = Rules()
    for node in sexpr.children(top, "rule"):
        name = str(node[1]) if len(node) > 1 else "?"
        rule = Rule(name, sexpr.value(node, "condition"), sexpr.value(node, "layer"))
        if sexpr.value(node, "severity") is not None and str(sexpr.value(node, "severity")) == "ignore":
            rule.ignored = True
        for c in sexpr.children(node, "constraint"):
            kind = str(c[1])
            if kind == "disallow":
                rule.disallow = rule.disallow | {str(x) for x in c[2:] if not isinstance(x, list)}
                continue
            bounds = {}
            for sub in c[2:]:
                if isinstance(sub, list) and sub and str(sub[0]) in ("min", "opt", "max"):
                    try:
                        bounds[str(sub[0])] = _length(sub[1])
                    except (ValueError, IndexError):
                        out.skipped.append(f"{name}: {kind} {sexpr.dumps(sub).strip()} is not a length")
            rule.constraints[kind] = bounds
        out.rules.append(rule)
    return out


def load(path: str | Path) -> Rules:
    """The rules in `path`; none when there is no such file."""
    p = Path(path)
    if not p.is_file():
        return Rules()
    return parse(p.read_text(encoding="utf-8"))


# --- expressions ---------------------------------------------------------

_TOK = re.compile(r"\s*(?:(\|\||&&|==|!=|<=|>=|[!<>()+\-*/,.])|'((?:[^'\\]|\\.)*)'|([0-9]+(?:\.[0-9]+)?[a-z]*)|([A-Za-z_][A-Za-z_0-9]*))")


def _tokens(text: str) -> list[tuple[str, object]]:
    out, i = [], 0
    while i < len(text):
        if text[i:].strip() == "":
            break
        m = _TOK.match(text, i)
        if m is None:
            raise Unknown(f"cannot read {text[i:i + 12]!r}")
        op, string, number, word = m.groups()
        if op:
            out.append(("op", op))
        elif string is not None:
            out.append(("str", string.replace("\\'", "'")))
        elif number is not None:
            out.append(("num", _length(number)))
        else:
            out.append(("word", word))
        i = m.end()
    return out


def _match(value: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(value, pattern)


def _prop(item: Item | None, name: str):
    if item is None:
        raise Unknown(f"B.{name} with only one item")
    key = name.lower()
    if key == "netname":
        return item.net
    if key == "netclass":
        return item.netclass
    if key == "type":
        return item.type
    if key == "layer":
        if item.layer is None:
            raise Unknown("Layer of an item on every layer")
        return item.layer
    if key == "reference":
        if not item.reference:
            raise Unknown(f"Reference of a {item.type}")
        return item.reference
    if key == "pad_type":
        return item.pad_type if item.type == "Pad" else ""
    raise Unknown(f"property {name}")


def _call(item: Item | None, name: str, args: list):
    if item is None:
        raise Unknown(f"B.{name}() with only one item")
    key = name.lower()
    if key == "hasnetclass":
        return _match(item.netclass, str(args[0]))
    if key == "memberoffootprint":
        return bool(item.reference) and _match(item.reference, str(args[0]))
    if key == "isplated":
        return item.type == "Via" or (item.type == "Pad" and item.pad_type != "NPTH, mechanical")
    if key == "existsonlayer":
        return item.layer is None or _match(item.layer, str(args[0]))
    raise Unknown(f"function {name}()")


class _Parser:
    def __init__(self, text: str, a: Item, b: Item | None) -> None:
        self.toks = _tokens(text)
        self.i = 0
        self.items = {"A": a, "B": b}

    def peek(self, value=None):
        if self.i >= len(self.toks):
            return None
        tok = self.toks[self.i]
        return tok if value is None or tok[1] == value else None

    def take(self, value=None):
        tok = self.peek(value)
        if tok is None:
            raise Unknown(f"expected {value!r}" if value else "unexpected end")
        self.i += 1
        return tok

    def parse(self):
        v = self.orexpr()
        if self.i != len(self.toks):
            raise Unknown(f"unexpected {self.toks[self.i][1]!r}")
        return v

    # Unknown on one side of || or && does not sink the whole expression
    # when the other side decides it.
    def orexpr(self):
        val, unknown = self._side(self.andexpr)
        while self.peek("||"):
            self.take()
            rhs, rhs_unknown = self._side(self.andexpr)
            if val is True or rhs is True:
                val, unknown = True, None
            else:
                val, unknown = False, unknown or rhs_unknown
        if unknown:
            raise unknown
        return val

    def andexpr(self):
        val, unknown = self._side(self.notexpr)
        while self.peek("&&"):
            self.take()
            rhs, rhs_unknown = self._side(self.notexpr)
            if (val is False and not unknown) or (rhs is False and not rhs_unknown):
                val, unknown = False, None
            else:
                val, unknown = bool(val) and bool(rhs), unknown or rhs_unknown
        if unknown:
            raise unknown
        return val

    def _side(self, parse):
        start = self.i
        try:
            return parse(), None
        except Unknown as exc:
            self._skip_operand(start)
            return None, exc

    def _skip_operand(self, start: int) -> None:
        """After an Unknown, move past the rest of this operand of || or &&."""
        self.i, depth = start, 0
        while self.i < len(self.toks):
            kind, v = self.toks[self.i]
            if kind == "op" and v == "(":
                depth += 1
            elif kind == "op" and v == ")":
                if depth == 0:
                    return
                depth -= 1
            elif kind == "op" and v in ("||", "&&") and depth == 0:
                return
            self.i += 1

    def notexpr(self):
        if self.peek("!"):
            self.take()
            return not self.notexpr()
        return self.compare()

    def compare(self):
        lhs = self.term()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("==", "!=", "<", ">", "<=", ">="):
            op = self.take()[1]
            rhs = self.term()
            if op in ("==", "!="):
                if isinstance(lhs, str) and isinstance(rhs, str):
                    same = _match(lhs, rhs) or _match(rhs, lhs)
                else:
                    same = lhs == rhs
                return same if op == "==" else not same
            try:
                return {"<": lhs < rhs, ">": lhs > rhs, "<=": lhs <= rhs, ">=": lhs >= rhs}[op]
            except TypeError as exc:
                raise Unknown(f"cannot compare {lhs!r} {op} {rhs!r}") from exc
        return lhs

    def term(self):
        tok = self.take()
        kind, v = tok
        if kind == "op" and v == "(":
            val = self.orexpr()
            self.take(")")
            return val
        if kind in ("str", "num"):
            return v
        if kind == "word":
            if v in ("A", "B") and self.peek("."):
                self.take(".")
                name = self.take()[1]
                if self.peek("("):
                    self.take("(")
                    args = []
                    while not self.peek(")"):
                        args.append(self.term())
                        if self.peek(","):
                            self.take(",")
                    self.take(")")
                    return _call(self.items[v], name, args)
                return _prop(self.items[v], name)
            if v in ("true", "false"):
                return v == "true"
        raise Unknown(f"unexpected {v!r}")


def _evaluate(text: str, a: Item, b: Item | None):
    return _Parser(text, a, b).parse()
