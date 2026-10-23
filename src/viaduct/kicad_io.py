"""Everything that talks to KiCad: reading the live board into a BoardModel
and writing plans back as one commit (one undo step), plus the board setup
KiCad exposes and Konnect does not cover (live netclass edits, text variables,
origins, graphics defaults).

All public values are millimetres; KiCad's API uses nanometres.
"""

from __future__ import annotations

import re

from google.protobuf.empty_pb2 import Empty
from kipy import KiCad
from kipy.board_types import (ArcTrack, BoardArc, BoardCircle, BoardPolygon, BoardRectangle,
                              BoardSegment, Track, Via, Zone)
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.project import Project
from kipy.project_types import TextVariables
from kipy.proto.board import board_commands_pb2 as bc, board_pb2, board_types_pb2 as bt
from kipy.proto.common.commands import project_commands_pb2 as pc
from kipy.proto.common.types import base_types_pb2 as base, project_settings_pb2 as ps
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


# ---------- board setup ----------

# netclass board settings that are plain distances, as named in the API
NETCLASS_DISTANCES = ("clearance", "track_width", "diff_pair_track_width", "diff_pair_gap", "diff_pair_via_gap")
# via sizes live in padstacks: (field, stack, part)
NETCLASS_VIAS = {"via_diameter": ("via_stack", "size"), "via_drill": ("via_stack", "drill"),
                 "microvia_diameter": ("microvia_stack", "size"), "microvia_drill": ("microvia_stack", "drill")}
NETCLASS_FIELDS = NETCLASS_DISTANCES + tuple(NETCLASS_VIAS) + ("color",)


def _hex(c: base.Color) -> str:
    return "#" + "".join(f"{round(v * 255):02x}" for v in (c.r, c.g, c.b, c.a))


def _color(text: str) -> base.Color:
    h = text.lstrip("#")
    if len(h) not in (6, 8) or any(ch not in "0123456789abcdefABCDEF" for ch in h):
        raise ValueError(f"color must be #RRGGBB or #RRGGBBAA, not {text!r}")
    v = [int(h[i:i + 2], 16) / 255 for i in range(0, len(h), 2)] + [1.0]
    return base.Color(r=v[0], g=v[1], b=v[2], a=v[3])


def netclass_dict(nc: ps.NetClass) -> dict:
    """One netclass in mm; a field that is None is not set on the class and comes from Default."""
    b = nc.board
    out = {"name": nc.name, "priority": nc.priority if nc.HasField("priority") else None}
    for f in NETCLASS_DISTANCES:
        out[f] = mm(getattr(b, f).value_nm) if b.HasField(f) else None
    for f, (stack, part) in NETCLASS_VIAS.items():
        s = getattr(b, stack)
        if part == "size":
            out[f] = mm(s.copper_layers[0].size.x_nm) if b.HasField(stack) and s.copper_layers else None
        else:
            out[f] = mm(s.drill.diameter.x_nm) if b.HasField(stack) and s.HasField("drill") else None
    out["color"] = _hex(b.color) if b.HasField("color") else None
    if nc.type == ps.NetClassType.NCT_IMPLICIT:
        out["constituents"] = list(nc.constituents)
    return out


def patch_netclass(nc: ps.NetClass, values: dict) -> ps.NetClass:
    """A copy of `nc` with `values` (mm) set; None leaves a field as it is."""
    out = ps.NetClass()
    out.CopyFrom(nc)
    b = out.board
    unknown = set(values) - set(NETCLASS_FIELDS) - {"priority"}
    if unknown:
        raise ValueError(f"unknown netclass fields: {sorted(unknown)}")
    for f, v in values.items():
        if v is None:
            continue
        if f == "priority":
            out.priority = int(v)
        elif f == "color":
            b.color.CopyFrom(_color(v))
        else:
            if v <= 0:
                raise ValueError(f"{f} must be positive, got {v}")
            if f in NETCLASS_DISTANCES:
                getattr(b, f).value_nm = nm(v)
            else:
                stack, part = NETCLASS_VIAS[f]
                s = getattr(b, stack)
                if part == "drill":
                    s.drill.diameter.x_nm = s.drill.diameter.y_nm = nm(v)
                else:
                    if not s.copper_layers:
                        s.copper_layers.add(layer=bt.BoardLayer.BL_F_Cu, shape=bt.PadStackShape.PSS_CIRCLE)
                    s.copper_layers[0].size.x_nm = s.copper_layers[0].size.y_nm = nm(v)
    d = netclass_dict(out)
    for kind in ("via", "microvia"):
        dia, drill = d[f"{kind}_diameter"], d[f"{kind}_drill"]
        if dia is not None and drill is not None and drill >= dia:
            raise ValueError(f"{kind} drill {drill} must be smaller than its diameter {dia}")
    return out


