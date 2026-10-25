"""Plane fanout: a via for every SMD pad of a plane net, placed before autorouting (all mm).

Each pad on a net that has a plane (a filled zone on an inner layer) is joined to that plane.
It shares a connection when one is close: a track to a via of its net that is on the plane,
or to a pad already joined to it (existing, or planned earlier in the same run), at most
share_distance long. Otherwise it gets its own straight stub to a new through via that lands
on the plane. Tracks run at multiples of 45 degrees: straight, or one diagonal and one
straight segment. With the vias in place and locked, the plane nets are complete before
Freerouting runs, so it routes only the signals and its result is never patched afterwards.

A spot is legal when the via and the stub keep every clearance plan.check applies (other nets'
copper on every layer, holes, the board edge), the via clears its own and other same-net pads
by pad_gap (unless via_in_pad allows it inside its own pad), and the whole via lies on the
plane's current fill. Among legal spots the cheapest wins:

- short stubs first;
- on parts with many pads (ICs, connectors) towards the part's centre, keeping the outside
  free for the signals that escape there; on small parts (passives) away from the other pads;
- away from other nets' pads, leaving a signal channel past the via where possible.

Pads with the fewest legal spots are placed first, and pads that fit nowhere are tried again
once the others are placed, when there may be a via to share. Through-hole pads already
reach every layer and are skipped, as are pads whose copper already reaches their plane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.ops import unary_union

from viaduct import connectivity
from viaduct.model import BoardModel, Copper
from viaduct.plan import EPS

DIRECTIONS = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in range(0, 360, 45)]


@dataclass
class NetRule:
    track_width: float = 0.25
    via_diameter: float = 0.6
    via_drill: float = 0.3


@dataclass
class Rules:
    fanout_distance: float = 1.5  # farthest a new via may sit from its pad's centre
    share_distance: float = 1.0   # longest track to a shared via or pad (0 = never share)
    pads_per_via: int = 4         # most pads one new via may serve
    step: float = 0.05
    pad_gap: float = 0.1          # via copper to pad copper, keeps a mask web for soldering
    edge_clearance: float = 0.3
    hole_to_hole: float = 0.25
    via_in_pad: bool = False      # allow a via inside a pad large enough to hold it
    channel: float = 0.45         # room past a via for a signal track, where it can be kept
    many_pads: int = 5            # parts with at least this many pads fan out inwards
    nets: dict[str, NetRule] = field(default_factory=dict)
    default: NetRule = field(default_factory=NetRule)

    def net(self, name: str) -> NetRule:
        return self.nets.get(name, self.default)


def plane_layers(model: BoardModel) -> dict[str, list[str]]:
    """Nets with a filled zone on an inner copper layer, and those layers."""
    inner = set(model.copper_layers[1:-1])
    out: dict[str, list[str]] = {}
    for c in model.items:
        if c.kind == "zone" and c.net:
            for L in c.geom:
                if L in inner and L not in out.setdefault(c.net, []):
                    out[c.net].append(L)
    return {n: sorted(ls, key=model.copper_layers.index) for n, ls in out.items() if ls}


def _reached(model: BoardModel, net: str, layers: list[str]) -> set[str]:
    """Ids of the pads and vias whose copper already joins the main fill of a zone of net on one of
    layers (the group holding most of its area, not a patch the pour left on a pad). Other
    zones do not count as a join: a pad touching a top pour that a far-off through-hole pad
    ties to the plane still gets its own via."""
    zones = {c.id for c in model.items if c.kind == "zone" and c.net == net and set(c.geom) & set(layers)}
    planes_only = BoardModel(model.copper_layers, [c for c in model.items if c.kind != "zone" or c.id in zones])
    groups = connectivity.islands(planes_only, net)
    main = {z: max(groups, key=lambda g: g["zone_area"].get(z, 0.0)) for z in zones}
    out = set()
    for g in main.values():
        out |= {i for i in g["item_ids"] if model.by_id[i].kind in ("pad", "via")}
    return out


def _minor(geom) -> float:
    """The narrow side of a pad, so a stub never comes out wider than its pad."""
    r = geom.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        return 0.0
    xs, ys = r.exterior.coords.xy
    sides = [math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i]) for i in range(2)]
    return min(sides)


class _Obstacles:
    """Existing copper per layer (zones left out: they refill around new copper), indexed."""

    def __init__(self, model: BoardModel):
        self.model = model
        self.layers = {}
        for L in model.copper_layers:
            members = [c for c in model.items if L in c.geom and c.kind != "zone"]
            self.layers[L] = (shapely.STRtree([c.geom[L] for c in members]), members)
        self.worst = max(model.net_clearance.values(), default=model.default_clearance)

    def clear(self, geoms: np.ndarray, layer: str, net: str, rules: Rules, skip: set[str]) -> np.ndarray:
        """Which geoms keep their clearance to the copper on layer: other nets by netclass,
        other pads of the same net by pad_gap."""
        ok = np.ones(len(geoms), bool)
        tree, members = self.layers[layer]
        reach = max(self.worst, rules.pad_gap) + 0.01
        gi, mi = tree.query(geoms, predicate="dwithin", distance=reach)
        for g, m in zip(gi, mi):
            if not ok[g]:
                continue
            c = members[m]
            if c.id in skip:
                continue
            if c.net == net:
                if c.kind != "pad":
                    continue
                need = rules.pad_gap
            else:
                need = self.model.clearance(net, c.net)
            if geoms[g].distance(c.geom[layer]) < need - EPS:
                ok[g] = False
        return ok

    def crowding(self, points: np.ndarray, layer_set: list[str], net: str, radius: float, rules: Rules) -> np.ndarray:
        """How far each via eats into the signal channel next to other nets' pads."""
        pen = np.zeros(len(points))
        for L in layer_set:
            tree, members = self.layers[L]
            for p, m in zip(*tree.query(points, predicate="dwithin", distance=radius + self.worst + rules.channel)):
                c = members[m]
                if c.kind == "pad" and c.net != net:
                    room = points[p].distance(c.geom[L]) - radius - self.model.clearance(net, c.net)
                    pen[p] = max(pen[p], rules.channel - room)
        return np.clip(pen, 0, None)


