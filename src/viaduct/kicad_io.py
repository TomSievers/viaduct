"""Everything that talks to KiCad: reading the live board into a BoardModel
and writing plans back as one commit (one undo step).

All public values are millimetres; KiCad's API uses nanometres.
"""

from __future__ import annotations

import re

from kipy import KiCad
from kipy.board_types import (ArcTrack, BoardArc, BoardCircle, BoardPolygon, BoardRectangle,
                              BoardSegment, Track, Via, Zone)
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.types import base_types_pb2 as base
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union

from viaduct import geometry
from viaduct.model import BoardModel, Copper, Hole
from viaduct.plan import Plan

NM = 1e6


def mm(nm: int | float) -> float:
    return round(nm / NM, 6)


def nm(mm_: float) -> int:
    return int(round(mm_ * NM))


# ---------- layers ----------

def layer_name(layer: int) -> str:
    """BL_In1_Cu -> In1.Cu"""
    return bt.BoardLayer.Name(layer)[3:].replace("_", ".", 1)


def layer_id(name: str) -> int:
    return bt.BoardLayer.Value("BL_" + name.replace(".", "_", 1))


def _copper_order(name: str) -> tuple[int, int]:
    if name == "F.Cu":
        return (0, 0)
    if name == "B.Cu":
        return (2, 0)
    m = re.match(r"In(\d+)\.Cu", name)
    return (1, int(m.group(1)) if m else 0)


def is_copper(layer: int) -> bool:
    return bt.BoardLayer.Name(layer).endswith("_Cu")


# ---------- connection ----------

class Session:
    """Lazily connected KiCad session; reconnects after KiCad restarts.

    `resolve` returns the socket to use right now (None = KiCad's default), so the
    session follows a hidden editor when one is open and the user's KiCad otherwise.
    """

    def __init__(self, resolve):
        self.resolve = resolve
        self._kicad: KiCad | None = None
        self._socket: str | None = None

    @property
    def target(self) -> str:
        return self.resolve() or "default"

    def board(self):
        socket = self.resolve()
        if socket != self._socket:
            self._kicad, self._socket = None, socket
        try:
            if self._kicad is None:
                self._kicad = KiCad(socket_path=socket, client_name="viaduct", timeout_ms=15000)
            return self._kicad.get_board()
        except Exception as e:
            self._kicad = None
            if "GetOpenDocuments" in str(e):
                raise RuntimeError("KiCad is running but no board is open in the PCB editor; "
                                   "open the .kicad_pcb in KiCad's PCB editor and retry") from e
            raise

    def version(self) -> str:
        self.board()
        return self._kicad.get_version().full_version


# ---------- reading ----------

def _poly(pwh: PolygonWithHoles):
    def ring(pl: PolyLine) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for node in pl.nodes:
            if node.has_point:
                pts.append((mm(node.point.x), mm(node.point.y)))
            elif node.has_arc:
                a = node.arc
                arc = geometry.arc_points((mm(a.start.x), mm(a.start.y)), (mm(a.mid.x), mm(a.mid.y)),
                                          (mm(a.end.x), mm(a.end.y)))
                pts.extend(arc if not pts or pts[-1] != arc[0] else arc[1:])
        return pts
    return geometry.polygon(ring(pwh.outline), [ring(h) for h in pwh.holes])


def _outline(board):
    lines, polys = [], []
    for s in board.get_shapes():
        if layer_name(s.layer) != "Edge.Cuts":
            continue
        if isinstance(s, BoardSegment):
            lines.append(LineString([(mm(s.start.x), mm(s.start.y)), (mm(s.end.x), mm(s.end.y))]))
        elif isinstance(s, BoardArc):
            lines.append(LineString(geometry.arc_points((mm(s.start.x), mm(s.start.y)),
                                                        (mm(s.mid.x), mm(s.mid.y)),
                                                        (mm(s.end.x), mm(s.end.y)))))
        elif isinstance(s, BoardRectangle):
            x1, y1, x2, y2 = mm(s.top_left.x), mm(s.top_left.y), mm(s.bottom_right.x), mm(s.bottom_right.y)
            polys.append(Polygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)]))
        elif isinstance(s, BoardCircle):
            c = (mm(s.center.x), mm(s.center.y))
            r = ((mm(s.radius_point.x) - c[0]) ** 2 + (mm(s.radius_point.y) - c[1]) ** 2) ** 0.5
            polys.append(geometry.via(c[0], c[1], 2 * r))
        elif isinstance(s, BoardPolygon):
            polys.extend(_poly(p) for p in s.polygons)
    polys.extend(polygonize(unary_union(lines)) if lines else [])
    if not polys:
        return None
    return max(polys, key=lambda p: p.area)


