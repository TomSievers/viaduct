"""Which copper of a net is actually joined together.

Two items of the same net are connected when their shapes touch on a shared
layer. Zone fills take part through their filled polygons (thermal spokes
included), vias and through-hole pads join all layers they span.
"""

from __future__ import annotations

from shapely.ops import unary_union

from viaduct.model import BoardModel, Copper


def islands(model: BoardModel, net: str) -> list[dict]:
    members = [c for c in model.items if c.net == net]
    parent = list(range(len(members)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    pos = {id(c): i for i, c in enumerate(members)}
    for L in model.copper_layers:
        for i, c in enumerate(members):
            if L not in c.geom:
                continue
            for other in model.near(L, c.geom[L], 0.0):
                if other.net != net or other is c:
                    continue
                if c.geom[L].intersects(other.geom[L]):
                    a, b = find(i), find(pos[id(other)])
                    if a != b:
                        parent[a] = b

    groups: dict[int, list[Copper]] = {}
    for i, c in enumerate(members):
        groups.setdefault(find(i), []).append(c)

    result = []
    for g in groups.values():
        pads = sorted(c.ref for c in g if c.kind == "pad" and c.ref)
        kinds: dict[str, int] = {}
        for c in g:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        bounds = unary_union([s for c in g for s in c.geom.values()]).bounds
        result.append({"pads": pads, "counts": kinds,
                       "bbox": [round(b, 3) for b in bounds],
                       "item_ids": [c.id for c in g]})
    result.sort(key=lambda r: (len(r["pads"]), sum(r["counts"].values())), reverse=True)
    for i, r in enumerate(result):
        r["main"] = i == 0
    return result
