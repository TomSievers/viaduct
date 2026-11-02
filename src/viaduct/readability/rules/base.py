"""What every readability rule shares: the Rule base class, the Finding it reports, and the
point and wire lookups several rules need."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from viaduct.readability.sheet import TOL, Pt, Sheet, Wire

SHRINK = 0.05       # bodies and text boxes are shrunk this much before overlap tests, so touching is not overlapping


@dataclass
class Finding:
    rule: str
    message: str
    at: Pt
    items: list[str] = field(default_factory=list)   # what is involved: references, label texts, uuids
    fix: str | None = None
    severity: str = ""      # filled in by the checker from the config
    sheet: str = ""

    def as_dict(self) -> dict:
        out = {"rule": self.rule, "severity": self.severity, "sheet": self.sheet, "message": self.message,
               "at": [round(self.at[0], 3), round(self.at[1], 3)], "items": self.items}
        if self.fix:
            out["fix"] = self.fix
        return out


class Rule:
    """A readability rule, checked once per sheet. The class docstring is its description.

    options holds every option the rule takes with its default; the config may override them
    with values of the same type.
    """
    id: str = ""
    title: str = ""
    severity: str = "warning"
    options: dict = {}

    def check(self, sheet: Sheet, options: dict) -> Iterator[Finding]:
        raise NotImplementedError

    def finding(self, message: str, at: Pt, items: list[str] = (), fix: str | None = None) -> Finding:
        return Finding(self.id, message, (float(at[0]), float(at[1])), list(items), fix)

    def describe(self) -> dict:
        return {"id": self.id, "title": self.title, "description": " ".join((self.__doc__ or "").split()),
                "default_severity": self.severity, "default_options": dict(self.options)}


# ---------- shared lookups ----------

def key(p: Pt) -> tuple[int, int]:
    """A point snapped to TOL, for dictionary lookups."""
    return round(p[0] / TOL), round(p[1] / TOL)


def same(p: Pt, q: Pt) -> bool:
    return math.dist(p, q) < TOL


def shrink(g: BaseGeometry, d: float = SHRINK) -> BaseGeometry:
    return g.buffer(-d, join_style="mitre")


def unit(p: Pt, q: Pt) -> Pt:
    d = math.dist(p, q)
    return ((q[0] - p[0]) / d, (q[1] - p[1]) / d) if d > TOL else (0.0, 0.0)


def wire_ends(wires: list[Wire]) -> dict[tuple[int, int], list[tuple[Wire, Pt]]]:
    """For each wire endpoint, the wires ending there with their other end."""
    ends = defaultdict(list)
    for w in wires:
        ends[key(w.a)].append((w, w.b))
        ends[key(w.b)].append((w, w.a))
    return ends


def passes_through(w: Wire, p: Pt) -> bool:
    """p lies on w but is not one of its ends."""
    return not same(w.a, p) and not same(w.b, p) and w.line.distance(Point(p)) < TOL


def arms(sheet: Sheet, p: Pt, wires: list[Wire] | None = None) -> int:
    """How many wire directions leave p: an end counts once, a wire passing through twice,
    and a pin tip at p once (it is a direction too)."""
    wires = sheet.wires if wires is None else wires
    n = 0
    for w in wires:
        if same(w.a, p) or same(w.b, p):
            n += 1
        elif passes_through(w, p):
            n += 2
    return n + sum(same(pin.tip, p) for pin in sheet.pins)


def tees(wires: list[Wire]) -> set[tuple[int, int]]:
    """Wire ends that lie on the middle of another wire."""
    tree = STRtree([w.line for w in wires])
    out = set()
    for w in wires:
        for e in (w.a, w.b):
            if any(passes_through(wires[i], e) for i in tree.query(Point(e).buffer(TOL), predicate="intersects")):
                out.add(key(e))
    return out


def stops(sheet: Sheet, wires: list[Wire]) -> set[tuple[int, int]]:
    """Points where a wire path ends: pin tips, labels, junctions, no-connects, branches,
    loose ends and wire ends on another wire's middle."""
    ends = wire_ends(wires)
    out = {key(p.tip) for p in sheet.pins} | {key(t.at) for t in sheet.labels}
    out |= {key(p) for p in sheet.junctions + sheet.no_connects}
    out |= {k for k, v in ends.items() if len(v) != 2}
    return out | tees(wires)


def chain(ends, stop_keys, start: Wire) -> tuple[list[Wire], Pt, Pt]:
    """The wires of the path through start between two stops, in order, and its two ends."""
    def extend(w, at):
        out = []
        while key(at) not in stop_keys:
            nxt = [(v, far) for v, far in ends[key(at)] if v is not w]
            if len(nxt) != 1 or nxt[0][0] is start:
                break
            w, at = nxt[0]
            out.append(w)
        return out, at
    back, a = extend(start, start.a)
    fwd, b = extend(start, start.b)
    return back[::-1] + [start] + fwd, a, b