def _explicit_netclasses(board) -> list[ps.NetClass]:
    reply = board.client.send(pc.GetNetClasses(), pc.NetClassesResponse)
    return [nc for nc in reply.net_classes if nc.type != ps.NetClassType.NCT_IMPLICIT]


def net_classes(board) -> list[dict]:
    return [netclass_dict(nc) for nc in _explicit_netclasses(board)]


def set_net_class(board, name: str, values: dict) -> dict:
    """Create or edit one netclass; fields not named keep their value.

    Always merge mode: in KiCad 10.0.7 a replace-mode SetNetClasses sent after a net's effective
    class was looked up (read_model does that) leaves pcbnew busy forever. Merge sends only this
    class, and only the fields it carries change, so it cannot unset a field.
    """
    current = next((nc for nc in _explicit_netclasses(board) if nc.name == name), None)
    before = netclass_dict(current) if current else None
    nc = patch_netclass(current or ps.NetClass(name=name, type=ps.NetClassType.NCT_EXPLICIT), values)
    cmd = pc.SetNetClasses(merge_mode=base.MapMergeMode.MMM_MERGE)
    cmd.net_classes.append(nc)
    board.client.send(cmd, Empty)
    after = next((c for c in net_classes(board) if c["name"] == name), None)
    expected = netclass_dict(nc)
    return {"created": before is None, "before": before, "after": after,
            "verified": after is not None and all(after[k] == expected[k] for k in NETCLASS_FIELDS),
            # KiCad caches each net's effective class and rebuilds it on save only
            "note": "nets keep their old effective rules until the board is saved: call save_board "
                    "before check_plan relies on the new values"}


def _project(board) -> Project:
    # Board.get_project() hands over the board's own DocumentSpecifier and Project() retypes it
    # in place, after which every board call fails with "document is not open"
    doc = base.DocumentSpecifier()
    doc.CopyFrom(board.document)
    return Project(board.client, doc)


def text_variables(board) -> dict:
    return dict(_project(board).get_text_variables().variables)


def set_text_variables(board, values: dict, remove=()) -> dict:
    project = _project(board)
    if remove:
        merged = {k: v for k, v in project.get_text_variables().variables.items() if k not in remove}
        merged.update(values)
        tv, mode = TextVariables(), base.MapMergeMode.MMM_REPLACE
        tv.variables = merged
    else:
        tv, mode = TextVariables(), base.MapMergeMode.MMM_MERGE
        tv.variables = values
    project.set_text_variables(tv, mode)
    return text_variables(board)


ORIGINS = {"grid": bc.BoardOriginType.BOT_GRID, "drill": bc.BoardOriginType.BOT_DRILL}


def origins(board) -> dict:
    out = {}
    for name, kind in ORIGINS.items():
        v = board.get_origin(kind)
        out[name] = [mm(v.x), mm(v.y)]
    return out


def set_origin(board, kind: str, x: float, y: float) -> dict:
    if kind not in ORIGINS:
        raise ValueError(f"origin must be one of {sorted(ORIGINS)}")
    board.set_origin(ORIGINS[kind], _vec(x, y))
    return origins(board)


def graphics_defaults(board) -> dict:
    out = {}
    for cls, g in board.get_graphics_defaults().items():
        t = g.proto.text
        out[board_pb2.BoardLayerClass.Name(cls)[4:].lower()] = {
            "line_width": mm(g.proto.line_thickness.value_nm),
            "text_size": [mm(t.size.x_nm), mm(t.size.y_nm)], "text_thickness": mm(t.stroke_width.value_nm),
            "italic": t.italic, "keep_upright": t.keep_upright}
    return out
