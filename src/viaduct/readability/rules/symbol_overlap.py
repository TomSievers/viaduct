"""symbol_overlap: symbol bodies or sheet boxes on top of each other."""

from __future__ import annotations

from shapely.strtree import STRtree

from viaduct.readability.rules.base import Rule, shrink


class SymbolOverlap(Rule):
    """Two symbol bodies, or a symbol and a hierarchical sheet box, overlap. Pins and wires of
    one disappear into the other."""
    id = "symbol_overlap"
    title = "Symbols overlap"
    severity = "error"
    options = {}

    def check(self, sheet, options):
        bodies = [(s.ref, shrink(s.body)) for s in sheet.symbols if s.body is not None]
        bodies += [(f"sheet {b.name}", shrink(b.box)) for b in sheet.sheets]
        bodies = [(n, b) for n, b in bodies if not b.is_empty]
        tree = STRtree([b for _, b in bodies])
        for i, (name, body) in enumerate(bodies):
            for j in tree.query(body, predicate="intersects"):
                if j > i:
                    c = body.intersection(bodies[j][1]).centroid
                    yield self.finding(f"{name} overlaps {bodies[j][0]}", (c.x, c.y), [name, bodies[j][0]],
                                       "move the symbols apart")
