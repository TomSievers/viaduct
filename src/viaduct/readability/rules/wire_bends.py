"""wire_bends: wire paths with many bends between two connection points."""

from __future__ import annotations

from viaduct.readability.rules.base import Rule, key, same, tees, unit, wire_ends


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
        ends = wire_ends(wires)
        stops = {key(p.tip) for p in sheet.pins} | {key(t.at) for t in sheet.labels}
        stops |= {key(p) for p in sheet.junctions + sheet.no_connects}
        stops |= {k for k, v in ends.items() if len(v) != 2}
        stops |= tees(wires)
        seen = set()
        for w in wires:
            if id(w) in seen:
                continue
            path = _walk(ends, stops, w)
            seen |= {id(v) for v in path}
            bends = sum(not same(unit(a.a, a.b), unit(b.a, b.b)) and not same(unit(a.a, a.b), unit(b.b, b.a))
                        for a, b in zip(path, path[1:]))
            if bends > options["max_bends"]:
                yield self.finding(f"wire path bends {bends} times", path[0].a, [v.uuid for v in path],
                                   "straighten the path, move a part, or use net labels")


def _walk(ends, stops, start):
    """The chain of wires through start, between two stops, in order."""
    def extend(w, at):
        out = []
        while key(at) not in stops:
            nxt = [(v, far) for v, far in ends[key(at)] if v is not w]
            if len(nxt) != 1 or nxt[0][0] is start:
                break
            w, at = nxt[0]
            out.append(w)
        return out
    back = extend(start, start.a)
    return back[::-1] + [start] + extend(start, start.b)
