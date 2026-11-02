"""wire_overlap: wires lying on top of each other."""

from __future__ import annotations

from shapely.strtree import STRtree

from viaduct.readability.rules.base import Rule
from viaduct.readability.sheet import TOL


class WireOverlap(Rule):
    """Two wires run on top of each other for part of their length. On screen they look like
    one wire, so it is impossible to see where each one starts and ends, or whether they
    connect."""
    id = "wire_overlap"
    title = "Wires overlap"
    severity = "error"
    options = {}

    def check(self, sheet, options):
        wires = sheet.wires
        tree = STRtree([w.line for w in wires])
        for i, w in enumerate(wires):
            for j in tree.query(w.line, predicate="intersects"):
                if j <= i:
                    continue
                x = w.line.intersection(wires[j].line)
                if x.length > TOL:
                    c = x.centroid
                    yield self.finding(f"wires overlap for {x.length:.2f} mm", (c.x, c.y), [w.uuid, wires[j].uuid],
                                       "delete the duplicate segment, or join the wires end to end")
