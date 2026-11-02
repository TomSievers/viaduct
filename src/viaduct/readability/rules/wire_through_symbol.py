"""wire_through_symbol: a wire crossing a symbol body or a sheet box."""

from __future__ import annotations

import math

from shapely.strtree import STRtree

from viaduct.readability.rules.base import Rule, shrink
from viaduct.readability.sheet import TOL


class WireThroughSymbol(Rule):
    """A wire passes over a symbol's body or a hierarchical sheet's box. It reads as if the
    wire connects inside the part, and it hides the drawing. Bodies are taken as the
    bounding box of the symbol's graphics. A wire that starts at a pin inside its own body,
    such as the centre of a small test point, and leaves the body from there is fine."""
    id = "wire_through_symbol"
    title = "Wire through symbol"
    severity = "error"
    options = {"power_symbols": False}

    def check(self, sheet, options):
        wires = sheet.wires
        tree = STRtree([w.line for w in wires])
        bodies = [(s.ref, s.body, [p.tip for p in s.pins if not p.hidden]) for s in sheet.symbols
                  if s.body is not None and (options["power_symbols"] or not s.power)]
        bodies += [(f"sheet {b.name}", b.box, []) for b in sheet.sheets]
        for name, body, tips in bodies:
            area = shrink(body)
            if area.is_empty:
                continue
            for i in tree.query(area, predicate="intersects"):
                inside = wires[i].line.intersection(area)
                if inside.geom_type == "LineString" and any(
                        min(math.dist(inside.coords[0], t), math.dist(inside.coords[-1], t)) < TOL for t in tips):
                    continue                    # leaving the body from a pin inside it
                x = inside.centroid
                yield self.finding(f"wire runs through {name}", (x.x, x.y), [name, wires[i].uuid],
                                   "route the wire around the body")
