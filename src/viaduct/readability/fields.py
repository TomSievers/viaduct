"""New positions for symbol fields that wire_through_text or text_overlap flag. Read-only.

A field is flagged by the same geometry those rules use. For each one, grid positions
around its symbol are tried in both orientations, nearest first; the first position that
is clear of every wire, drawn line, sheet box and other text by margin, and stays nearer
its own symbol than any other, wins. Planned moves count as obstacles for the fields
planned after them, so two fields never land on the same spot. A field that fits nowhere
is reported with its length, so the caller can see that the parts need more room, not the
text another place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.strtree import STRtree

from viaduct.readability.rules.base import SHRINK
from viaduct.readability.sheet import Sheet, Text, field_box

GRID = 0.635                # half the schematic grid: KiCad's own field placement step


@dataclass
class Move:
    text: Text
    at: tuple[float, float]
    angle: float


def flagged(sheet: Sheet, wire_clearance: float, text_clearance: float) -> list[int]:
    """Indexes into sheet.texts of the symbol fields the two text rules report."""
    wires = STRtree([w.line for w in sheet.wires])
    lines = [s.strokes for s in sheet.symbols if not s.strokes.is_empty] + [b.box for b in sheet.sheets]
    grow = text_clearance / 2 - SHRINK
    areas = [t.box.buffer(grow, join_style="mitre") for t in sheet.texts]
    texts = STRtree(areas)
    out = []
    for i, t in enumerate(sheet.texts):
        if t.kind != "field" or t.owner is None:
            continue
        if len(wires.query(t.box.buffer(wire_clearance - SHRINK, join_style="mitre"), predicate="intersects")):
            out.append(i)
        elif any(j != i for j in texts.query(areas[i], predicate="intersects")):
            out.append(i)
        elif any(areas[i].intersects(g) for g in lines):
            out.append(i)
    return [i for i in out if _symbol(sheet, sheet.texts[i]) is not None]


def plan(sheet: Sheet, wire_clearance: float = 0.2, text_clearance: float = 0.0, margin: float = 0.25,
         radius: float = 20.0, reach: float = 2.5) -> tuple[list[Move], list[dict]]:
    """Moves for every flagged field, and the fields no position fits."""
    static = [w.line for w in sheet.wires] + [s.strokes for s in sheet.symbols if not s.strokes.is_empty]
    static += [b.box for b in sheet.sheets]
    obstacles = STRtree(static)
    boxes = [t.box for t in sheet.texts]              # updated as fields are planned
    bodies = [s for s in sheet.symbols if s.body is not None]
    body_tree = STRtree([s.body for s in bodies])
    steps = int(radius / GRID)

    moves, unplaced = [], []
    for i in flagged(sheet, wire_clearance, text_clearance):
        t = sheet.texts[i]
        sym = _symbol(sheet, t)
        others = STRtree([b for j, b in enumerate(boxes) if j != i])
        # nearest first: distance from where the field is, plus a step's worth for turning it
        candidates = sorted(
            (math.dist((x, y), t.at) + (2 * GRID if a != t.angle else 0), x, y, a)
            for dx in range(-steps, steps + 1) for dy in range(-steps, steps + 1)
            for x, y in [(round(sym.at[0] + dx * GRID, 3), round(sym.at[1] + dy * GRID, 3))]
            for a in (t.angle, (t.angle + 90) % 180))
        best = None
        for bound, x, y, a in candidates:
            if best is not None and bound >= best[0]:
                break
            b = field_box(t.text, t.font, t.justify, a, (x, y), sym.turn)
            room = b.buffer(margin)
            if len(obstacles.query(room, predicate="intersects")) or len(others.query(room, predicate="intersects")):
                continue
            own = b.distance(sym.body) if sym.body is not None else 0.0
            if own > reach:
                continue
            near = body_tree.query(b, predicate="dwithin", distance=own + 0.5)
            if any(bodies[k] is not sym for k in near):
                continue                    # it would read as another part's text
            if best is None or bound + own < best[0]:
                best = (bound + own, x, y, a, b)
        if best is None:
            w = t.box.bounds
            unplaced.append({"reference": t.owner, "field": t.name, "text": t.text,
                             "length_mm": round(max(w[2] - w[0], w[3] - w[1]), 2),
                             "reason": f"no spot within {radius:g} mm of the symbol is {margin:g} mm clear of "
                                       f"wires, lines and text and nearest its own symbol: give the parts more "
                                       f"room, or shorten or hide the field"})
            continue
        _, x, y, a, b = best
        boxes[i] = b
        moves.append(Move(t, (x, y), a))
    return moves, unplaced


def _symbol(sheet: Sheet, t: Text):
    return next((s for s in sheet.symbols if s.ref == t.owner and s.unit == t.unit), None)
