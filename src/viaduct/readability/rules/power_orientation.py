"""power_orientation: supply symbols point up, ground symbols point down."""

from __future__ import annotations

import re

from viaduct.readability.rules.base import Rule


class PowerOrientation(Rule):
    """Power symbols follow the convention that current flows down the page: supply symbols
    sit above what they feed, pointing up, and ground and negative rails point down. Nets whose
    name matches down_pattern (a regular expression) count as ground or negative."""
    id = "power_orientation"
    title = "Power symbol orientation"
    severity = "warning"
    options = {"down_pattern": r"(?i)^(.*gnd.*|vss.*|vee.*|-.*|earth|0)$"}

    def check(self, sheet, options):
        down = re.compile(options["down_pattern"])
        for s in sheet.symbols:
            pins = [p for p in s.pins if not p.hidden] or s.pins
            if not s.power or len(pins) != 1:
                continue
            ox, oy = pins[0].outward
            ground = bool(down.match(s.value))
            # the pin leaves the body toward what it powers: up for ground, down for a supply
            want = -1 if ground else 1
            if abs(oy) < 0.5:
                yield self.finding(f"{s.value} symbol {s.ref} points sideways", s.at, [s.ref],
                                   "rotate it to point " + ("down" if ground else "up"))
            elif oy * want < 0:
                yield self.finding(f"{s.value} symbol {s.ref} points " + ("up" if ground else "down"), s.at, [s.ref],
                                   "rotate it to point " + ("down" if ground else "up"))
