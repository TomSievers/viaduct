"""An indexed view of one sheet for searching it fast: what is in a region, what blocks a
shape, where a shape fits, and an orthogonal wire path between two points. Read-only.

Everything on the sheet is an Item of one kind:
- wire: a wire segment (id: its uuid)
- line: everything a symbol draws, pins included (id: reference)
- body: a symbol's bounding box (id: reference)
- text: visible text (id: "R1 Value", "label SDA", ...)
- sheet: a hierarchical sheet's box (id: "sheet <name>")
- pin: one pin's line (id: "R1 pin 2")
- point: a junction, no-connect flag or label anchor (id: "junction x,y", ...)

Queries use a spatial index, so a search over thousands of candidate positions takes
milliseconds per candidate set instead of testing every item. The sheet can be edited in
memory (add or remove items) and the index follows.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from shapely.geometry import LineString, Point, box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from viaduct.readability.sheet import TOL, Pt, Sheet

KINDS = ("wire", "line", "body", "text", "sheet", "pin", "point")
GRID = 1.27
_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


@dataclass
class Item:
    kind: str
    id: str
    geom: BaseGeometry
    owner: str | None = None    # the symbol or sheet a text, pin or line belongs to

    def as_dict(self) -> dict:
        x0, y0, x1, y1 = self.geom.bounds
        out = {"kind": self.kind, "id": self.id, "bounds": [round(v, 3) for v in (x0, y0, x1, y1)]}
        if self.owner and self.owner != self.id:
            out["owner"] = self.owner
        return out


@dataclass
class Candidate:
    x: float
    y: float
    variant: object
    geom: BaseGeometry
    cost: float


class Space:
    def __init__(self, sheet: Sheet):
        self.items: list[Item] = []
        for w in sheet.wires:
            self.items.append(Item("wire", w.uuid, w.line))
        for s in sheet.symbols:
            if not s.strokes.is_empty:
                self.items.append(Item("line", s.ref, s.strokes, s.ref))
            if s.body is not None:
                self.items.append(Item("body", s.ref, s.body, s.ref))
            for p in s.pins:
                if not p.hidden:
                    self.items.append(Item("pin", f"{p.owner} pin {p.number}", p.line, p.owner))
        for b in sheet.sheets:
            self.items.append(Item("sheet", f"sheet {b.name}", b.box, b.name))
            for p in b.pins:
                self.items.append(Item("pin", f"{p.owner} pin {p.number}", p.line, p.owner))
        for t in sheet.texts:
            self.items.append(Item("text", t.label, t.box, t.owner))
        for j in sheet.junctions:
            self.items.append(Item("point", f"junction {j[0]:g},{j[1]:g}", Point(j)))
        for p in sheet.no_connects:
            self.items.append(Item("point", f"no_connect {p[0]:g},{p[1]:g}", Point(p)))
        for t in sheet.labels:
            self.items.append(Item("point", f"{t.label} anchor", Point(t.at), t.text))
        self._tree = None

    # ---------- editing ----------

    def add(self, item: Item) -> None:
        self.items.append(item)
        self._tree = None

    def remove(self, ids: Iterable[str], kind: str | None = None) -> None:
        ids = set(ids)
        self.items = [i for i in self.items if not (i.id in ids and (kind is None or i.kind == kind))]
        self._tree = None

    def move(self, kind: str, id: str, old: BaseGeometry, new: BaseGeometry) -> None:
        """Give one item a new shape; old picks it out when several share an id."""
        item = next(i for i in self.items if i.kind == kind and i.id == id and i.geom.equals(old))
        item.geom = new
        self._tree = None

    @property
    def tree(self) -> STRtree:
        if self._tree is None:
            self._tree = STRtree([i.geom for i in self.items])
        return self._tree

    # ---------- queries ----------

    def query(self, region: BaseGeometry, kinds: Iterable[str] | None = None, exclude: Iterable[str] = (),
              distance: float = 0.0) -> list[Item]:
        """Items of the given kinds that intersect region, or come within distance of it."""
        kinds, exclude = set(kinds or KINDS), set(exclude)
        if distance > 0:
            hits = self.tree.query(region, predicate="dwithin", distance=distance)
        else:
            hits = self.tree.query(region, predicate="intersects")
        return [self.items[k] for k in sorted(hits)
                if self.items[k].kind in kinds and self.items[k].id not in exclude
                and (self.items[k].owner is None or self.items[k].owner not in exclude)]

    def blocked(self, geom: BaseGeometry, clearance: float = 0.0, kinds: Iterable[str] | None = None,
                exclude: Iterable[str] = ()) -> bool:
        return bool(self.query(geom, kinds, exclude, clearance))

    # ---------- placement search ----------

    def search(self, shape: Callable[[float, float, object], BaseGeometry], near: Pt, radius: float = 20.0,
               step: float = 0.635, variants: Iterable = (None,), clearance: float = 0.25,
               kinds: Iterable[str] = ("wire", "line", "text", "sheet", "pin"), exclude: Iterable[str] = (),
               variant_cost: Callable[[object], float] | None = None,
               extra: Callable[[BaseGeometry], float | None] | None = None,
               origin: Pt | None = None, limit: int = 1) -> list[Candidate]:
        """The cheapest positions on a step grid within radius of near where shape(x, y, variant)
        is clearance clear of the given kinds. Cost is the distance from origin (default: near)
        plus variant_cost(variant) plus extra(geom); extra returns None to refuse a position.
        Candidates are tried cheapest first and the search stops as soon as no remaining one can
        beat the limit-th best, so a free spot nearby is found after a handful of tests."""
        origin = origin or near
        variants = list(variants)
        n = int(radius / step)
        cands = []
        for dx in range(-n, n + 1):
            for dy in range(-n, n + 1):
                x, y = round(near[0] + dx * step, 4), round(near[1] + dy * step, 4)
                if math.hypot(x - near[0], y - near[1]) > radius + 1e-9:
                    continue
                for v in variants:
                    cands.append((math.dist((x, y), origin) + (variant_cost(v) if variant_cost else 0), x, y, v))
        cands.sort(key=lambda c: c[0])
        best: list[Candidate] = []
        for bound, x, y, v in cands:
            if len(best) >= limit and bound >= best[-1].cost:
                break
            g = shape(x, y, v)
            if g is None or self.blocked(g, clearance, kinds, exclude):
                continue
            more = extra(g) if extra else 0.0
            if more is None:
                continue
            best.append(Candidate(x, y, v, g, bound + more))
            best.sort(key=lambda c: c.cost)
            del best[limit:]
        return best

    # ---------- routing ----------

    def route(self, start: Pt, end: Pt, start_dir: Pt | None = None, end_dir: Pt | None = None,
              clearance: float = 0.25, exclude: Iterable[str] = (), margin: float = 15.0,
              bend_cost: float = 3.0, cross_cost: float = 8.0) -> list[Pt] | None:
        """An orthogonal wire path on the 1.27 mm grid from start to end, as its corner points,
        or None when there is none within margin mm of the two points.

        The path keeps clearance from symbol bodies, text and sheet boxes, never touches a pin
        other than at start or end, never runs along another wire, and never meets another
        wire's end, a junction, a no-connect flag or a label anchor (that would connect it).
        It may cross other wires, at a cost of cross_cost steps, but never turns on one.
        start_dir and end_dir are the directions the path must leave start and arrive at end
        from, such as a pin's outward direction; each bend costs bend_cost steps.
        """
        exclude = set(exclude)
        # the grid runs through start; end must lie on it
        if any(abs((e - s) / GRID - round((e - s) / GRID)) > 1e-3 for s, e in zip(start, end)):
            return None
        x0, y0 = min(start[0], end[0]) - margin, min(start[1], end[1]) - margin
        x1, y1 = max(start[0], end[0]) + margin, max(start[1], end[1]) + margin
        ends = {_k(start), _k(end)}
        stop_pins = [i for i in self.query(Point(start).buffer(TOL)) + self.query(Point(end).buffer(TOL))
                     if i.kind == "pin"]
        stop_ids = {i.id for i in stop_pins}
        stop_owners = {i.owner for i in stop_pins}
        node_cache: dict = {}
        edge_cache: dict = {}

        def node(p):
            """None if p can't be used, 'cross' if only passing straight over a wire, else 'free'."""
            k = _k(p)
            if k in node_cache:
                return node_cache[k]
            state = "free"
            if k not in ends:
                here = self.query(Point(p).buffer(TOL), exclude=exclude)
                for i in here:
                    if i.kind in ("point", "pin", "sheet") or (i.kind == "body" and i.owner not in stop_owners):
                        state = None
                        break
                    if i.kind == "wire":
                        a, b = i.geom.coords[0], i.geom.coords[-1]
                        if _k(a) == k or _k(b) == k:
                            state = None
                            break
                        state = "cross"
                if state and self.blocked(Point(p), clearance, ("text",), exclude):
                    state = None
            node_cache[k] = state
            return state

        def edge(p, q):
            k = (_k(p), _k(q))
            if k in edge_cache:
                return edge_cache[k]
            seg = LineString([p, q])
            ok = not self.blocked(seg, clearance, ("text",), exclude)
            if ok:
                for i in self.query(seg, ("wire", "pin", "body", "sheet", "line"), exclude):
                    if i.kind == "wire":
                        if i.geom.intersection(seg).length > TOL:
                            ok = False          # running along another wire
                    elif i.kind == "pin":
                        # the pin being connected may only be touched at its tip
                        touch = i.geom.intersection(seg)
                        if i.id not in stop_ids or not all(
                                min(math.dist(c, start), math.dist(c, end)) < TOL for c in _coords(touch)):
                            ok = False
                    elif i.kind in ("body", "sheet"):
                        # the first and last step may leave or enter the symbol being connected
                        own = i.owner in stop_owners and (_k(p) in ends or _k(q) in ends)
                        if not own and i.geom.buffer(-0.05).intersects(seg):
                            ok = False
                    elif i.owner not in stop_owners:
                        ok = False              # a symbol's drawing, unless it is the one we connect to
                    if not ok:
                        break
            edge_cache[k] = edge_cache[(k[1], k[0])] = ok
            return ok

        goal = _k(end)
        h = lambda p: (abs(p[0] - end[0]) + abs(p[1] - end[1])) / GRID
        heap = [(h(start), 0.0, start, None)]
        came: dict = {}
        seen: dict = {}
        pushed: dict = {}
        while heap:
            f, g, p, d = heapq.heappop(heap)
            state = (_k(p), d)
            if state in seen and seen[state] <= g:
                continue
            seen[state] = g
            if _k(p) == goal:
                if end_dir is not None and d is not None and (d[0], d[1]) != (-end_dir[0], -end_dir[1]):
                    continue
                path = [p]
                while state in came:
                    state, p = came[state]
                    path.append(p)
                return _corners(path[::-1])
            here = node(p)
            for nd in _DIRS:
                if d is None and start_dir is not None and nd != (round(start_dir[0]), round(start_dir[1])):
                    continue
                if d is not None and nd == (-d[0], -d[1]):
                    continue
                if d is not None and nd != d and (here == "cross"):
                    continue                    # never turn on top of another wire
                q = (round(p[0] + nd[0] * GRID, 4), round(p[1] + nd[1] * GRID, 4))
                if not (x0 <= q[0] <= x1 and y0 <= q[1] <= y1) or _k(q) == _k(start):
                    continue                    # outside the window, or back through the start
                there = node(q)
                if there is None or not edge(p, q):
                    continue
                cost = g + 1 + (bend_cost if d is not None and nd != d else 0) + (cross_cost if there == "cross" else 0)
                nstate = (_k(q), nd)
                if nstate in pushed and pushed[nstate] <= cost:
                    continue
                pushed[nstate] = cost
                came[nstate] = ((_k(p), d), p)
                heapq.heappush(heap, (cost + h(q), cost, q, nd))
        return None


def _k(p: Pt) -> tuple[int, int]:
    return round(p[0] / TOL), round(p[1] / TOL)


def _coords(g: BaseGeometry) -> list[Pt]:
    if g.is_empty:
        return []
    if hasattr(g, "geoms"):
        return [c for part in g.geoms for c in _coords(part)]
    return list(g.coords)


def _corners(path: list[Pt]) -> list[Pt]:
    """A grid path with its straight runs merged: just the start, the bends and the end."""
    def heading(p, q):
        return tuple((d > TOL) - (d < -TOL) for d in (q[0] - p[0], q[1] - p[1]))
    out = [path[0]]
    for a, b, c in zip(path, path[1:], path[2:]):
        if heading(a, b) != heading(b, c):
            out.append(b)
    out.append(path[-1])
    return out


def rect(width: float, height: float) -> Callable[[float, float, object], BaseGeometry]:
    """A shape for search: a width x height box centred on the position."""
    return lambda x, y, v: box(x - width / 2, y - height / 2, x + width / 2, y + height / 2)
