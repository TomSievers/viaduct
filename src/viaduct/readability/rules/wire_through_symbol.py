"""wire_through_symbol: a wire crossing a symbol body or a sheet box."""

from __future__ import annotations

from shapely.strtree import STRtree

from viaduct.readability.rules.base import Rule, shrink


class WireThroughSymbol(Rule):
    """A wire passes over a symbol's body or a hierarchical sheet's box. It reads as if the
    wire connects inside the part, and it hides the drawing. Bodies are taken as the
    bounding box of the symbol's graphics."""
    id = "wire_through_symbol"
    title = "Wire through symbol"
    severity = "error"
    options = {"power_symbols": False}

    def check(self, sheet, options):
        wires = sheet.wires
        tree = STRtree([w.line for w in wires])
        bodies = [(s.ref, s.body) for s in sheet.symbols
                  if s.body is not None and (options["power_symbols"] or not s.power)]
        bodies += [(f"sheet {b.name}", b.box) for b in sheet.sheets]
        for name, body in bodies:
            area = shrink(body)
            if area.is_empty:
                continue
            for i in tree.query(area, predicate="intersects"):
                x = wires[i].line.intersection(area).centroid
                yield self.finding(f"wire runs through {name}", (x.x, x.y), [name, wires[i].uuid],
                                   "route the wire around the body")