@dataclass
class _Pad:
    pad: Copper
    layer: str
    net: str
    rule: NetRule
    centre: tuple[float, float]
    cand: dict = field(default_factory=dict)  # arrays of legal spots, cheapest first
    own: set[str] = field(default_factory=set)  # this pad and the same-net pads it overlaps
    geom: object = None                         # their copper on this pad's layer, as one shape

    def merge_overlapping(self, obs: "_Obstacles") -> None:
        """Count same-net pads that overlap this one as part of it: a footprint that stacks two
        pads of one net in one place (USB-C A1/B12) would otherwise have each block the other."""
        tree, members = obs.layers[self.layer]
        g = self.pad.geom[self.layer]
        touching = [members[i] for i in tree.query(g, predicate="intersects")]
        mine = [c for c in touching if c.kind == "pad" and c.net == self.net]
        self.own = {c.id for c in mine} | {self.pad.id}
        self.geom = unary_union([c.geom[self.layer] for c in mine] + [g])


def _footprints(model: BoardModel) -> dict[str, tuple[int, tuple[float, float]]]:
    """Per reference: pad count and the centre of its pads."""
    groups: dict[str, list] = {}
    for c in model.items:
        if c.kind == "pad" and c.ref and c.geom:
            groups.setdefault(c.ref.split(".")[0], []).append(next(iter(c.geom.values())))
    out = {}
    for ref, geoms in groups.items():
        x1, y1, x2, y2 = unary_union(geoms).bounds
        out[ref] = (len(geoms), ((x1 + x2) / 2, (y1 + y2) / 2))
    return out


