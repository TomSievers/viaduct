"""A small s-expression reader for KiCad files that remembers where each list sits in the
text, so an edit can replace one list and leave every other byte as KiCad wrote it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r'\s*(?:(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()"]+))', re.S)


@dataclass
class Node:
    """One (...) list. items holds child Nodes and atoms; a quoted atom keeps its quotes."""
    items: list = field(default_factory=list)
    start: int = 0          # index of "("
    end: int = 0            # index just past ")"

    @property
    def name(self) -> str | None:
        return self.items[0] if self.items and isinstance(self.items[0], str) else None

    @property
    def atoms(self) -> list[str]:
        """Unquoted atoms after the name."""
        return [unquote(a) for a in self.items[1:] if isinstance(a, str)]

    def lists(self, name: str | None = None) -> list[Node]:
        return [n for n in self.items if isinstance(n, Node) and (name is None or n.name == name)]

    def first(self, name: str) -> Node | None:
        return next(iter(self.lists(name)), None)


def parse(text: str) -> Node:
    """Parse the first top-level list in text."""
    stack: list[Node] = []
    pos = 0
    while True:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"unbalanced s-expression at offset {pos}")
        pos = m.end()
        if m.group(1):
            stack.append(Node(start=m.start(1)))
        elif m.group(2):
            node = stack.pop()
            node.end = m.end(2)
            if not stack:
                return node
            stack[-1].items.append(node)
        elif stack:
            stack[-1].items.append(m.group(3) or m.group(4))


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def unquote(atom: str) -> str:
    """A quoted atom's text; KiCad writes a newline in text as \\n."""
    if len(atom) >= 2 and atom[0] == atom[-1] == '"':
        return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), atom[1:-1])
    return atom


def quote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + s.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r") + '"'


def num(v: float) -> str:
    """A number as KiCad writes it: up to 6 decimals, no trailing zeros."""
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def yes(v: bool) -> str:
    return "yes" if v else "no"


def fmt(item, depth: int) -> str:
    """Format a nested Python list the way KiCad pretty-prints: atoms on the opening line,
    each sub-list on its own line one tab deeper, ")" on its own line when there were any."""
    if not isinstance(item, list):
        return item
    atoms = [a for a in item if not isinstance(a, list)]
    subs = [a for a in item if isinstance(a, list)]
    if not subs:
        return "(" + " ".join(atoms) + ")"
    pad = "\t" * (depth + 1)
    # keep atoms that follow a sub-list (KiCad's "addsublayer") in their place
    out, line = [], "(" + " ".join(a for a in item[:item.index(subs[0])])
    for a in item[item.index(subs[0]):]:
        out.append(pad + (fmt(a, depth + 1) if isinstance(a, list) else a))
    return line + "\n" + "\n".join(out) + "\n" + "\t" * depth + ")"


def indent_of(text: str, node: Node) -> str:
    """The whitespace before node on its line."""
    line_start = text.rfind("\n", 0, node.start) + 1
    return text[line_start:node.start]
