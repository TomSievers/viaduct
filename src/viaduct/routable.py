"""Gross routing errors a board has before routing starts. Every finding is a proof.

A finding means no route exists at the assigned sizes, given the placement, the through vias of
the netclass (none inside a pad, unless allowed), and the copper that cannot move: pads and locked
tracks and vias. Unlocked copper can be ripped up, so it blocks nothing. Whenever a check is unsure it stays silent, so a clean result
only means nothing is provably wrong, not that the board will route.

Three stages, from cheap to expensive:

1. track_start_blocked, track_start_conflict: a track's end must lie on its pad, and its round end
   needs its clearance. Either no spot on the pad allows that, or two pads' tracks each fit alone
   but are too close wherever they start.
2. pad_walled_in: from where it can start, the track's centre line cannot reach a via spot, copper
   of its net, or open board. Exact geometry: a shape shrunk by the track's own size, not a grid.
3. nets_trapped: each net can get out alone, but a group needs more tracks than the gaps around
   them hold. Any track leaving the group crosses those gaps, so max-flow over them is a bound:
   capacities and via counts are generous, and a shortfall cannot be routed around.

Zones are not obstacles, since they refill around tracks. Custom rules are not evaluated.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from viaduct.model import BoardModel, Copper
from viaduct.plan import EPS

RADII = (1.5, 3.0, 6.0)      # how far a track is followed; the search only grows when it has to
CLUSTER_GAP = 2.0            # nets this close can compete for the same gaps
WINDOW_MARGIN = 2.5          # past the group, the board counts as open
SAMPLE = 0.25                # mm between triangulation points along an obstacle's outline
INFINITE = 10 ** 9


@dataclass
class _Start:
    pad: Copper
    width: float
    via_diameter: float
    via_drill: float
    regions: dict            # layer -> where on the pad the track's end can lie
    reached: str = ""        # same_net, via or open
    spots: object = None     # via spots found while searching (one search only)


class _Space:
    """The copper that cannot move, with each obstacle's inflated shape kept for reuse."""

    def __init__(self, model: BoardModel, edge_clearance: float, hole_to_hole: float, via_in_pad: bool = False):
        self.model, self.edge, self.hole_to_hole, self.via_in_pad = model, edge_clearance, hole_to_hole, via_in_pad
        self.window_spots = {}
        self.worst = max(model.net_clearance.values(), default=model.default_clearance)
        self.trees, self.inflated, self.insets = {}, {}, {}
        self.holes = [h for h in model.holes
                      if not (self._via(h.owner) and not model.by_id[h.owner].locked)]
        self.hole_points = STRtree([Point(h.x, h.y) for h in self.holes]) if self.holes else None

    def _via(self, owner: str) -> bool:
        item = self.model.by_id.get(owner)
        return item is not None and item.kind == "via"

    def tree(self, layer: str):
        if layer not in self.trees:
            members = [c for c in self.model.items if layer in c.geom and c.kind != "zone"]
            self.trees[layer] = (STRtree([c.geom[layer] for c in members]), members)
        return self.trees[layer]

    def near(self, layer: str, bounds, margin: float) -> list[Copper]:
        tree, members = self.tree(layer)
        x1, y1, x2, y2 = bounds
        return [members[i] for i in tree.query(box(x1 - margin, y1 - margin, x2 + margin, y2 + margin))]

    @staticmethod
    def fixed(c: Copper) -> bool:
        return c.kind == "pad" or bool(c.locked)

    def grown_exact(self, c: Copper, layer: str):
        """The obstacle's own shape, a hair larger so its outline counts as inside it."""
        key = (c.id, layer, "exact")
        if key not in self.inflated:
            self.inflated[key] = shapely.prepare(c.geom[layer].buffer(1e-4)) or c.geom[layer].buffer(1e-4)
        return self.inflated[key]

    def spots_in_window(self, s: _Start, window):
        key = (s.pad.net, window.bounds, s.via_diameter, s.via_drill)
        if key not in self.window_spots:
            self.window_spots[key] = self.via_spots(s.pad.net, window, s.via_diameter, s.via_drill)
        return self.window_spots[key]

    def _grown(self, c: Copper, layer: str, amount: float):
        key = (c.id, layer, round(amount, 4))
        if key not in self.inflated:
            self.inflated[key] = c.geom[layer].buffer(amount - EPS, quad_segs=8)
        return self.inflated[key]

    def free(self, layer: str, net: str, region, half: float):
        """region minus what a track or via of this half width on this net has to stay away from."""
        if region.is_empty:
            return region
        cut = []
        for c in self.near(layer, region.bounds, half + self.worst + 0.05):
            if c.net != net and self.fixed(c):
                cut.append(self._grown(c, layer, half + self.model.clearance(net, c.net)))
        if not cut:
            return region
        out = region.difference(shapely.union_all(cut))
        return out

    def inside_edge(self, region, half: float):
        if self.model.outline is None:
            return region
        key = round(half, 4)
        if key not in self.insets:
            self.insets[key] = self.model.outline.buffer(-(half + self.edge - EPS), quad_segs=8)
        return region.intersection(self.insets[key])

    def via_spots(self, net: str, region, diameter: float, drill: float):
        spots = self.inside_edge(region, diameter / 2)
        for layer in self.model.copper_layers:
            if spots.is_empty:
                return spots
            spots = self.free(layer, net, spots, diameter / 2)
        if spots.is_empty or self.hole_points is None:
            return spots
        keep = max(h.r for h in self.holes) + drill / 2 + self.hole_to_hole
        x1, y1, x2, y2 = spots.bounds
        close = self.hole_points.query(box(x1 - keep, y1 - keep, x2 + keep, y2 + keep))
        cut = [Point(self.holes[i].x, self.holes[i].y).buffer(self.holes[i].r + drill / 2 + self.hole_to_hole - EPS)
               for i in close]
        return spots.difference(shapely.union_all(cut)) if cut else spots


