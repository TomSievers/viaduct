"""wire_through_text: a wire running through or touching a label, field or free text."""

from __future__ import annotations

from shapely.geometry import Point
from shapely.strtree import STRtree

from viaduct.readability.rules.base import SHRINK, Rule


class WireThroughText(Rule):
    """A wire runs through visible text, or closer to it than clearance (mm): a net label, a
    symbol's reference or value, a sheet name or a free text. The line strikes through or
    underlines the characters, and it is unclear which wire a label names. A label's own
    wire, where it meets the label's connection point, is not counted."""
    id = "wire_through_text"
    title = "Wire through text"
    severity = "error"
    options = {"clearance": 0.2}

    def check(self, sheet, options):
        wires = sheet.wires
        tree = STRtree([w.line for w in wires])
        for t in sheet.texts:
            area = t.box.buffer(options["clearance"] - SHRINK, join_style="mitre")
            if t.kind.endswith("label"):
                area = area.difference(Point(t.at).buffer(options["clearance"] + 2 * SHRINK))
            if area.is_empty:
                continue
            for i in tree.query(area, predicate="intersects"):
                x = wires[i].line.intersection(area).centroid
                yield self.finding(f"wire runs through {t.label}", (x.x, x.y), [t.owner or t.text, wires[i].uuid],
                                   "move the text clear of the wire, or reroute the wire")
