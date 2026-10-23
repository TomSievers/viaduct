"""Board model: copper geometry in millimetres, independent of KiCad.

kicad_io builds a BoardModel from the live board; everything else (plan
checking, connectivity, routing helpers) works on this model, so it can be
tested without KiCad running.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree


@dataclass
class Copper:
    """One piece of copper: a pad, track, arc, via or filled zone."""

    id: str
    kind: str                      # pad | track | arc | via | zone
    net: str                       # "" for unconnected copper
    geom: dict[str, BaseGeometry]  # layer name -> shape on that layer
    ref: str | None = None         # "U10.47" for pads, zone name for zones
    drill: float | None = None     # drill diameter, pads and vias only
    at: tuple[float, float] | None = None
    width: float | None = None     # tracks only

    @property
    def layers(self) -> list[str]:
        return list(self.geom)


@dataclass
class Hole:
    x: float
    y: float
    r: float
    owner: str


@dataclass
class BoardModel:
    copper_layers: list[str]
    items: list[Copper]
    holes: list[Hole] = field(default_factory=list)
    outline: BaseGeometry | None = None          # board shape from Edge.Cuts
    net_clearance: dict[str, float] = field(default_factory=dict)
    default_clearance: float = 0.2
    nets: set[str] = field(default_factory=set)

    def __post_init__(self):
        self._index: dict[str, tuple[STRtree, list[Copper]]] = {}
        self.by_id = {c.id: c for c in self.items}

    def clearance(self, net_a: str, net_b: str) -> float:
        """KiCad uses the larger of the two netclass clearances."""
        return max(self.net_clearance.get(net_a, self.default_clearance),
                   self.net_clearance.get(net_b, self.default_clearance))

    def layer_index(self, layer: str) -> tuple[STRtree, list[Copper]]:
        """Spatial index of all copper on one layer (zones included)."""
        if layer not in self._index:
            members = [c for c in self.items if layer in c.geom]
            self._index[layer] = (STRtree([c.geom[layer] for c in members]), members)
        return self._index[layer]

    def fingerprint(self) -> str:
        """Hash of all pads, tracks, arcs and vias (zones excluded: refills change them).

        Any edit that moves, adds, deletes or re-nets copper changes it, so a plan checked
        against one fingerprint is never applied to a board that changed underneath it.
        """
        h = hashlib.sha1()
        for c in sorted((c for c in self.items if c.kind != "zone"), key=lambda c: c.id):
            h.update(f"{c.id}|{c.kind}|{c.net}".encode())
            for L in sorted(c.geom):
                h.update(f"|{L}:{','.join(f'{v:.4f}' for v in c.geom[L].bounds)}".encode())
        h.update(",".join(self.copper_layers).encode())
        return h.hexdigest()[:16]

    def near(self, layer: str, geom: BaseGeometry, margin: float) -> list[Copper]:
        tree, members = self.layer_index(layer)
        return [members[i] for i in tree.query(geom.buffer(margin))]
