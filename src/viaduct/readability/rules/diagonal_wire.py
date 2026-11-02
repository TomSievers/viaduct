"""diagonal_wire: wires that are neither horizontal nor vertical."""

from __future__ import annotations

from viaduct.readability.rules.base import Rule


class DiagonalWire(Rule):
    """A wire runs at an angle. Schematics are read along horizontal and vertical lines;
    diagonal wires are hard to follow and to keep clear of other items."""
    id = "diagonal_wire"
    title = "Diagonal wire"
    severity = "warning"
    options = {"buses": True}

    def check(self, sheet, options):
        for w in sheet.wires:
            if (options["buses"] or not w.bus) and not (w.horizontal or w.vertical):
                mid = ((w.a[0] + w.b[0]) / 2, (w.a[1] + w.b[1]) / 2)
                yield self.finding("wire is not horizontal or vertical", mid, [w.uuid],
                                   "redraw it as a horizontal and a vertical segment")
