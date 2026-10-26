"""off_grid: connection points off the schematic grid."""

from __future__ import annotations

from viaduct.readability.rules.base import Rule
from viaduct.readability.sheet import TOL


class OffGrid(Rule):
    """A wire end, junction, pin tip, label or no-connect flag is off the grid (mm, KiCad's
    default 1.27). Off-grid points leave wires that almost meet: they look connected and are
    not, or need a jog to connect."""
    id = "off_grid"
    title = "Off grid"
    severity = "warning"
    options = {"grid": 1.27}

    def check(self, sheet, options):
        g = options["grid"]
        if g <= 0:
            return

        def off(p) -> bool:
            return any(abs(v / g - round(v / g)) * g > TOL for v in p)

        for w in sheet.wires:
            for p in (w.a, w.b):
                if off(p):
                    yield self.finding("wire end is off the grid", p, [w.uuid], "snap it to the grid")
        for p in sheet.junctions:
            if off(p):
                yield self.finding("junction is off the grid", p, [], "snap it to the grid")
        for p in sheet.no_connects:
            if off(p):
                yield self.finding("no-connect flag is off the grid", p, [], "snap it to the grid")
        for t in sheet.labels:
            if off(t.at):
                yield self.finding(f"{t.label} is off the grid", t.at, [t.text], "snap it to the grid")
        for s in sheet.symbols:
            tips = [p for p in s.pins if not p.hidden and off(p.tip)]
            if tips:
                yield self.finding(f"{len(tips)} pins of {s.ref} are off the grid", tips[0].tip, [s.ref],
                                   "move the symbol so its pins land on the grid")