def read_model(board, with_zones: bool = True) -> BoardModel:
    copper = sorted((layer_name(l) for l in board.get_enabled_layers() if is_copper(l)), key=_copper_order)
    nets = board.get_nets()
    net_names = {n.name for n in nets if n.name}
    clearance, default = {}, 0.2
    try:
        for name, nc in board.get_netclass_for_nets(nets).items():
            if nc.clearance is not None:
                clearance[name] = mm(nc.clearance)
                if nc.name == "Default":
                    default = mm(nc.clearance)
    except Exception:
        pass

    items: list[Copper] = []
    holes: list[Hole] = []

    # pads: exact copper outline per layer, with "REF.NUM" names
    refs: dict[str, str] = {}
    for fp in board.get_footprints():
        try:
            ref = fp.reference_field.text.value
            for p in fp.definition.pads:
                refs[p.id.value] = f"{ref}.{p.number}"
        except Exception:
            continue
    pads = board.get_pads()
    pad_geom: dict[str, dict[str, object]] = {p.id.value: {} for p in pads}
    presence = board.check_padstack_presence_on_layers(pads, [layer_id(L) for L in copper])
    for L in copper:
        lid = layer_id(L)
        on = [p for p in pads if presence.get(p, {}).get(lid, False)]
        if not on:
            continue
        polys = board.get_pad_shapes_as_polygons(on, lid)
        if len(polys) != len(on):          # the batch call drops missing shapes: go one by one
            polys = [board.get_pad_shapes_as_polygons(p, lid) for p in on]
        for p, poly in zip(on, polys):
            if poly is not None:
                pad_geom[p.id.value][L] = _poly(poly)
    for p in pads:
        drill = mm(p.padstack.drill.diameter.x) if p.padstack.drill.diameter.x else None
        at = (mm(p.position.x), mm(p.position.y))
        items.append(Copper(p.id.value, "pad", p.net.name, pad_geom[p.id.value],
                            ref=refs.get(p.id.value), drill=drill, at=at))
        if drill:
            holes.append(Hole(at[0], at[1], drill / 2, refs.get(p.id.value, p.id.value)))

    for t in board.get_tracks():
        L = layer_name(t.layer)
        if isinstance(t, ArcTrack):
            pts = geometry.arc_points((mm(t.start.x), mm(t.start.y)), (mm(t.mid.x), mm(t.mid.y)),
                                      (mm(t.end.x), mm(t.end.y)))
            g = LineString(pts).buffer(mm(t.width) / 2)
            kind = "arc"
        else:
            g = geometry.track(mm(t.start.x), mm(t.start.y), mm(t.end.x), mm(t.end.y), mm(t.width))
            kind = "track"
        items.append(Copper(t.id.value, kind, t.net.name, {L: g}, width=mm(t.width)))

    for v in board.get_vias():
        x, y, d = mm(v.position.x), mm(v.position.y), mm(v.diameter)
        drill = mm(v.drill_diameter)
        start, end = v.padstack.drill.start_layer, v.padstack.drill.end_layer
        span = copper
        if start and end and layer_name(start) in copper and layer_name(end) in copper:
            i0, i1 = sorted((copper.index(layer_name(start)), copper.index(layer_name(end))))
            span = copper[i0:i1 + 1]
        items.append(Copper(v.id.value, "via", v.net.name, {L: geometry.via(x, y, d) for L in span},
                            drill=drill, at=(x, y)))
        holes.append(Hole(x, y, drill / 2, v.id.value))

    if with_zones:
        for z in board.get_zones():
            if z.type != bt.ZoneType.ZT_COPPER or z.net is None:
                continue
            geom = {}
            for lid, polys in z.filled_polygons.items():
                L = layer_name(lid)
                if L in copper and polys:
                    geom[L] = unary_union([_poly(p) for p in polys])
            if geom:
                items.append(Copper(z.id.value, "zone", z.net.name, geom, ref=z.name))

    return BoardModel(copper, items, holes, _outline(board), clearance, default, net_names)