def _pieces(g) -> list:
    return [p for p in getattr(g, "geoms", [g]) if not p.is_empty]


def _joined(model: BoardModel, pad: Copper) -> bool:
    for L, g in pad.geom.items():
        for c in model.near(L, g, 0):
            if c.net == pad.net and c.kind != "pad" and g.intersects(c.geom[L]):
                return True
    return False


# ---------- stage 1: the start of the track ----------

def _regions(space: _Space, pad: Copper, width: float, layers: list[str]) -> dict:
    out = {}
    for L in layers:
        region = space.free(L, pad.net, space.inside_edge(pad.geom[L], width / 2), width / 2)
        if not region.is_empty:
            out[L] = region
    return out


def _largest_width(space: _Space, pad: Copper, width: float, layers: list[str]) -> float:
    """The widest track known to fit, 0 if not even a thin one does."""
    low, high = 0.0, width
    for _ in range(10):
        middle = (low + high) / 2
        if _regions(space, pad, middle, layers):
            low = middle
        else:
            high = middle
    return max(0.0, math.floor((low - 2 * EPS) * 1000) / 1000)   # obstacles are grown just short of the limit


def _blocked(space: _Space, pad: Copper, width: float, layers: list[str]) -> dict:
    largest = _largest_width(space, pad, width, layers)
    near = []
    for L in layers:
        for c in space.near(L, pad.geom[L].bounds, width + space.worst):
            if c.net != pad.net and space.fixed(c):
                need = space.model.clearance(pad.net, c.net)
                gap = pad.geom[L].distance(c.geom[L])
                if gap < width + need:
                    near.append({"item": c.ref or c.id, "kind": c.kind, "net": c.net, "layer": L,
                                 "gap_to_pad": round(gap, 4), "required": need})
    near.sort(key=lambda b: b["gap_to_pad"])
    names = ", ".join(dict.fromkeys(b["item"] for b in near[:4])) or "the board edge"
    return {"rule": "track_start_blocked", "severity": "error",
            "message": f"a {width} mm track cannot start anywhere on {pad.ref} ({pad.net}): too close to {names}",
            "at": list(pad.at), "items": [pad.ref], "net": pad.net, "width": width, "blockers": near[:5],
            "fix": f"largest width that fits is {largest} mm" if largest else "no track fits at this clearance"}


def _farthest(a, b) -> float:
    """The largest distance between a point of a and a point of b, which lies between corners."""
    pa = shapely.get_coordinates(a.convex_hull)
    pb = shapely.get_coordinates(b.convex_hull)
    return float(np.sqrt(((pa[:, None, :] - pb[None, :, :]) ** 2).sum(axis=2)).max())


def _via_in_pad(space: _Space, s_pad: Copper, diameter: float, drill: float, layers: list[str]) -> bool:
    """A via inside the pad joins it without any track."""
    return any(not space.via_spots(s_pad.net, s_pad.geom[L], diameter, drill).is_empty for L in layers)


