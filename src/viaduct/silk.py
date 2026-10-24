"""Reference designator placement on the silkscreen, independent of KiCad (all mm).

kicad_io builds a SilkScene from the live board; place() finds a readable spot next to each
part for its Reference text, or hides it when nothing fits. A spot is readable when the text:

- sits outside its own courtyard, at most max_gap from it, and closer to its own part than to
  any other part (so it is not read as a neighbour's label);
- keeps clear of exposed pads, other silkscreen, component bodies (where it would be covered
  once assembled), the board edge, and the other references.

Candidates are tried on a grid around each part, cheapest first: close to the part, centred
on a side, horizontal, at the current size (shrinking is preferred over rotating). Parts with the fewest free spots are placed first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

# KiCad's stroke font: a character advances about 0.82 x its width, plus the stroke itself
CHAR_ADVANCE = 0.82


@dataclass
class Label:
    """A footprint's Reference text, to be placed."""

    ref: str
    side: str                                  # F | B
    text: str                                  # what is printed, usually ref
    anchor: tuple[float, float, float, float]  # the part's courtyard bounds
    size: float                                # character height now
    thickness: float


@dataclass
class SilkScene:
    """What a reference must stay clear of, per side ("F" / "B")."""

    outline: BaseGeometry | None
    labels: list[Label]
    pads: dict[str, list[BaseGeometry]] = field(default_factory=dict)     # exposed copper
    silk: dict[str, list[BaseGeometry]] = field(default_factory=dict)     # drawn silkscreen, stroked
    bodies: dict[str, list[BaseGeometry]] = field(default_factory=dict)   # component bodies (fab outline)
    texts: dict[str, list[BaseGeometry]] = field(default_factory=dict)    # silk text that stays put
    anchors: dict[str, list[tuple[str, tuple]]] = field(default_factory=dict)  # every part's courtyard bounds


@dataclass
class Rules:
    sizes: tuple[float | None, ...] = (None, 0.8)  # None = the text's current size
    text_clearance: float = 0.5   # between references, so neighbours read as separate labels
    pad_clearance: float = 0.15
    silk_clearance: float = 0.12
    edge_clearance: float = 0.35
    max_gap: float = 1.2          # farthest a text may sit from its part's courtyard
    rotate: bool = True           # also try vertical text
    step: float = 0.05            # candidate grid; tight gaps between parts are often under 0.1