def list_zones(board) -> list[dict]:
    out = []
    for z in board.get_zones():
        out.append({"id": z.id.value, "name": z.name, "net": z.net.name if z.net else "",
                    "layers": [layer_name(l) for l in z.layers], "priority": z.priority,
                    "type": bt.ZoneType.Name(z.type), "filled": z.filled,
                    "clearance": mm(z.clearance) if z.clearance is not None else None,
                    "min_thickness": mm(z.min_thickness) if z.min_thickness is not None else None,
                    "island_mode": bt.IslandRemovalMode.Name(z.island_mode) if z.island_mode is not None else None})
    return out


# ---------- writing ----------

def _vec(x: float, y: float) -> Vector2:
    return Vector2.from_xy(nm(x), nm(y))


def build_items(board, plan: Plan) -> list:
    nets = {n.name: n for n in board.get_nets()}
    out = []
    for t in plan.tracks:
        tr = Track()
        tr.start, tr.end = _vec(t.x1, t.y1), _vec(t.x2, t.y2)
        tr.width = nm(t.width)
        tr.layer = layer_id(t.layer)
        tr.net = nets[t.net]
        out.append(tr)
    for v in plan.vias:
        via = Via()
        via.position = _vec(v.x, v.y)
        via.net = nets[v.net]
        ps = via._proto.pad_stack
        layer = ps.copper_layers.add()
        layer.layer = bt.BoardLayer.BL_F_Cu
        layer.shape = bt.PadStackShape.PSS_CIRCLE
        layer.size.x_nm = layer.size.y_nm = nm(v.diameter)
        ps.drill.diameter.x_nm = ps.drill.diameter.y_nm = nm(v.drill)
        ps.drill.start_layer = bt.BoardLayer.BL_F_Cu
        ps.drill.end_layer = bt.BoardLayer.BL_B_Cu
        out.append(via)
    for z in plan.zones:
        zone = Zone()
        zone.type = bt.ZoneType.ZT_COPPER
        zone.layers = [layer_id(z.layer)]
        zone.net = nets[z.net]
        zone.name = z.name
        zone.priority = z.priority
        ring = PolyLine()
        for x, y in z.outline:
            ring.append(PolyLineNode.from_xy(nm(x), nm(y)))
        ring.closed = True
        pwh = PolygonWithHoles()
        pwh.outline = ring
        zone.outline = pwh
        zone.island_mode = bt.IslandRemovalMode.IRM_ALWAYS
        if z.clearance is not None:
            zone.clearance = nm(z.clearance)
        if z.min_thickness is not None:
            zone.min_thickness = nm(z.min_thickness)
        out.append(zone)
    return out


def apply(board, plan: Plan, message: str) -> dict:
    """Delete and create in one commit, then read back what KiCad really holds."""
    items = build_items(board, plan)
    commit = board.begin_commit()
    try:
        if plan.delete:
            board.remove_items_by_id([base.KIID(value=u) for u in plan.delete])
        created = board.create_items(items) if items else []
        board.push_commit(commit, message)
    except Exception:
        board.drop_commit(commit)
        raise
    created_ids = [c.id.value for c in created]
    live = {t.id.value for t in board.get_tracks()} | {v.id.value for v in board.get_vias()} | \
           {z.id.value for z in board.get_zones()}
    return {
        "created": len(created_ids),
        "expected_created": len(items),
        "created_missing": [u for u in created_ids if u not in live],
        "deleted": len(plan.delete),
        "deleted_still_present": [u for u in plan.delete if u in live],
        "created_ids": created_ids,
    }


def set_copper_layers(board, count: int) -> dict:
    before = board.get_copper_layer_count()
    keep = [l for l in board.get_enabled_layers() if not is_copper(l)]
    board.set_enabled_layers(count, keep)
    return {"before": before, "after": board.get_copper_layer_count()}


def update_zone(board, zone_id: str, **props) -> dict:
    zone = next((z for z in board.get_zones() if z.id.value == zone_id), None)
    if zone is None:
        raise ValueError(f"no zone with id {zone_id}")
    if props.get("name") is not None:
        zone.name = props["name"]
    if props.get("priority") is not None:
        zone.priority = int(props["priority"])
    if props.get("clearance") is not None:
        zone.clearance = nm(props["clearance"])
    if props.get("min_thickness") is not None:
        zone.min_thickness = nm(props["min_thickness"])
    if props.get("island_mode") is not None:
        zone.island_mode = bt.IslandRemovalMode.Value("IRM_" + props["island_mode"].upper())
    board.update_items(zone)
    return next(z for z in list_zones(board) if z["id"] == zone_id)
