"""wire_bends: wire paths with many bends between two connection points."""

from __future__ import annotations

from viaduct.readability.rules.base import Rule, chain, same, stops, unit, wire_ends


class WireBends(Rule):
    """A wire path between two connection points (pins, junctions, labels, branches) bends
    more than max_bends times. Every bend is a place the eye can lose the path; a net label
    or moving a part usually removes most of them."""
    id = "wire_bends"
    title = "Too many bends"
    severity = "warning"
    options = {"max_bends": 3}

    def check(self, sheet, options):
        wires = [w for w in sheet.wires if not w.bus]
        ends, stop_keys = wire_ends(wires), stops(sheet, wires)
        seen = set()
        for w in wires:
            if id(w) in seen:
                continue
            path, _, _ = chain(ends, stop_keys, w)
            seen |= {id(v) for v in path}
            bends = sum(not same(unit(a.a, a.b), unit(b.a, b.b)) and not same(unit(a.a, a.b), unit(b.b, b.a))
                        for a, b in zip(path, path[1:]))
            if bends > options["max_bends"]:
                yield self.finding(f"wire path bends {bends} times", path[0].a, [v.uuid for v in path],
                                   "straighten the path, move a part, or use net labels")
