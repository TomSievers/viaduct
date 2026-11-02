"""wire_over_pin: a wire touching a pin anywhere but end to end at its tip."""

from __future__ import annotations

from shapely.geometry import Point
from shapely.strtree import STRtree

from viaduct.readability.rules.base import Rule, same
from viaduct.readability.sheet import TOL


class WireOverPin(Rule):
    """A wire touches a pin other than by ending at its tip: it passes over the tip, so the pin
    connects to the middle of the wire, or it runs along or across the pin's line. Either way
    the reader cannot tell whether the pin is connected."""
    id = "wire_over_pin"
    title = "Wire over pin"
    severity = "error"
    options = {}

    def check(self, sheet, options):
        wires = sheet.wires
        tree = STRtree([w.line for w in wires])
        for pin in sheet.pins:
            line = pin.line.buffer(TOL)
            for i in tree.query(line, predicate="intersects"):
                w = wires[i]
                x = w.line.intersection(line)
                ends_at_tip = same(w.a, pin.tip) or same(w.b, pin.tip)
                if ends_at_tip and x.length < 3 * TOL:
                    continue                    # a proper connection
                if ends_at_tip:
                    msg = f"wire runs back along {pin.label}"
                elif w.line.distance(Point(pin.tip)) < TOL:
                    msg = f"{pin.label} connects to the middle of a wire"
                else:
                    msg = f"wire crosses {pin.label}"
                yield self.finding(msg, pin.tip, [pin.owner, w.uuid],
                                   "end the wire at the pin tip, leaving straight outward")
