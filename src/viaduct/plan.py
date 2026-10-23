"""Copper plans: parse, check against the board model, describe.

A plan is plain JSON in millimetres:

    {
      "delete": ["<uuid>", ...],                       # tracks, arcs, vias or zones
      "tracks": [{"net": "GND", "layer": "F.Cu", "start": [x, y], "end": [x, y], "width": 0.25}],
      "vias":   [{"net": "GND", "at": [x, y], "diameter": 0.6, "drill": 0.3}],
      "zones":  [{"net": "GND", "layer": "In1.Cu", "outline": [[x, y], ...],
                  "name": "GND_In1", "priority": 0, "clearance": 0.2, "min_thickness": 0.2}]
    }

Tracks may also be written {"x1", "y1", "x2", "y2", "w"} and vias {"x", "y"}.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry.base import BaseGeometry

from viaduct import geometry
from viaduct.model import BoardModel

EPS = 0.001          # 1 um tolerance on clearance comparisons
DELETABLE = {"track", "arc", "via", "zone"}


@dataclass
class NewTrack:
    net: str
    layer: str
    x1: float
    y1: float
    x2: float
    y2: float
    width: float

    @property
    def geom(self) -> BaseGeometry:
        return geometry.track(self.x1, self.y1, self.x2, self.y2, self.width)


@dataclass
class NewVia:
    net: str
    x: float
    y: float
    diameter: float
    drill: float

    @property
    def geom(self) -> BaseGeometry:
        return geometry.via(self.x, self.y, self.diameter)


@dataclass
class NewZone:
    net: str
    layer: str
    outline: list[tuple[float, float]]
    name: str = ""
    priority: int = 0
    clearance: float | None = None
    min_thickness: float | None = None


@dataclass
class Plan:
    delete: list[str] = field(default_factory=list)
    tracks: list[NewTrack] = field(default_factory=list)
    vias: list[NewVia] = field(default_factory=list)
    zones: list[NewZone] = field(default_factory=list)

    def summary(self) -> dict:
        return {"delete": len(self.delete), "tracks": len(self.tracks),
                "vias": len(self.vias), "zones": len(self.zones)}


def _pt(d: dict, key: str, xk: str, yk: str) -> tuple[float, float]:
    if key in d:
        return float(d[key][0]), float(d[key][1])
    return float(d[xk]), float(d[yk])


def parse(data: dict, default_via=(0.6, 0.3), default_width=0.25) -> Plan:
    unknown = set(data) - {"delete", "tracks", "segs", "vias", "zones", "fails", "ripped"}
    if unknown:
        raise ValueError(f"unknown plan keys: {sorted(unknown)}")
    plan = Plan(delete=[str(u) for u in data.get("delete", [])])
    for t in data.get("tracks", []) + data.get("segs", []):
        x1, y1 = _pt(t, "start", "x1", "y1")
        x2, y2 = _pt(t, "end", "x2", "y2")
        plan.tracks.append(NewTrack(t["net"], t["layer"], x1, y1, x2, y2,
                                    float(t.get("width", t.get("w", default_width)))))
    for v in data.get("vias", []):
        x, y = _pt(v, "at", "x", "y")
        plan.vias.append(NewVia(v["net"], x, y, float(v.get("diameter", default_via[0])),
                                float(v.get("drill", default_via[1]))))
    for z in data.get("zones", []):
        plan.zones.append(NewZone(z["net"], z["layer"], [tuple(map(float, p)) for p in z["outline"]],
                                  z.get("name", ""), int(z.get("priority", 0)),
                                  z.get("clearance"), z.get("min_thickness")))
    return plan


def check(model: BoardModel, plan: Plan, edge_clearance: float = 0.3,
          hole_to_hole: float = 0.25, layers: list[str] | None = None) -> list[dict]:
    """Every rule the plan would break. Empty list means safe to apply.

    New copper is checked against existing copper of other nets (zones excluded:
    they refill around new copper), against other new copper, against holes and
    against the board edge. `layers` overrides the enabled copper layers, e.g.
    when checking a plan for a stackup that is about to be enabled.
    """
    out: list[dict] = []
    layers = layers or model.copper_layers
    deleted = set(plan.delete)

    for uid in plan.delete:
        item = model.by_id.get(uid)
        if item is None:
            out.append({"rule": "delete_missing", "id": uid})
        elif item.kind not in DELETABLE:
            out.append({"rule": "delete_refused", "id": uid, "kind": item.kind,
                        "why": "only tracks, arcs, vias and zones can be deleted"})

    new: list[tuple[str, str, BaseGeometry, str]] = []   # (label, net, geom, layer)
    for i, t in enumerate(plan.tracks):
        label = f"track[{i}]"
        if t.net not in model.nets:
            out.append({"rule": "unknown_net", "item": label, "net": t.net})
        if t.layer not in layers:
            out.append({"rule": "layer_not_enabled", "item": label, "layer": t.layer})
        new.append((label, t.net, t.geom, t.layer))
    for i, v in enumerate(plan.vias):
        label = f"via[{i}]"
        if v.net not in model.nets:
            out.append({"rule": "unknown_net", "item": label, "net": v.net})
        for L in layers:
            new.append((label, v.net, v.geom, L))
    for i, z in enumerate(plan.zones):
        if z.net not in model.nets:
            out.append({"rule": "unknown_net", "item": f"zone[{i}]", "net": z.net})
        if z.layer not in layers:
            out.append({"rule": "layer_not_enabled", "item": f"zone[{i}]", "layer": z.layer})

    worst = max(model.net_clearance.values(), default=model.default_clearance)
    for label, net, geom, L in new:
        if L not in model.copper_layers:
            continue                      # layer not on the board yet: nothing to collide with
        for c in model.near(L, geom, worst + 0.05):
            if c.net == net or c.id in deleted or c.kind == "zone":
                continue
            need = model.clearance(net, c.net)
            d = geom.distance(c.geom[L])
            if d < need - EPS:
                out.append({"rule": "clearance", "item": label, "layer": L, "net": net,
                            "other": c.ref or c.id, "other_kind": c.kind, "other_net": c.net,
                            "actual": round(d, 4), "required": need})

    for a in range(len(new)):
        la, na, ga, La = new[a]
        for b in range(a + 1, len(new)):
            lb, nb, gb, Lb = new[b]
            if La != Lb or na == nb or la == lb:
                continue
            need = model.clearance(na, nb)
            d = ga.distance(gb)
            if d < need - EPS:
                out.append({"rule": "clearance_new", "item": la, "other": lb, "layer": La,
                            "actual": round(d, 4), "required": need})

    holes = [(h.x, h.y, h.r, h.owner) for h in model.holes if h.owner not in deleted]
    for i, v in enumerate(plan.vias):
        for (hx, hy, hr, owner) in holes:
            gap = math.hypot(hx - v.x, hy - v.y) - hr - v.drill / 2
            if gap < hole_to_hole - EPS:
                out.append({"rule": "hole_to_hole", "item": f"via[{i}]", "other": owner,
                            "actual": round(gap, 4), "required": hole_to_hole})
        holes.append((v.x, v.y, v.drill / 2, f"via[{i}]"))

    if model.outline is not None:
        edge = model.outline.boundary
        for label, net, geom, L in new:
            if not model.outline.contains(geom) or geom.distance(edge) < edge_clearance - EPS:
                out.append({"rule": "board_edge", "item": label, "layer": L,
                            "actual": round(geom.distance(edge), 4), "required": edge_clearance})

    # one finding per (rule, item, other) is enough
    seen, unique = set(), []
    for v in out:
        key = (v["rule"], v.get("item"), v.get("other"), v.get("id"))
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique
