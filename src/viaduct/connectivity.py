"""Which copper of a net is actually joined together.

Two items of the same net are connected when their shapes touch on a shared
layer. Zone fills take part through their filled polygons (thermal spokes
included), each polygon on its own: KiCad keeps a fill patch on a pad the pour
cannot reach, and that patch joins nothing else. Vias and through-hole pads join
all layers they span.
"""

from __future__ import annotations

from shapely.ops import unary_union
from shapely.strtree import STRtree

from viaduct.model import BoardModel, Copper


def _pieces(c: Copper) -> list[Copper]:
    """A zone as one item per fill polygon and layer; anything else as itself."""
    if c.kind != "zone":
        return [c]
    out = []
    for L, g in c.geom.items():
        for poly in getattr(g, "geoms", [g]):
            out.append(Copper(c.id, "zone", c.net, {L: poly}, ref=c.ref))
    return out


def islands(model: BoardModel, net: str) -> list[dict]:
    members = [p for c in model.items if c.net == net for p in _pieces(c)]
    parent = list(range(len(members)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for L in model.copper_layers:
        on = [i for i, c in enumerate(members) if L in c.geom]
        if not on:
            continue
        tree = STRtree([members[i].geom[L] for i in on])
        for a, b in zip(*tree.query([members[i].geom[L] for i in on], predicate="intersects")):
            ra, rb = find(on[a]), find(on[b])
            if ra != rb:
                parent[ra] = rb

    groups: dict[int, list[Copper]] = {}
    for i, c in enumerate(members):
        groups.setdefault(find(i), []).append(c)

    result = []
    for g in groups.values():
        items = list({c.id: c for c in g}.values())
        pads = sorted(c.ref for c in items if c.kind == "pad" and c.ref)
        kinds: dict[str, int] = {}
        for c in items:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        bounds = unary_union([s for c in g for s in c.geom.values()]).bounds
        zones: dict[str, float] = {}
        for c in g:
            if c.kind == "zone":
                zones[c.id] = zones.get(c.id, 0.0) + sum(s.area for s in c.geom.values())
        result.append({"pads": pads, "counts": kinds, "zone_area": {z: round(a, 3) for z, a in zones.items()},
                       "bbox": [round(b, 3) for b in bounds],
                       "item_ids": [c.id for c in items]})
    result.sort(key=lambda r: (len(r["pads"]), sum(r["counts"].values())), reverse=True)
    for i, r in enumerate(result):
        r["main"] = i == 0
    return result
