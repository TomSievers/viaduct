"""New positions for symbol fields that wire_through_text or text_overlap flag. Read-only.

A field is flagged by the same geometry those rules use. Each one is placed by
Space.search: the nearest grid position, in either orientation, that is clear of every
wire, drawn line, pin, sheet box and other text by margin, and stays nearer its own symbol
than any other. Planned moves count as obstacles for the fields planned after them. A field
that fits nowhere is reported with its length: the parts need more room, not the text
another place.
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.strtree import STRtree

from viaduct.readability.rules.base import SHRINK
from viaduct.readability.sheet import Sheet, Symbol, Text, field_box
from viaduct.readability.space import Item, Space

STEP = 0.635                # half the schematic grid: KiCad's own field placement step


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
        if t.kind != "field" or symbol_of(sheet, t) is None:
            continue
        if (len(wires.query(t.box.buffer(wire_clearance - SHRINK, join_style="mitre"), predicate="intersects"))
                or any(j != i for j in texts.query(areas[i], predicate="intersects"))
                or any(areas[i].intersects(g) for g in lines)):
            out.append(i)
    return out


def plan(sheet: Sheet, wire_clearance: float = 0.2, text_clearance: float = 0.0, margin: float = 0.25,
         radius: float = 20.0, reach: float = 2.5, space: Space | None = None) -> tuple[list[Move], list[dict]]:
    """Moves for every flagged field, and the fields no position fits."""
    space = space or Space(sheet)
    moves, unplaced = [], []
    for i in flagged(sheet, wire_clearance, text_clearance):
        t = sheet.texts[i]
        sym = symbol_of(sheet, t)

        def shape(x, y, angle, t=t, sym=sym):
            return field_box(t.text, t.font, t.justify, angle, (x, y), sym.turn)

        def near_own(g, sym=sym):
            own = g.distance(sym.body) if sym.body is not None else 0.0
            if own > reach:
                return None
            others = space.query(g, ("body",), exclude=(sym.ref,), distance=own + 0.5)
            return None if others else own          # it would read as another part's text

        found = space.search(shape, sym.at, radius, STEP, variants=(t.angle, (t.angle + 90) % 180),
                             clearance=margin, kinds=("wire", "line", "text", "sheet", "pin"),
                             exclude=(t.label,), origin=t.at, extra=near_own,
                             variant_cost=lambda a, t=t: 2 * STEP if a != t.angle else 0)
        if not found:
            w = t.box.bounds
            unplaced.append({"reference": t.owner, "field": t.name, "text": t.text,
                             "length_mm": round(max(w[2] - w[0], w[3] - w[1]), 2),
                             "reason": f"no spot within {radius:g} mm of the symbol is {margin:g} mm clear of "
                                       f"wires, lines and text and nearest its own symbol: give the parts more "
                                       f"room, or shorten or hide the field"})
            continue
        c = found[0]
        space.remove([t.label], "text")
        space.add(Item("text", t.label, c.geom, t.owner))
        t.box, t.at, t.angle = c.geom, (c.x, c.y), c.variant
        moves.append(Move(t, (c.x, c.y), c.variant))
    return moves, unplaced


def symbol_of(sheet: Sheet, t: Text) -> Symbol | None:
    return next((s for s in sheet.symbols if s.ref == t.owner and s.unit == t.unit), None)
