"""text_overlap: text over other text, over a symbol's drawing or over a sheet box."""

from __future__ import annotations

from shapely.strtree import STRtree

from viaduct.readability.rules.base import SHRINK, Rule, shrink


class TextOverlap(Rule):
    """Visible text overlaps other text, crosses a line of a symbol's drawing (its own
    symbol's included, pins too), or lies on another hierarchical sheet's box. Overlapping
    characters, and characters struck through by a line, are unreadable. Text inside a body
    that touches none of its lines, such as a value inside an IC box, is fine. clearance (mm)
    asks for extra room around each text."""
    id = "text_overlap"
    title = "Text overlaps"
    severity = "error"
    options = {"clearance": 0.0}

    def check(self, sheet, options):
        grow = options["clearance"] / 2 - SHRINK        # half from each side of a pair
        texts = [t for t in sheet.texts if not t.box.buffer(grow, join_style="mitre").is_empty]
        areas = [t.box.buffer(grow, join_style="mitre") for t in texts]
        tree = STRtree(areas)
        for i, t in enumerate(texts):
            for j in tree.query(areas[i], predicate="intersects"):
                if j > i:
                    u = texts[j]
                    c = areas[i].intersection(areas[j]).centroid
                    yield self.finding(f"{t.label} overlaps {u.label}", (c.x, c.y),
                                       [t.owner or t.text, u.owner or u.text], "move one of the texts")
        for s in sheet.symbols:
            for i in tree.query(s.strokes, predicate="intersects"):
                t = texts[i]
                what = "the drawing of its own symbol" if t.owner == s.ref else f"the drawing of {s.ref}"
                c = s.strokes.intersection(areas[i]).centroid
                yield self.finding(f"{t.label} overlaps {what}", (c.x, c.y), [t.owner or t.text, s.ref],
                                   "move the text clear of the lines")
        for b in sheet.sheets:
            area = shrink(b.box)
            for i in tree.query(area, predicate="intersects"):
                t = texts[i]
                if t.owner == b.name and t.kind == "field":
                    continue                    # the sheet's own name and file fields
                c = areas[i].intersection(area).centroid
                yield self.finding(f"{t.label} overlaps sheet {b.name}", (c.x, c.y), [t.owner or t.text, b.name],
                                   "move the text clear of the sheet box")