def _conflicts(space: _Space, starts: list[_Start]) -> list[dict]:
    """Pairs on different nets with one possible layer each, the same one, where even the two spots
    farthest apart leave too little room between the tracks."""
    single = [s for s in starts if len(s.regions) == 1]
    if len(single) < 2:
        return []
    out = []
    reach = max(s.width for s in single) + space.worst
    regions = [next(iter(s.regions.values())) for s in single]
    first, second = STRtree(regions).query(regions, predicate="dwithin", distance=reach)
    for i, j in zip(first.tolist(), second.tolist()):
        a, b = single[i], single[j]
        if i >= j or a.pad.net == b.pad.net or next(iter(a.regions)) != next(iter(b.regions)):
            continue
        need = space.model.clearance(a.pad.net, b.pad.net)
        gap = _farthest(regions[i], regions[j]) - a.width / 2 - b.width / 2
        if gap >= need - EPS:
            continue
        fits = [s for s in (a, b) if _via_in_pad(space, s.pad, s.via_diameter, s.via_drill, list(s.regions))]
        if fits and space.via_in_pad:
            continue
        out.append({"rule": "track_start_conflict", "severity": "error",
                    "via_in_pad_fits": [s.pad.ref for s in fits],
                    "message": f"tracks from {a.pad.ref} ({a.pad.net}, {a.width} mm) and {b.pad.ref} ({b.pad.net}, "
                               f"{b.width} mm) are at most {round(gap, 4)} mm apart wherever they start; "
                               f"{need} mm required",
                    "at": list(a.pad.at), "items": [a.pad.ref, b.pad.ref], "actual": round(gap, 4), "required": need})
    return out


# ---------- stage 2: one track on its own ----------

def _search(space: _Space, s: _Start) -> str:
    """What the track can reach from its start: same_net, via, open or enclosed."""
    model, pad = space.model, s.pad
    x1, y1, x2, y2 = pad.geom[next(iter(s.regions))].bounds
    extent = max(x2 - x1, y2 - y1)
    centre = Point((x1 + x2) / 2, (y1 + y2) / 2)
    result = "enclosed"
    for radius in RADII:
        circle = centre.buffer(radius + extent, quad_segs=16)
        s.spots = None
        result = "enclosed"
        for L, start in s.regions.items():
            free = space.free(L, pad.net, space.inside_edge(circle, s.width / 2), s.width / 2)
            parts = [p for p in _pieces(free) if p.distance(start) <= EPS]
            for part in parts:
                for c in model.near(L, part, 0.01):
                    if c.net == pad.net and c.id != pad.id and part.intersects(c.geom[L]):
                        return "same_net"
                if s.spots is None:
                    s.spots = space.via_spots(pad.net, circle, s.via_diameter, s.via_drill)
                    if not space.via_in_pad:
                        s.spots = s.spots.difference(unary_union(list(pad.geom.values())))
                if not s.spots.is_empty and part.intersects(s.spots):
                    return "via"
                if part.distance(circle.exterior) < 0.01:
                    result = "open"
        if result == "enclosed":
            return result           # nothing leaves this circle, so a larger one adds nothing
    return result


# ---------- stage 3: groups of tracks ----------

class _Flow:
    """Dinic's max-flow, with the residual graph kept to read the cut from."""

    def __init__(self):
        self.to, self.left, self.adjacent = [], [], []

    def node(self) -> int:
        self.adjacent.append([])
        return len(self.adjacent) - 1

    def edge(self, a: int, b: int, forward: int, backward: int = 0) -> None:
        self.adjacent[a].append(len(self.to))
        self.to.append(b)
        self.left.append(forward)
        self.adjacent[b].append(len(self.to))
        self.to.append(a)
        self.left.append(backward)

    def _levels(self, source: int) -> list[int]:
        level = [-1] * len(self.adjacent)
        level[source] = 0
        queue = deque([source])
        while queue:
            a = queue.popleft()
            for e in self.adjacent[a]:
                if self.left[e] > 0 and level[self.to[e]] < 0:
                    level[self.to[e]] = level[a] + 1
                    queue.append(self.to[e])
        return level

    def run(self, source: int, sink: int) -> int:
        total = 0
        while True:
            level = self._levels(source)
            if level[sink] < 0:
                return total
            position = [0] * len(self.adjacent)
            while True:                     # one augmenting path at a time, iteratively
                path, a = [], source
                while a != sink:
                    edges = self.adjacent[a]
                    while position[a] < len(edges):
                        e = edges[position[a]]
                        if self.left[e] > 0 and level[self.to[e]] == level[a] + 1:
                            break
                        position[a] += 1
                    if position[a] == len(edges):
                        if not path:
                            break
                        level[a] = -1
                        a = self.to[path.pop() ^ 1]
                        continue
                    path.append(edges[position[a]])
                    a = self.to[edges[position[a]]]
                if a != sink:
                    break
                pushed = min(self.left[e] for e in path)
                for e in path:
                    self.left[e] -= pushed
                    self.left[e ^ 1] += pushed
                total += pushed

    def reachable(self, source: int) -> list[bool]:
        return [v >= 0 for v in self._levels(source)]


