"""Solvers for the rules that are errors by default: a plan of Konnect edits per sheet. Read-only.

The solvers run in order on an in-memory copy of the sheet, each seeing what the ones before
it changed:
1. wire_overlap: overlapping collinear wires become one run, split where the old ones ended.
2. wire_through_text and text_overlap on net labels: the label is turned to the nearest
   orientation that is clear (its connection point stays where it is).
3. wire_through_symbol, wire_through_text on labels, free text and sheet fields, and
   wire_over_pin where a wire crosses or runs along a pin: the wire path is routed again
   between its two connection points (Space.route), leaving pins straight outward.
4. missing_junction: a dot wherever three or more wires meet after the steps above.
5. text_overlap and wire_through_text on symbol fields: fields.plan moves the text.
Reported with options instead of solved: a pin on a wire's middle (a junction connects it,
or the wire is routed away: only the designer knows which is meant), symbol_overlap (a
clear spot for one of the symbols is suggested; its wires need redrawing), and free text
over a wire or other text (a clear spot is suggested; Konnect cannot move text).

The plan is applied with Konnect in this order: batch_add_wire, batch_delete_schematic_wire
(which prunes the dots deleted wires leave unjustified, judged with the new wires already in
place), batch_add_junction, batch_rotate_labels, then edit_schematic_component per symbol. "remaining" counts what
the rules still find once the plan is applied, worked out on the in-memory copy.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from shapely.affinity import translate
from shapely.geometry import Point
from shapely.ops import unary_union

from viaduct.readability import fields
from viaduct.readability.rules import BY_ID
from viaduct.readability.rules.base import SHRINK, arms, chain, key, passes_through, same, stops, wire_ends
from viaduct.readability.sheet import TOL, Pt, Sheet, Text, Wire, label_box
from viaduct.readability.space import Item, Space, rect

LABELS = ("label", "global_label", "hierarchical_label")

SOLVED = ("wire_overlap", "wire_through_symbol", "wire_through_text", "wire_over_pin", "missing_junction",
          "text_overlap", "symbol_overlap")


class SheetFix:
    def __init__(self, sheet: Sheet, options: dict[str, dict], rules: set[str]):
        self.sheet, self.options, self.rules = sheet, options, rules
        self.space = Space(sheet)
        self.added: list[Wire] = []
        self.deleted: list[str] = []
        self.junctions: list[Pt] = []
        self.moves: list[fields.Move] = []
        self.turned: list[tuple[Text, float]] = []
        self.unresolved: list[dict] = []
        self.solved: Counter = Counter()
        self._new = 0

    def run(self) -> SheetFix:
        if "wire_overlap" in self.rules:
            self.overlaps()
        if self.rules & {"text_overlap", "wire_through_text"}:
            self.labels()
        self.reroutes()
        if "missing_junction" in self.rules:
            self.dots()
        if self.rules & {"text_overlap", "wire_through_text"}:
            moves, unplaced = fields.plan(self.sheet, self._opt("wire_through_text", "clearance", 0.2),
                                          self._opt("text_overlap", "clearance", 0.0), space=self.space)
            self.moves += moves
            self.solved["field text"] += len(moves)
            self.unresolved += [{"rule": "text_overlap", **u} for u in unplaced]
        if self.rules & {"text_overlap", "wire_through_text"}:
            self.free_text()
        if "symbol_overlap" in self.rules:
            self.overlapping_symbols()
        return self

    def _opt(self, rule: str, name: str, default):
        return self.options.get(rule, {}).get(name, default)

    # ---------- editing the in-memory copy ----------

    def _replace(self, old: list[Wire], segments: list[tuple[Pt, Pt]]) -> None:
        gone = {w.uuid for w in old}
        for w in old:
            if w.uuid.startswith("new-"):
                self.added = [a for a in self.added if a.uuid != w.uuid]
            else:
                self.deleted.append(w.uuid)
        self.sheet.wires = [w for w in self.sheet.wires if w.uuid not in gone]
        self.space.remove(gone, "wire")
        for a, b in segments:
            self._new += 1
            w = Wire(a, b, f"new-{self._new}")
            self.sheet.wires.append(w)
            self.added.append(w)
            self.space.add(Item("wire", w.uuid, w.line))
        # like Konnect, drop the dots the old wires leave with fewer than three wires
        for w in old:
            for j in [j for j in self.sheet.junctions if same(j, w.a) or same(j, w.b)]:
                if arms(self.sheet, j) < 3:
                    self.sheet.junctions.remove(j)
                    self.space.remove([f"junction {j[0]:g},{j[1]:g}"], "point")

    # ---------- solvers ----------

    def overlaps(self) -> None:
        lines = defaultdict(list)
        for w in self.sheet.wires:
            if w.bus or w.horizontal == w.vertical:
                continue                        # buses, diagonals and zero-length wires
            lines[("h", round(w.a[1], 4)) if w.horizontal else ("v", round(w.a[0], 4))].append(w)
        for (axis, off), ws in lines.items():
            ax = 0 if axis == "h" else 1
            span = lambda w: (min(w.a[ax], w.b[ax]), max(w.a[ax], w.b[ax]))
            groups, cluster, hi = [], [], None
            for w in sorted(ws, key=span):
                lo, top = span(w)
                if cluster and lo < hi - TOL:
                    cluster.append(w)
                    hi = max(hi, top)
                else:
                    groups.append(cluster)
                    cluster, hi = [w], top
            groups.append(cluster)
            for g in (g for g in groups if len(g) > 1):
                cuts = sorted({round(v, 4) for w in g for v in span(w)})
                pt = (lambda v: (v, off)) if axis == "h" else (lambda v: (off, v))
                self._replace(g, [(pt(a), pt(b)) for a, b in zip(cuts, cuts[1:]) if b - a > TOL])
                self.solved["wire_overlap"] += 1

    def _text_trouble(self, t: Text, geom) -> bool:
        """Whether text t drawn as geom is flagged by wire_through_text or text_overlap."""
        wire_area = geom.buffer(self._opt("wire_through_text", "clearance", 0.2) - SHRINK, join_style="mitre")
        if t.kind in LABELS:
            wire_area = wire_area.difference(Point(t.at).buffer(self._opt("wire_through_text", "clearance", 0.2)
                                                                + 2 * SHRINK))
        if self.space.query(wire_area, ("wire",)):
            return True
        area = geom.buffer(self._opt("text_overlap", "clearance", 0.0) / 2 - SHRINK, join_style="mitre")
        return any(not (i.kind == "text" and i.id == t.label and i.geom.equals(t.box))
                   for i in self.space.query(area, ("text", "line", "sheet")))

    def labels(self) -> None:
        for t in [t for t in self.sheet.texts if t.kind in LABELS]:
            if not self._text_trouble(t, t.box):
                continue
            if any(u is not t and u.kind in LABELS and u.text == t.text and same(u.at, t.at) for u in self.sheet.texts):
                # batch_rotate_labels picks a label by name and position only, so it can't tell these apart
                self.unresolved.append({"rule": "wire_through_text", "at": [round(v, 3) for v in t.at],
                                        "items": [t.label], "reason": f"two labels named {t.text} sit on the same "
                                        f"point; turn the {t.kind} by hand, or remove the duplicate"})
                continue
            for angle in sorted((0.0, 90.0, 180.0, 270.0), key=lambda a: (a - t.angle) % 360 != 180):
                if angle == t.angle % 360:
                    continue
                geom = label_box(t.kind, t.text, t.at, angle, t.font)
                if not self._text_trouble(t, geom):
                    self.space.move("text", t.label, t.box, geom)
                    t.box, t.angle = geom, angle
                    self.turned.append((t, angle))
                    self.solved["label turned"] += 1
                    break

    def free_text(self) -> None:
        for t in [t for t in self.sheet.texts if t.kind == "text"]:
            if not self._text_trouble(t, t.box):
                continue
            x0, y0, x1, y1 = t.box.bounds
            c = t.box.centroid
            found = self.space.search(rect(x1 - x0, y1 - y0), (c.x, c.y), radius=30.0, step=1.27,
                                      exclude=(t.label,), kinds=("wire", "line", "body", "text", "sheet", "pin"))
            entry = {"rule": "wire_through_text", "at": [round(t.at[0], 3), round(t.at[1], 3)], "items": [t.text[:60]],
                     "reason": "free text over a wire or other text: Konnect cannot move text, so move it in KiCad"}
            if found:
                entry["suggestion"] = {"move_by": [round(found[0].x - c.x, 3), round(found[0].y - c.y, 3)]}
            self.unresolved.append(entry)

    def reroutes(self) -> None:
        texts = {t.label: t for t in self.sheet.texts}
        targets: dict[str, str] = {}            # wire uuid -> rule
        if "wire_through_symbol" in self.rules:
            for f in BY_ID["wire_through_symbol"].check(self.sheet, self.options.get("wire_through_symbol", {})):
                targets.setdefault(f.items[1], f.rule)
        if "wire_through_text" in self.rules:
            for f in BY_ID["wire_through_text"].check(self.sheet, self.options.get("wire_through_text", {})):
                t = texts.get(f.message.removeprefix("wire runs through "))
                if t is None or t.kind == "text" or fields.symbol_of(self.sheet, t) is not None:
                    continue                    # free text and symbol fields move instead
                w = next(w for w in self.sheet.wires if w.uuid == f.items[1])
                if t.kind in LABELS and w.line.distance(Point(t.at)) < TOL:
                    continue                    # a label's own wire: turning the label was the fix
                targets.setdefault(f.items[1], f.rule)
        if "wire_over_pin" in self.rules:
            for f in BY_ID["wire_over_pin"].check(self.sheet, {}):
                if "middle of a wire" in f.message:
                    self.unresolved.append({
                        "rule": f.rule, "at": [round(v, 3) for v in f.at], "items": f.items,
                        "reason": f"{f.message}: if the pin belongs on that net, add a junction at the pin "
                                  f"tip; if not, route the wire away (route_schematic_wire)"})
                else:
                    targets.setdefault(f.items[1], f.rule)
        for uuid, rule in targets.items():
            wires = [w for w in self.sheet.wires if not w.bus]
            start = next((w for w in wires if w.uuid == uuid), None)
            if start is None:
                continue                        # already routed again with an earlier path
            ends = wire_ends(wires)
            path, a, b = chain(ends, stops(self.sheet, wires), start)
            if self._loose(a, ends, wires) or self._loose(b, ends, wires):
                # a dead end connects nothing there: removing the path leaves every net as it was
                self._replace(path, [])
                self.solved[f"{rule} (dangling wire removed)"] += 1
                continue
            route = self.space.route(a, b, self._outward(a), self._outward(b), exclude=[w.uuid for w in path])
            if route is None:
                self.unresolved.append({"rule": rule, "at": [round(v, 3) for v in a], "items": [w.uuid for w in path],
                                        "reason": f"no clear route from {a} to {b} within 15 mm: move the parts "
                                                  f"apart or replace the wire with net labels"})
                continue
            self._replace(path, list(zip(route, route[1:])))
            self.solved[rule] += 1

    def _loose(self, p: Pt, ends, wires: list[Wire]) -> bool:
        """Whether p is a wire end that touches nothing else: no other wire, pin tip, label,
        junction, no-connect flag or sheet pin."""
        if len(ends.get(key(p), [])) != 1 or any(passes_through(w, p) for w in wires):
            return False
        marks = [q.tip for q in self.sheet.pins] + [t.at for t in self.sheet.labels]
        marks += self.sheet.junctions + self.sheet.no_connects
        return not any(same(p, m) for m in marks)

    def _outward(self, p: Pt) -> Pt | None:
        pin = next((q for q in self.sheet.pins if same(q.tip, p)), None)
        return pin.outward if pin is not None else None

    def dots(self) -> None:
        for f in BY_ID["missing_junction"].check(self.sheet, {}):
            self.sheet.junctions.append(f.at)
            self.junctions.append(f.at)
            self.space.add(Item("point", f"junction {f.at[0]:g},{f.at[1]:g}", Point(f.at)))
            self.solved["missing_junction"] += 1

    def overlapping_symbols(self) -> None:
        syms = {s.ref: s for s in self.sheet.symbols}
        for f in BY_ID["symbol_overlap"].check(self.sheet, {}):
            name = next((n for n in reversed(f.items) if n in syms), None)
            entry = {"rule": f.rule, "at": [round(v, 3) for v in f.at], "items": f.items,
                     "reason": f"{f.message}: moving a symbol leaves its wires behind, so this is not solved"}
            if name is not None:
                s = syms[name]
                extent = unary_union([g for g in (s.body, s.strokes) if g is not None and not g.is_empty]
                                     + [t.box for t in self.sheet.texts if t.owner == s.ref])
                found = self.space.search(lambda x, y, v: translate(extent, x - s.at[0], y - s.at[1]), s.at,
                                          radius=30.0, step=1.27, clearance=1.27, exclude=(s.ref,),
                                          kinds=("wire", "line", "body", "text", "sheet", "pin"))
                if found:
                    entry["suggestion"] = {"move": s.ref, "to": [found[0].x, found[0].y],
                                           "then": "route its wires again (route_schematic_wire)"}
            self.unresolved.append(entry)

    # ---------- result ----------

    def steps(self, schematic: str) -> list[dict]:
        out = []
        if self.added:
            out.append({"konnect": "batch_add_wire", "args": {"schematic": schematic, "wires": [
                {"x1": w.a[0], "y1": w.a[1], "x2": w.b[0], "y2": w.b[1]} for w in self.added]}})
        if self.deleted:
            out.append({"konnect": "batch_delete_schematic_wire", "args": {"schematic": schematic,
                                                                           "uuids": self.deleted}})
        if self.junctions:
            out.append({"konnect": "batch_add_junction", "args": {"schematic": schematic, "positions": [
                {"x": round(p[0], 4), "y": round(p[1], 4)} for p in self.junctions]}})
        if self.turned:
            out.append({"konnect": "batch_rotate_labels", "args": {"schematic": schematic, "labels": [
                {"net": t.text, "x": t.at[0], "y": t.at[1], "rotation": angle} for t, angle in self.turned]}})
        edits = {}
        for m in self.moves:
            e = edits.setdefault((m.text.owner, m.text.unit), {"schematic": schematic, "reference": m.text.owner,
                                                               "unit": m.text.unit, "field_placements": {}})
            e["field_placements"][m.text.name] = {"x": m.at[0], "y": m.at[1], "rotation": m.angle}
        out += [{"konnect": "edit_schematic_component", "args": e} for e in edits.values()]
        return out
