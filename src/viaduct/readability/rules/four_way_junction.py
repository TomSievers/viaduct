"""four_way_junction: four or more wire directions meet at one junction."""

from __future__ import annotations

from viaduct.readability.rules.base import Rule, arms


class FourWayJunction(Rule):
    """Four or more wire directions meet at one junction. A dot that is small or lost in
    printing turns it into a plain crossing; two T junctions a grid step apart cannot be
    misread."""
    id = "four_way_junction"
    title = "Four-way junction"
    severity = "warning"
    options = {}

    def check(self, sheet, options):
        wires = [w for w in sheet.wires if not w.bus]
        for j in sheet.junctions:
            n = arms(sheet, j, wires)
            if n >= 4:
                yield self.finding(f"{n} wires meet at one junction", j, [],
                                   "split it into two T junctions offset by a grid step")