def _candidates(p: _Pad, model: BoardModel, obs: _Obstacles, fill, inset, holes: np.ndarray,
                parts: dict, rules: Rules) -> dict:
    """Every legal via spot around one pad, as arrays sorted by cost."""
    r = p.rule.via_diameter / 2
    pad_geom = p.geom
    cx, cy = p.centre
    count, (fx, fy) = parts.get(p.pad.ref.split(".")[0] if p.pad.ref else "", (1, p.centre))
    ox, oy = cx - fx, cy - fy
    norm = math.hypot(ox, oy)
    ox, oy = (ox / norm, oy / norm) if norm > 0.05 else (0.0, 0.0)

    ts = np.arange(0.0, rules.fanout_distance + 1e-9, rules.step)
    rows = []
    for k, (dx, dy) in enumerate(DIRECTIONS):
        out_dot = dx * ox + dy * oy
        # many-pad parts fan in (signals escape outwards), small parts fan away from their pads
        dir_cost = 1.5 * (1 + out_dot) if count >= rules.many_pads else 1.5 * (1 - out_dot)
        diag = 0.2 if k % 2 else 0.0
        for t in ts:
            rows.append((cx + dx * t, cy + dy * t, t, dir_cost + diag + 2 * t, k))
    a = np.array(rows)
    x, y, t, cost, k = a[:, 0].round(4), a[:, 1].round(4), a[:, 2], a[:, 3], a[:, 4].astype(int)
    pts = shapely.points(x, y)
    vias = shapely.buffer(pts, r, quad_segs=8)

    in_pad = shapely.contains(pad_geom, vias)
    off_pad = shapely.distance(vias, pad_geom) >= rules.pad_gap - EPS
    ok = off_pad | (in_pad if rules.via_in_pad else False)
    ok &= shapely.contains(fill, vias)
    if inset is not None:
        ok &= shapely.contains(inset, vias)
    if len(holes):
        gap = np.hypot(x[:, None] - holes[None, :, 0], y[:, None] - holes[None, :, 1]) - holes[None, :, 2] \
            - p.rule.via_drill / 2
        ok &= (gap >= rules.hole_to_hole - EPS).all(axis=1)

    width = min(p.rule.track_width, max(_minor(p.pad.geom[p.layer]), 0.1))
    stubs = np.array([None] * len(x), dtype=object)
    need = ok & ~in_pad
    if need.any():
        n = int(need.sum())
        ends = np.stack([np.stack([np.full(n, cx), np.full(n, cy)], axis=1), np.stack([x[need], y[need]], axis=1)],
                        axis=1)
        stubs[need] = shapely.buffer(shapely.linestrings(ends), width / 2)
    for L in model.copper_layers:
        idx = np.flatnonzero(ok)
        if not len(idx):
            break
        ok[idx] = obs.clear(vias[idx], L, p.net, rules, p.own)
    idx = np.flatnonzero(ok & ~in_pad)
    if len(idx):
        ok[idx] = obs.clear(stubs[idx], p.layer, p.net, rules, p.own)

    idx = np.flatnonzero(ok)
    cost = cost[idx] + 2 * obs.crowding(pts[idx], model.copper_layers, p.net, r, rules)
    order = idx[np.argsort(cost, kind="stable")]
    return {"x": x[order], "y": y[order], "t": t[order], "dir": k[order], "in_pad": in_pad[order],
            "via": vias[order], "stub": stubs[order], "width": width}


def _conflicts(p: _Pad, via, track, x: float | None, y: float | None, placed: list, model: BoardModel,
               rules: Rules) -> bool:
    """Whether a new via and/or track breaks a clearance to the vias and tracks placed so far."""
    for q, qvia, qtrack, qx, qy in placed:
        if via is not None and qvia is not None and \
                math.hypot(qx - x, qy - y) - q.rule.via_drill / 2 - p.rule.via_drill / 2 < rules.hole_to_hole - EPS:
            return True
        if q.net == p.net:
            continue
        need = model.clearance(p.net, q.net)
        same_layer = q.layer == p.layer
        for mine, theirs, check in ((via, qvia, True), (track, qvia, True), (via, qtrack, True),
                                    (track, qtrack, same_layer)):
            if check and mine is not None and theirs is not None and mine.distance(theirs) < need - EPS:
                return True
    return False


@dataclass
class _Target:
    """Copper of a plane net that is (or will be) on its plane: something a pad may share."""

    label: str
    x: float
    y: float
    layers: set[str]
    copper_id: str | None = None   # the board item it is (a pad, an existing via): a join may touch it
    geom: dict = field(default_factory=dict)
    root: int | None = None        # the planned via it hangs on, for pads_per_via
    pad: bool = True               # a track to a pad is no wider than that pad