def _clusters(demands: list[_Start]) -> list[list[_Start]]:
    shapes = [unary_union(list(s.regions.values())) for s in demands]
    parent = list(range(len(demands)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    if len(shapes) > 1:
        first, second = STRtree(shapes).query(shapes, predicate="dwithin", distance=CLUSTER_GAP)
        for i, j in zip(first.tolist(), second.tolist()):
            parent[find(i)] = find(j)
    groups: dict[int, list[_Start]] = {}
    for i, s in enumerate(demands):
        groups.setdefault(find(i), []).append(s)
    return list(groups.values())


def _via_limit(spots, diameter: float, clearance: float) -> int:
    """More vias than this cannot fit: their centres keep a pitch apart, so discs of half a pitch
    around them are disjoint, and lie within the spots grown by half a pitch."""
    pitch = diameter + clearance
    return math.ceil(spots.buffer(pitch / 2).area / (math.pi * (pitch / 2) ** 2))


def _triangles(space: _Space, layer: str, window, nets: list[str]):
    """The free space on a layer as triangles over the fixed copper's outlines.

    Returns the corners per triangle, each corner's obstacle (-1 for the window), the obstacles,
    and the triangles' shapes. Delaunay does not follow outlines, so an edge can cross an obstacle;
    it then counts as wider than the gap it spans, which only makes the bound more generous.
    """
    obstacles = [c for c in space.near(layer, window.bounds, 0) if space.fixed(c)
                 and c.geom[layer].intersects(window)]
    outlines = [shapely.segmentize(c.geom[layer].boundary, SAMPLE) for c in obstacles]
    outlines.append(shapely.segmentize(window.boundary, 1.0))
    coords, index = shapely.get_coordinates(outlines, return_index=True)
    who = np.where(index == len(obstacles), -1, index)
    coords = np.round(coords, 5)
    points, first = np.unique(coords, axis=0, return_index=True)
    owner = who[first]
    parts = shapely.get_parts(shapely.delaunay_triangles(shapely.multipoints(points)))
    corners = shapely.get_coordinates(parts).reshape(-1, 4, 2)[:, :3]
    flat = np.round(corners.reshape(-1, 2), 5)
    _, inverse = np.unique(np.vstack([points, flat]), axis=0, return_inverse=True)
    inverse = inverse.ravel()
    lookup = np.full(inverse.max() + 1, -1)
    lookup[inverse[:len(points)]] = np.arange(len(points))
    ids = lookup[inverse[len(points):]].reshape(-1, 3)
    known = (ids >= 0).all(axis=1)
    ids, parts = ids[known], parts[known]
    same = (owner[ids[:, 0]] == owner[ids[:, 1]]) & (owner[ids[:, 1]] == owner[ids[:, 2]]) & (owner[ids[:, 0]] >= 0)
    keep = np.ones(len(ids), dtype=bool)
    for t in np.nonzero(same)[0]:               # a triangle wholly inside an obstacle is no free space
        c = obstacles[owner[ids[t, 0]]]
        if space.grown_exact(c, layer).covers(parts[t]):
            keep[t] = False
    return ids[keep], owner, points, obstacles, parts[keep]


def _edges(ids):
    """Each edge shared by two triangles, as corner ids and the two triangle indexes."""
    n = int(ids.max()) + 1
    pairs = np.vstack([ids[:, [0, 1]], ids[:, [1, 2]], ids[:, [0, 2]]])
    low, high = pairs.min(axis=1), pairs.max(axis=1)
    keys = low * n + high
    triangle = np.tile(np.arange(len(ids)), 3)
    order = np.argsort(keys, kind="stable")
    sorted_keys = keys[order]
    match = np.nonzero(sorted_keys[1:] == sorted_keys[:-1])[0]
    a, b = order[match], order[match + 1]
    return low[a], high[a], triangle[a], triangle[b]


def _capacity(space: _Space, group: list[_Start], width: float) -> dict | None:
    """Max-flow of the nets using at least this width, out of the window around the group."""
    run = [s for s in group if s.width >= width]
    by_net: dict[str, list[_Start]] = {}
    for s in run:
        by_net.setdefault(s.pad.net, []).append(s)
    if len(by_net) < 2:
        return None
    nets = sorted(by_net)
    between_tracks = min(space.model.clearance(a, b) for a in nets for b in nets if a != b)
    x1, y1, x2, y2 = shapely.total_bounds([r for s in group for r in s.regions.values()])
    window = box(x1 - WINDOW_MARGIN, y1 - WINDOW_MARGIN, x2 + WINDOW_MARGIN, y2 + WINDOW_MARGIN)

    flow = _Flow()
    source, sink = flow.node(), flow.node()
    net_node = {n: flow.node() for n in nets}
    for n in nets:
        flow.edge(source, net_node[n], 1)

    # via spots, as one sink per connected area, limited by how many vias could possibly fit there
    spots = [space.spots_in_window(s, window) for s in run]
    spots = [g for g in spots if not g.is_empty]
    areas = _pieces(shapely.union_all(spots)) if spots else []
    via = min(s.via_diameter for s in run)
    area_nodes = []
    for area in areas:
        node = flow.node()
        area_nodes.append(node)
        flow.edge(node, sink, _via_limit(area, via, between_tracks))
    area_tree = STRtree(areas) if areas else None

    side_by_net: dict[str, float] = {}

    def side(net: str) -> float:
        if net not in side_by_net:
            side_by_net[net] = min(space.model.clearance(n, net) for n in nets)
        return side_by_net[net]

    gaps = []
    for layer in sorted({L for s in run for L in s.regions}):
        ids, owner, points, obstacles, shapes = _triangles(space, layer, window, nets)
        if not len(ids):
            continue
        base = len(flow.adjacent)
        for _ in range(len(ids)):
            flow.node()
        for t in np.nonzero((owner[ids] == -1).any(axis=1))[0].tolist():
            flow.edge(base + t, sink, INFINITE)
        corner_owner = owner[ids]
        for t, who in enumerate(corner_owner.tolist()):
            for i in set(who) - {-1}:
                node = net_node.get(obstacles[i].net)
                if node is not None:
                    flow.edge(node, base + t, INFINITE)
        if area_tree is not None:
            hits, hit_areas = area_tree.query(shapes, predicate="intersects")
            for t, a in zip(hits.tolist(), hit_areas.tolist()):
                flow.edge(base + t, area_nodes[a], INFINITE)

        low, high, t1, t2 = _edges(ids)
        sides = np.array([side(c.net) for c in obstacles] + [0.0])      # -1 picks the window: no clearance
        lengths = np.hypot(*(points[low] - points[high]).T)
        through = np.floor((lengths - sides[owner[low]] - sides[owner[high]] + between_tracks + EPS)
                           / (width + between_tracks)).astype(int).clip(min=0)
        for e in np.nonzero((owner[low] == owner[high]) & (owner[low] >= 0))[0]:
            c = obstacles[owner[low[e]]]
            if space.grown_exact(c, layer).covers(LineString([points[low[e]], points[high[e]]])):
                through[e] = 0      # along an obstacle's own outline: nothing passes
        for a, b, k in zip(t1.tolist(), t2.tolist(), through.tolist()):
            if k:
                flow.edge(base + a, base + b, k, k)
        gaps.append((base, t1, t2, owner[low], owner[high], through, obstacles))

    for s in run:                   # a via inside the pad itself needs no track at all
        if area_tree is not None and space.via_in_pad:
            for region in s.regions.values():
                for hit in area_tree.query(region, predicate="intersects").tolist():
                    flow.edge(net_node[s.pad.net], area_nodes[hit], INFINITE)

    got = flow.run(source, sink)
    if got >= len(nets):
        return None
    reachable = np.array(flow.reachable(source))
    trapped = [n for n in nets if reachable[net_node[n]]]
    pads = sorted(s.pad.ref for n in trapped for s in by_net[n])
    limits = {}
    for base, t1, t2, oa, ob, through, obstacles in gaps:
        for e in np.nonzero(reachable[base + t1] != reachable[base + t2])[0].tolist():
            if oa[e] >= 0 and ob[e] >= 0 and oa[e] != ob[e]:
                ca, cb = obstacles[oa[e]], obstacles[ob[e]]
                limits[tuple(sorted((ca.ref or ca.id, cb.ref or cb.id)))] = int(through[e])
    return {"rule": "nets_trapped", "severity": "error",
            "message": f"{len(trapped)} nets at {width} mm need a way out here, but only "
                       f"{len(trapped) - (len(nets) - got)} tracks can leave: {', '.join(trapped[:8])}",
            "at": [round((x1 + x2) / 2, 3), round((y1 + y2) / 2, 3)], "items": pads, "nets": trapped,
            "shortfall": len(nets) - got,
            "limits": [{"between": list(k), "tracks": v} for k, v in sorted(limits.items())[:12]]}


# ---------- all together ----------

def check(model: BoardModel, rules: dict, nets: list[str] | None = None, references: list[str] | None = None,
          edge_clearance: float = 0.0, hole_to_hole: float = 0.0, layers: list[str] | None = None,
          via_in_pad: bool = False) -> dict:
    """Every provable routing error. rules maps a net to its track_width, via_diameter and via_drill.

    edge_clearance and hole_to_hole are the board's own limits: a value above them could report
    what is not an error. layers are the ones tracks may start on, by default the outer two, since
    an inner layer is usually a plane and would always have room. via_in_pad counts a via inside a
    pad as a way to join it, off by default as in plan_fanout; a finding says when one would fit.
    Findings naming none of the given nets or references are left out; everything else still counts.
    """
    if nets is not None and (unknown := sorted(set(nets) - model.nets)):
        raise ValueError(f"no such nets on this board: {unknown}")
    pads = [c for c in model.items if c.kind == "pad" and c.ref]
    if references is not None:
        known = {p.ref.split(".")[0] for p in pads}
        if unknown := sorted(set(references) - known):
            raise ValueError(f"no such references on this board: {unknown}")
    layers = layers or [model.copper_layers[0], model.copper_layers[-1]]
    space = _Space(model, edge_clearance, hole_to_hole, via_in_pad)

    def selected(pad_ref: str, net: str) -> bool:
        return (nets is None or net in nets) and (references is None or pad_ref.split(".")[0] in references)

    per_net: dict[str, int] = {}
    for p in pads:
        per_net[p.net] = per_net.get(p.net, 0) + 1

    findings, starts, joined, checked = [], [], 0, 0
    for p in pads:
        if not p.net or per_net[p.net] < 2 or p.net not in rules:
            continue
        on = [L for L in p.geom if L in layers]
        if not on:
            continue
        if _joined(model, p):
            joined += selected(p.ref, p.net)
            continue
        checked += selected(p.ref, p.net)
        r = rules[p.net]
        regions = _regions(space, p, r.track_width, on)
        if regions:
            starts.append(_Start(p, r.track_width, r.via_diameter, r.via_drill, regions))
        elif selected(p.ref, p.net):
            fits = _via_in_pad(space, p, r.via_diameter, r.via_drill, on)
            if not (fits and via_in_pad):
                findings.append({**_blocked(space, p, r.track_width, on), "via_in_pad_fits": fits})
    findings += [f for f in _conflicts(space, starts) if any(selected(i, _net(starts, i)) for i in f["items"])]

    demands = []
    for s in starts:
        s.reached = _search(space, s)
        if s.reached == "enclosed":
            if selected(s.pad.ref, s.pad.net):
                findings.append({
                    "rule": "pad_walled_in", "severity": "error",
                    "message": f"a {s.width} mm track from {s.pad.ref} ({s.pad.net}) cannot reach a via spot, "
                               "copper of its net or open board: everything around it is too close",
                    "at": list(s.pad.at), "items": [s.pad.ref], "net": s.pad.net, "width": s.width})
        elif s.reached != "same_net":
            demands.append(s)

    for group in _clusters(demands) if demands else []:
        if len({s.pad.net for s in group}) < 2:
            continue
        for width in sorted({s.width for s in group}, reverse=True):
            found = _capacity(space, group, width)
            if found:
                if any(selected(i, _net(starts, i)) for i in found["items"]):
                    findings.append(found)
                break

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    return {"checked": checked, "already_joined": joined, "errors": len(findings), "by_rule": counts,
            "findings": findings}


def _net(starts: list[_Start], ref: str) -> str:
    return next((s.pad.net for s in starts if s.pad.ref == ref), "")
