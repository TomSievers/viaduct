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


def summary(model: BoardModel, nets: list[str] | None = None, only_broken: bool = False) -> list[dict]:
    """One row per net, by name: its copper and how many groups that copper falls apart in.

    cut_off_pads counts the pads outside the main group, as listed by net_islands.
    """
    unknown = sorted(set(nets or ()) - model.nets)
    if unknown:
        raise ValueError(f"no such nets on this board: {unknown}")
    by_net: dict[str, list[Copper]] = {}
    for c in model.items:
        by_net.setdefault(c.net, []).append(c)
    rows = []
    for net in sorted(nets if nets is not None else model.nets):
        groups = islands(model, net)
        if only_broken and len(groups) < 2:
            continue
        kinds = [c.kind for c in by_net.get(net, [])]
        rows.append({"net": net, "class": model.net_class.get(net), "pads": kinds.count("pad"),
                     "tracks": kinds.count("track") + kinds.count("arc"), "vias": kinds.count("via"),
                     "zones": kinds.count("zone"),
                     "track_mm": round(sum(c.length or 0 for c in by_net.get(net, [])), 3),
                     "islands": len(groups),
                     "cut_off_pads": sum(len(g["pads"]) for g in groups if not g["main"])})
    return rows
