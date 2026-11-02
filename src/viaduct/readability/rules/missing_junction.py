"""missing_junction: three or more wires meet without a junction dot."""

from __future__ import annotations

from viaduct.readability.rules.base import Rule, key, passes_through, tees, wire_ends


class MissingJunction(Rule):
    """Three or more wire directions meet at a point that has no junction dot. Without the dot
    a T looks like a wire ending next to another; KiCad adds the dot itself, so a missing one
    usually comes from a hand-written or generated file. Wires meeting at a pin tip are
    pin_exit's: KiCad draws no dot there."""
    id = "missing_junction"
    title = "Missing junction"
    severity = "error"
    options = {}

    def check(self, sheet, options):
        wires = [w for w in sheet.wires if not w.bus]
        dots = {key(j) for j in sheet.junctions}
        tips = {key(p.tip) for p in sheet.pins}
        ends, tee = wire_ends(wires), tees(wires)
        for k, here in ends.items():
            if k in dots or k in tips or (len(here) < 3 and k not in tee):
                continue
            p = _point(here, k)
            n = len(here) + 2 * sum(passes_through(w, p) for w in wires)
            if n >= 3:
                yield self.finding(f"{n} wires meet without a junction", p, [w.uuid for w, _ in here],
                                   "add a junction at the point")


def _point(here, k):
    w = here[0][0]
    return w.a if key(w.a) == k else w.b