def _octilinear(x0: float, y0: float, x1: float, y1: float) -> list[list[tuple[float, float]]]:
    """Paths from one point to another at multiples of 45 degrees: straight, or the two ways
    of one diagonal and one straight segment."""
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) < 1e-6 or abs(dy) < 1e-6 or abs(abs(dx) - abs(dy)) < 1e-6:
        return [[(x0, y0), (x1, y1)]]
    d = min(abs(dx), abs(dy))
    sx, sy = math.copysign(d, dx), math.copysign(d, dy)
    return [[(x0, y0), (x0 + sx, y0 + sy), (x1, y1)], [(x0, y0), (x1 - sx, y1 - sy), (x1, y1)]]


def _length(path) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def _join(p: _Pad, targets: list[_Target], counts: dict[int, int], model: BoardModel, obs: _Obstacles,
          inset, placed: list, rules: Rules):
    """The shortest legal track from p to a target it may share, as (target, path, geom), or None."""
    pad_geom = p.pad.geom[p.layer]
    options = []
    for t in targets:
        if p.layer not in t.layers or (t.root is not None and counts[t.root] >= rules.pads_per_via):
            continue
        if math.hypot(t.x - p.centre[0], t.y - p.centre[1]) > rules.share_distance + EPS:
            continue
        if p.layer in t.geom and t.geom[p.layer].intersects(p.geom):
            options.append((0.0, t, None))       # already touching: no track needed
            continue
        for path in _octilinear(*p.centre, t.x, t.y):
            if _length(path) <= rules.share_distance + EPS:
                options.append((_length(path), t, path))
    width = min(p.rule.track_width, max(_minor(pad_geom), 0.1))
    for length, t, path in sorted(options, key=lambda o: o[0]):
        if path is None:
            return t, None, None
        width_here = min(width, max(_minor(t.geom[p.layer]), 0.1)) if t.pad and p.layer in t.geom else width
        g = shapely.LineString(path).buffer(width_here / 2)
        if inset is not None and not inset.contains(g):
            continue
        skip = p.own | ({t.copper_id} if t.copper_id else set())
        if not obs.clear(np.array([g]), p.layer, p.net, rules, skip)[0]:
            continue
        if _conflicts(p, None, g, None, None, placed, model, rules):
            continue
        return t, (path, width_here), g
    return None


