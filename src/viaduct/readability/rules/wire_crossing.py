"""wire_crossing: two wires cross without connecting."""

from __future__ import annotations

from shapely.strtree import STRtree

from viaduct.readability.rules.base import Rule, same


class WireCrossing(Rule):
    """Two wires cross each other without connecting. A reader has to look for a missing
    junction dot to know the nets are separate; routing around, or a net label on one side,
    takes the question away."""
    id = "wire_crossing"
    title = "Wires cross"
    severity = "warning"
    options = {"buses": False}

    def check(self, sheet, options):
        wires = [w for w in sheet.wires if options["buses"] or not w.bus]
        tree = STRtree([w.line for w in wires])
        for i, w in enumerate(wires):
            for j in tree.query(w.line, predicate="intersects"):
                if j <= i:
                    continue
                v = wires[j]
                x = w.line.intersection(v.line)
                if x.geom_type != "Point":
                    continue                    # collinear overlap: wire_overlap
                p = (x.x, x.y)
                if any(same(p, e) for e in (w.a, w.b, v.a, v.b)):
                    continue                    # a corner or a T, not a crossing
                if any(same(p, jn) for jn in sheet.junctions):
                    continue                    # connected: four_way_junction
                yield self.finding("wires cross without connecting", p, [w.uuid, v.uuid],
                                   "route one wire around, or break it with a net label on each side")