def text_box(text: str, x: float, y: float, size: float, thickness: float, rotation: float):
    """Estimated extent of a stroke-font text centred on x, y, at 0 or 90 degrees."""
    w, h = len(text) * size * CHAR_ADVANCE + thickness, size + thickness
    if round(rotation) % 180 == 90:
        w, h = h, w
    return box(x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def _bounds_gap(r, a) -> float:
    dx = max(0.0, max(r[0], a[0]) - min(r[2], a[2]))
    dy = max(0.0, max(r[1], a[1]) - min(r[3], a[3]))
    return float(np.hypot(dx, dy))


def _candidates(label: Label, rules: Rules) -> dict[str, np.ndarray]:
    """Every spot around the part as arrays: cost, x, y, rotation, size, half width, half height."""
    ax1, ay1, ax2, ay2 = label.anchor
    acx, acy = (ax1 + ax2) / 2, (ay1 + ay2) / 2
    parts = []
    sizes = [label.size if s is None else s for s in rules.sizes]
    for n, size in enumerate(dict.fromkeys(sizes)):
        # a smaller horizontal text reads better than a rotated one
        size_pen = 0.0 if size >= label.size else 0.6 * n
        for rot, rot_pen in ((0, 0.0), (90, 1.5)) if rules.rotate else ((0, 0.0),):
            b = text_box(label.text, 0, 0, size, label.thickness, rot).bounds
            hw, hh = b[2], b[3]
            reach = rules.max_gap + 0.01
            xs = np.arange(ax1 - reach - hw, ax2 + reach + hw + 1e-9, rules.step)
            ys = np.arange(ay1 - reach - hh, ay2 + reach + hh + 1e-9, rules.step)
            X, Y = (a.ravel() for a in np.meshgrid(xs, ys))
            gap = np.hypot(np.maximum(0, np.maximum(ax1 - (X + hw), (X - hw) - ax2)),
                           np.maximum(0, np.maximum(ay1 - (Y + hh), (Y - hh) - ay2)))
            ok = (gap > 0) & (gap <= rules.max_gap)   # outside its courtyard, but near it
            X, Y, gap = X[ok], Y[ok], gap[ok]
            # horizontal text reads best centred above or below, vertical text beside the part
            along = np.abs(X - acx) if rot == 0 else np.abs(Y - acy)
            k = len(X)
            parts.append({"cost": gap * 3 + rot_pen + size_pen + 0.15 * along, "x": X.round(3), "y": Y.round(3),
                          "rotation": np.full(k, float(rot)), "size": np.full(k, size),
                          "hw": np.full(k, hw), "hh": np.full(k, hh)})
    c = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    order = np.argsort(c["cost"], kind="stable")
    return {key: v[order] for key, v in c.items()}


def _gaps(px: np.ndarray, py: np.ndarray, anchors: np.ndarray) -> np.ndarray:
    """Distance from each point to each anchor box: shape (points, anchors)."""
    dx = np.maximum(0, np.maximum(anchors[None, :, 0] - px[:, None], px[:, None] - anchors[None, :, 2]))
    dy = np.maximum(0, np.maximum(anchors[None, :, 1] - py[:, None], py[:, None] - anchors[None, :, 3]))
    return np.hypot(dx, dy)


class _Side:
    """One side's obstacles, indexed, and the references placed on it so far."""

    def __init__(self, scene: SilkScene, side: str, rules: Rules):
        geoms = [g.buffer(rules.pad_clearance) for g in scene.pads.get(side, [])]
        geoms += [g.buffer(rules.silk_clearance) for g in scene.silk.get(side, [])]
        geoms += [g.buffer(-0.05) for g in scene.bodies.get(side, [])]
        geoms += [g.buffer(rules.text_clearance) for g in scene.texts.get(side, [])]
        self.obstacles = STRtree([g for g in geoms if not g.is_empty])
        self.refs = [r for r, _ in scene.anchors.get(side, [])]
        self.anchors = np.array([a for _, a in scene.anchors.get(side, [])], dtype=float).reshape(-1, 4)
        self.placed: list[tuple[float, float, float, float]] = []


def _free(lb: Label, c: dict[str, np.ndarray], side: _Side, inset) -> np.ndarray:
    """Indexes into c of the spots clear of the board edge and every fixed obstacle, and closer
    to lb's own part than to any other, cheapest first."""
    boxes = shapely.box(c["x"] - c["hw"], c["y"] - c["hh"], c["x"] + c["hw"], c["y"] + c["hh"])
    ok = shapely.contains(inset, boxes) if inset is not None else np.ones(len(boxes), bool)
    idx = np.flatnonzero(ok)
    if len(idx):
        hit = side.obstacles.query(boxes[idx], predicate="intersects")[0]
        ok[idx[np.unique(hit)]] = False
    idx = np.flatnonzero(ok)
    if len(idx) and len(side.anchors):
        gaps = _gaps(c["x"][idx], c["y"][idx], side.anchors)
        own = _gaps(c["x"][idx], c["y"][idx], np.array([lb.anchor], dtype=float))[:, 0]
        others = np.array([r != lb.ref for r in side.refs])
        nearest_other = gaps[:, others].min(axis=1) if others.any() else np.full(len(idx), np.inf)
        idx = idx[nearest_other + 0.05 >= own]
    return idx


def place(scene: SilkScene, rules: Rules | None = None, references: list[str] | None = None) -> list[dict]:
    """A spot for each label (or all of `references`), or none.

    Returns rows {reference, placed, x, y, rotation, size, gap} for placed labels and
    {reference, placed: False} for those that fit nowhere.
    """
    rules = rules or Rules()
    inset = scene.outline.buffer(-rules.edge_clearance) if scene.outline is not None else None
    if inset is not None:
        shapely.prepare(inset)
    sides = {s: _Side(scene, s, rules) for s in {lb.side for lb in scene.labels}}
    labels = [lb for lb in scene.labels if references is None or lb.ref in references]

    free = {}
    for lb in labels:
        c = _candidates(lb, rules)
        free[lb.ref] = (c, _free(lb, c, sides[lb.side], inset))

    # the labels with the fewest free spots go first
    rows = []
    m = rules.text_clearance
    for lb in sorted(labels, key=lambda lb: (len(free[lb.ref][1]), lb.ref)):
        c, idx = free[lb.ref]
        side = sides[lb.side]
        pick = None
        for i in idx:
            r = (c["x"][i] - c["hw"][i], c["y"][i] - c["hh"][i], c["x"][i] + c["hw"][i], c["y"][i] + c["hh"][i])
            if all(r[2] + m <= p[0] or p[2] + m <= r[0] or r[3] + m <= p[1] or p[3] + m <= r[1] for p in side.placed):
                pick, box_ = i, r
                break
        if pick is None:
            rows.append({"reference": lb.ref, "placed": False})
            continue
        side.placed.append(box_)
        rows.append({"reference": lb.ref, "placed": True, "x": float(c["x"][pick]), "y": float(c["y"][pick]),
                     "rotation": float(c["rotation"][pick]), "size": float(c["size"][pick]),
                     "gap": round(_bounds_gap(box_, lb.anchor), 3)})
    return sorted(rows, key=lambda r: r["reference"])


def moves(rows: list[dict], hide_unplaced: bool = True) -> list[dict]:
    """move_footprint_texts entries for place()'s rows."""
    out = []
    for r in rows:
        if r["placed"]:
            out.append({"reference": r["reference"], "text": "Reference", "x": r["x"], "y": r["y"],
                        "rotation": r["rotation"], "size": r["size"], "visible": True})
        elif hide_unplaced:
            out.append({"reference": r["reference"], "text": "Reference", "visible": False})
    return out


def stroke(geom: BaseGeometry, width: float) -> BaseGeometry:
    """A drawn line or outline as the area its stroke covers."""
    return shapely.buffer(geom, max(width, 0.01) / 2)