def fanout(model: BoardModel, rules: Rules | None = None, planes: dict[str, list[str]] | None = None,
           references: list[str] | None = None, include_connected: bool = False,
           lock: bool = True) -> dict:
    """Join each SMD pad of a plane net to its plane: through a shared via or pad nearby, or a
    new via. Returns rows per pad, the plan (tracks and vias, locked unless lock=false) and
    errors for planes that cannot be used."""
    rules = rules or Rules()
    planes = planes if planes is not None else plane_layers(model)
    errors, fills = [], {}
    for net, layers in planes.items():
        bad = [L for L in layers if L not in model.copper_layers]
        if bad:
            errors.append(f"{net}: {bad} not enabled copper layers")
            continue
        g = [c.geom[L] for c in model.items if c.kind == "zone" and c.net == net for L in layers if L in c.geom]
        if not g:
            errors.append(f"{net}: no filled zone on {layers}; add one or refill zones first")
            continue
        fills[net] = unary_union(g)
        shapely.prepare(fills[net])
    if errors:
        return {"errors": errors, "rows": [], "plan": {"tracks": [], "vias": []}}

    outer = {model.copper_layers[0], model.copper_layers[-1]}
    pads, targets = [], {}
    for net, layers in planes.items():
        reached = _reached(model, net, layers)
        targets[net] = [_Target(c.ref or f"via {c.id}", *(c.at or next(iter(c.geom.values())).centroid.coords[0]),
                                set(c.geom), c.id, c.geom, pad=c.kind == "pad") for c in model.items if c.id in reached]
        skip = set() if include_connected else reached
        for c in model.items:
            if c.kind != "pad" or c.net != net or c.drill or c.id in skip:
                continue
            if references is not None and (not c.ref or c.ref.split(".")[0] not in references):
                continue
            on = [L for L in c.geom if L in outer]
            if len(on) != 1:
                continue
            g = c.geom[on[0]]
            centre = c.at if c.at and g.contains(shapely.points(*c.at)) else tuple(g.representative_point().coords[0])
            pads.append(_Pad(c, on[0], net, rules.net(net), centre))
    if include_connected:      # a pad planned again must not count as its own connection
        planned = {p.pad.id for p in pads}
        targets = {n: [t for t in ts if t.copper_id not in planned] for n, ts in targets.items()}

    obs = _Obstacles(model)
    inset = model.outline.buffer(-rules.edge_clearance) if model.outline is not None else None
    if inset is not None:
        shapely.prepare(inset)
    holes = np.array([(h.x, h.y, h.r) for h in model.holes], dtype=float).reshape(-1, 3)
    parts = _footprints(model)
    for p in pads:
        p.merge_overlapping(obs)
        p.cand = _candidates(p, model, obs, fills[p.net], inset, holes, parts, rules)

    placed, rows, tracks, vias = [], {}, [], []
    counts: dict[int, int] = {}
    share = rules.share_distance > 0
    pending = sorted(pads, key=lambda p: (len(p.cand["x"]), p.pad.ref or p.pad.id))
    while pending:
        left = []
        for p in pending:
            label = p.pad.ref or p.pad.id
            row = {"pad": label, "net": p.net, "layer": p.layer, "spots": len(p.cand["x"])}
            joined = _join(p, targets[p.net], counts, model, obs, inset, placed, rules) if share else None
            if joined is not None:
                t, path, g = joined
                if path is not None:
                    pts, width = path
                    placed.append((p, None, g, None, None))
                    for a, b in zip(pts, pts[1:]):
                        tracks.append({"net": p.net, "layer": p.layer, "start": [round(a[0], 4), round(a[1], 4)],
                                       "end": [round(b[0], 4), round(b[1], 4)], "width": width, "locked": lock})
                if t.root is not None:
                    counts[t.root] += 1
                targets[p.net].append(_Target(label, *p.centre, {p.layer}, p.pad.id, {p.layer: p.pad.geom[p.layer]},
                                              t.root))
                rows[label] = {**row, "placed": True, "shared": t.label,
                               "track": round(_length(path[0]), 3) if path else 0.0}
                if t.root is not None:
                    rows[label]["via"] = vias[t.root]["at"]
                continue
            c = p.cand
            pick = None
            for i in range(len(c["x"])):
                stub = None if c["in_pad"][i] else c["stub"][i]
                if not _conflicts(p, c["via"][i], stub, c["x"][i], c["y"][i], placed, model, rules):
                    pick = i
                    break
            if pick is None:
                left.append(p)
                rows[label] = {**row, "placed": False}
                continue
            x, y = float(c["x"][pick]), float(c["y"][pick])
            stub = None if c["in_pad"][pick] else c["stub"][pick]
            placed.append((p, c["via"][pick], stub, x, y))
            if stub is not None:
                tracks.append({"net": p.net, "layer": p.layer, "start": [round(p.centre[0], 4), round(p.centre[1], 4)],
                               "end": [x, y], "width": c["width"], "locked": lock})
            vias.append({"net": p.net, "at": [x, y], "diameter": p.rule.via_diameter, "drill": p.rule.via_drill,
                         "locked": lock})
            root = len(vias) - 1
            counts[root] = 1
            targets[p.net] += [_Target(f"new via at {x}, {y}", x, y, set(model.copper_layers), None,
                                       {L: c["via"][pick] for L in model.copper_layers}, root, pad=False),
                               _Target(label, *p.centre, {p.layer}, p.pad.id, {p.layer: p.pad.geom[p.layer]}, root)]
            rows[label] = {**row, "placed": True, "via": [x, y], "stub": round(float(c["t"][pick]), 3),
                           "direction": int(c["dir"][pick]) * 45, "in_pad": bool(c["in_pad"][pick])}
        if len(left) == len(pending):
            break
        pending = left
    return {"errors": [], "planes": planes, "rows": sorted(rows.values(), key=lambda r: r["pad"]),
            "plan": {"tracks": tracks, "vias": vias}}
