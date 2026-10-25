"""Everything that talks to KiCad: reading the live board into a BoardModel
and writing plans back as one commit (one undo step), plus the board setup
KiCad exposes and Konnect does not cover (live netclass edits, text variables,
origins, graphics defaults).

All public values are millimetres; KiCad's API uses nanometres.
"""

from __future__ import annotations

import hashlib
import re

from google.protobuf.empty_pb2 import Empty
from kipy import KiCad
from kipy.board_types import (ArcTrack, BoardArc, BoardBezier, BoardCircle, BoardPolygon, BoardRectangle,
                              BoardSegment, BoardText, BoardTextBox, Field, Track, Via, Zone)
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.project import Project
from kipy.project_types import TextVariables
from kipy.util import pack_any
from kipy.proto.board import board_commands_pb2 as bc, board_pb2, board_types_pb2 as bt
from kipy.proto.common.commands import project_commands_pb2 as pc
from kipy.proto.common.commands.editor_commands_pb2 import ItemStatusCode, UpdateItems, UpdateItemsResponse
from kipy.proto.common.types import base_types_pb2 as base, project_settings_pb2 as ps
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box as shapely_box
from shapely.ops import polygonize, unary_union

from viaduct import fanout, geometry, silk
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
        if t.locked:
            tr._proto.locked = base.LockedState.LS_LOCKED
        out.append(tr)
    for v in plan.vias:
        via = Via()
        via.position = _vec(v.x, v.y)
        via.net = nets[v.net]
        via.locked = v.locked
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


def check_moves(board, plan: Plan) -> list[dict]:
    """The plan's field and graphic moves, checked against the live board."""
    out = []
    for key, prepare in (("field_moves", prepare_fields), ("graphic_moves", prepare_graphics)):
        moves = getattr(plan, key)
        if moves:
            out += [{"rule": key[:-1], "detail": e} for e in prepare(board, moves)[1]]
    return out


def plan_token(board, model: BoardModel, plan: Plan) -> str:
    """The board_token of a plan: the copper fingerprint, plus every text and graphic the plan
    moves, so apply_plan refuses when one of them was changed since check_plan (a dx/dy would
    otherwise stack on someone else's move). Plans without moves keep the plain fingerprint."""
    if not plan.field_moves and not plan.graphic_moves:
        return model.fingerprint()
    h = hashlib.sha1(model.fingerprint().encode())
    graphics = _graphics(board) if plan.graphic_moves else {}
    for m in plan.graphic_moves:
        it = graphics.get(m.get("id"))
        h.update(it[1].proto.SerializeToString(deterministic=True) if it else b"missing")
    by_ref = _footprints_by_reference(board) if plan.field_moves else {}
    for m in plan.field_moves:
        fps = by_ref.get(m.get("reference"), [])
        name = str(m.get("text", "Reference"))
        hit = [p for r, p in (_footprint_texts(fps[0]) if len(fps) == 1 else [])
               if (r["name"] or "").lower() == name.lower() or r["id"] == name]
        h.update(hit[0].SerializeToString(deterministic=True) if hit else b"missing")
    return h.hexdigest()[:16]


def apply(board, plan: Plan, message: str) -> dict:
    """Delete, create and move in one commit, then read back what KiCad really holds."""
    items = build_items(board, plan)
    fields, field_errors = prepare_fields(board, plan.field_moves) if plan.field_moves else ([], [])
    graphics, graphic_errors = prepare_graphics(board, plan.graphic_moves) if plan.graphic_moves else ([], [])
    _refuse(field_errors + graphic_errors)
    before = graphics_boxes(board, graphics)
    commit = board.begin_commit()
    try:
        if plan.delete:
            board.remove_items_by_id([base.KIID(value=u) for u in plan.delete])
        created = board.create_items(items) if items else []
        _send(board, field_requests(fields) + [(None, [p for _, _, _, p, _, _ in graphics])])
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
        "field_moves": verify_fields(board, fields),
        "graphic_moves": verify_graphics(board, graphics, before),
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


# ---------- moving text and graphics ----------

def translate(msg, dx: int, dy: int) -> None:
    """Shift every point of a board item message by (dx, dy) nm, in place. Text sizes
    (attributes.size) are the only other Vector2 fields in graphics and stay as they are."""
    for f, v in msg.ListFields():
        if f.message_type is None or f.name == "size":
            continue
        repeated = f.is_repeated if hasattr(f, "is_repeated") else f.label == f.LABEL_REPEATED  # protobuf < 6
        for sub in (v if repeated else [v]):
            if f.message_type.full_name == "kiapi.common.types.Vector2":
                sub.x_nm += dx
                sub.y_nm += dy
            else:
                translate(sub, dx, dy)


def _box(b) -> list[float] | None:
    return None if b is None else [mm(b.pos.x), mm(b.pos.y), mm(b.pos.x + b.size.x), mm(b.pos.y + b.size.y)]


def _check_layer(board, name: str) -> int:
    lid = layer_id(name)
    if is_copper(lid):
        raise ValueError(f"{name} is copper: text and graphics on copper are copper, use check_plan/apply_plan")
    if lid not in board.get_enabled_layers():
        raise ValueError(f"{name} is not enabled on this board")
    return lid


def _send(board, requests: list[tuple[str | None, list]]) -> None:
    """Send UpdateItems requests; the caller holds the commit. Each request is
    (container, protos): KiCad takes one container per request, so the fields of different
    footprints go in separate requests. kipy's wrappers re-pack some items from their own cached
    geometry when .proto is read (polygons), so edits go on copies sent as they are."""
    for container, protos in requests:
        if not protos:
            continue
        cmd = UpdateItems()
        cmd.header.document.CopyFrom(board.document)
        if container:
            cmd.header.container.value = container
        cmd.items.extend(pack_any(p) for p in protos)
        reply = board.client.send(cmd, UpdateItemsResponse)
        failed = [r.status.error_message or ItemStatusCode.Name(r.status.code)
                  for r in reply.updated_items if r.status.code != ItemStatusCode.ISC_OK]
        if failed:
            raise RuntimeError(f"KiCad refused the update: {failed}")


def _update(board, requests: list[tuple[str | None, list]], message: str) -> None:
    """_send inside one commit (one undo step)."""
    commit = board.begin_commit()
    try:
        _send(board, requests)
        board.push_commit(commit, message)
    except Exception:
        board.drop_commit(commit)
        raise


def _copy(it):
    p = type(it.proto)()
    p.CopyFrom(it.proto)
    return p


def _locked(board_text_msg) -> bool:
    return board_text_msg.locked == base.LockedState.LS_LOCKED


def _refuse(errors: list[str]) -> None:
    """Batches are all or nothing: report every problem, write nothing."""
    if errors:
        raise ValueError("nothing was changed:\n" + "\n".join(errors))


def _position(move: dict, where: str, errors: list[str]) -> tuple[str | None, tuple[float, float] | None]:
    """("abs" | "rel" | None, (x, y)) from a move's x/y or dx/dy."""
    has_abs, has_rel = ("x" in move or "y" in move), ("dx" in move or "dy" in move)
    if has_abs and has_rel:
        errors.append(f"{where}: pass x and y, or dx and dy, not both")
    elif has_abs:
        if "x" not in move or "y" not in move:
            errors.append(f"{where}: pass both x and y")
        else:
            return "abs", (float(move["x"]), float(move["y"]))
    elif has_rel:
        return "rel", (float(move.get("dx", 0.0)), float(move.get("dy", 0.0)))
    return None, None


def _style(t, move: dict, where: str, errors: list[str]) -> None:
    """rotation, size and thickness of a text (kiapi Text) from a move."""
    if move.get("rotation") is not None:
        t.attributes.angle.value_degrees = float(move["rotation"])
    for key in ("size", "thickness"):
        v = move.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            errors.append(f"{where}: {key} must be a positive number")
        elif key == "size":
            t.attributes.size.x_nm = t.attributes.size.y_nm = nm(v)
        else:
            t.attributes.stroke_width.value_nm = nm(v)


def _graphics(board) -> dict[str, tuple[str, object]]:
    """Free board text, text boxes, shapes and dimensions (not footprint children) by id."""
    out = {}
    for t in board.get_text():
        out[t.id.value] = ("textbox" if isinstance(t, BoardTextBox) else "text", t)
    for s in board.get_shapes():
        out[s.id.value] = (type(s).__name__.removeprefix("Board").lower(), s)
    for d in board.get_dimensions():
        out[d.id.value] = ("dimension", d)
    return out


def _graphic_text(kind: str, p) -> str | None:
    if kind == "text":
        return p.text.text
    if kind == "textbox":
        return p.textbox.text
    if kind == "dimension":
        return p.text.text
    return None


def list_graphics(board, layer: str | None = None, kind: str | None = None, bbox: list[float] | None = None,
                  contains: str | None = None) -> list[dict]:
    items = [(i, k, it) for i, (k, it) in _graphics(board).items()
             if (kind is None or k == kind) and (layer is None or layer_name(it.proto.layer) == layer)]
    boxes = board.get_item_bounding_box([it for _, _, it in items]) if items else []
    rows = []
    for (i, k, it), b in zip(items, boxes):
        box, text = _box(b), _graphic_text(k, it.proto)
        if bbox and box and (box[2] < bbox[0] or box[0] > bbox[2] or box[3] < bbox[1] or box[1] > bbox[3]):
            continue
        if contains and (text is None or contains.lower() not in text.lower()):
            continue
        rows.append({"id": i, "kind": k, "layer": layer_name(it.proto.layer), "text": text, "bbox": box,
                     "locked": _locked(it.proto)})
    return rows


GRAPHIC_MOVE_KEYS = {"id", "dx", "dy", "x", "y", "rotation", "size", "layer"}


def prepare_graphics(board, moves: list[dict], include_locked: bool = False) -> tuple[list, list[str]]:
    """Check graphic moves against the board and build the edited copies.

    Returns (plan, errors); plan rows are (id, kind, item, edited proto, mode, xy). Nothing is
    sent: edit_graphics and apply_plan do that.
    """
    found = _graphics(board)
    errors, plan, seen = [], [], set()
    enabled = board.get_enabled_layers()
    for n, move in enumerate(moves):
        where = f"move {n} ({move.get('id')})"
        unknown = set(move) - GRAPHIC_MOVE_KEYS
        if unknown:
            errors.append(f"{where}: unknown keys {sorted(unknown)}")
            continue
        i = move.get("id")
        if i not in found:
            errors.append(f"{where}: no board text, shape or dimension with this id "
                          "(footprint fields move with move_footprint_texts)")
            continue
        if i in seen:
            errors.append(f"{where}: this id is already moved by an earlier move")
            continue
        seen.add(i)
        kind, it = found[i]
        p = _copy(it)
        if is_copper(p.layer):
            errors.append(f"{where}: on {layer_name(p.layer)}; copper text and shapes are copper, and moving "
                          "them here would skip the clearance checks")
            continue
        if _locked(p) and not include_locked:
            errors.append(f"{where}: locked; pass include_locked=true to move it anyway")
            continue
        mode, xy = _position(move, where, errors)
        if (mode == "abs" or any(move.get(k) is not None for k in ("rotation", "size"))) and kind != "text":
            errors.append(f"{where}: x/y, rotation and size apply to texts; a {kind} moves with dx/dy")
            continue
        if mode == "rel":
            translate(p, nm(xy[0]), nm(xy[1]))
        elif mode == "abs":
            p.text.position.x_nm, p.text.position.y_nm = nm(xy[0]), nm(xy[1])
        if kind == "text":
            _style(p.text, move, where, errors)
        if move.get("layer"):
            try:
                lid = layer_id(move["layer"])
            except ValueError:
                errors.append(f"{where}: unknown layer {move['layer']!r}")
                continue
            if is_copper(lid) or lid not in enabled:
                errors.append(f"{where}: {move['layer']} is copper or not enabled")
                continue
            p.layer = lid
        if mode is None and p == it.proto:
            errors.append(f"{where}: nothing to change")
            continue
        plan.append((i, kind, it, p, mode, xy))
    return plan, errors


def graphics_boxes(board, plan: list) -> list:
    return [_box(b) for b in board.get_item_bounding_box([it for _, _, it, *_ in plan])] if plan else []


def verify_graphics(board, plan: list, before: list) -> dict:
    """Read the moved items back and compare them with what was sent."""
    if not plan:
        return {"moved": 0, "verified": True, "failed": [], "items": []}
    now = _graphics(board)
    after = [_box(b) for b in board.get_item_bounding_box([now[i][1] for i, *_ in plan])]
    rows = []
    for (i, kind, _, p, mode, xy), b, a in zip(plan, before, after):
        live = now[i][1].proto
        ok = live.layer == p.layer
        if mode == "rel":
            ok &= b is not None and a is not None and all(abs(a[j] - b[j] - xy[j % 2]) < 1e-3 for j in range(4))
        if kind == "text":
            ok &= _text_row("text", None, live, live.text) == _text_row("text", None, p, p.text)
        rows.append({"id": i, "kind": kind, "layer": layer_name(live.layer), "before": b, "after": a, "verified": ok})
    return {"moved": len(rows), "verified": all(r["verified"] for r in rows),
            "failed": [r["id"] for r in rows if not r["verified"]], "items": rows}


def edit_graphics(board, moves: list[dict], include_locked: bool = False,
                  message: str = "Viaduct: move graphics") -> dict:
    """Move free board items, each its own way, in one commit.

    A move is {"id", "dx", "dy"} for any item, or {"id", "x", "y", "rotation", "size"} for a
    text (absolute), plus an optional "layer" (not copper). Every move is checked first; one
    bad move means nothing is written.
    """
    if not moves:
        raise ValueError("pass at least one move (list_board_graphics shows the ids)")
    plan, errors = prepare_graphics(board, moves, include_locked)
    _refuse(errors)
    before = graphics_boxes(board, plan)
    _update(board, [(None, [p for _, _, _, p, _, _ in plan])], message)
    return verify_graphics(board, plan, before)


def move_graphics(board, ids: list[str], dx: float, dy: float, layer: str | None = None,
                  include_locked: bool = False) -> dict:
    """Shift several free board items by the same offset."""
    if not ids:
        raise ValueError("pass the ids of the items to move (list_board_graphics shows them)")
    return edit_graphics(board, [{"id": i, "dx": dx, "dy": dy, **({"layer": layer} if layer else {})} for i in ids],
                         include_locked)


def set_board_text(board, text_id: str, x: float | None = None, y: float | None = None,
                   rotation: float | None = None, layer: str | None = None, size: float | None = None) -> dict:
    """Place one free board text at an absolute position and angle."""
    move = {"id": text_id, **{k: v for k, v in (("x", x), ("y", y), ("rotation", rotation), ("layer", layer),
                                                ("size", size)) if v is not None}}
    result = edit_graphics(board, [move], message="Viaduct: place board text")
    live = _graphics(board)[text_id][1].proto
    return {**_text_row("text", None, live, live.text), "verified": result["verified"]}


# footprint text

def _footprints_by_reference(board) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in board.get_footprints():
        out.setdefault(f.reference_field.text.value, []).append(f)
    return out


def _footprint(board, reference: str):
    fps = _footprints_by_reference(board).get(reference, [])
    if not fps:
        raise ValueError(f"no footprint with reference {reference!r}")
    if len(fps) > 1:
        raise ValueError(f"{len(fps)} footprints share reference {reference!r}; annotate the board first")
    return fps[0]


def _text_row(kind: str, name: str | None, board_text, text) -> dict:
    a = text.attributes
    return {"kind": kind, "name": name, "id": board_text.id.value, "text": text.text,
            "layer": layer_name(board_text.layer), "position": [mm(text.position.x_nm), mm(text.position.y_nm)],
            "rotation": round(a.angle.value_degrees, 6), "size": [mm(a.size.x_nm), mm(a.size.y_nm)],
            "thickness": mm(a.stroke_width.value_nm), "locked": _locked(board_text)}


def _field_row(p) -> dict:
    return {**_text_row("field", p.name, p.text, p.text.text), "visible": p.visible, "movable": True}


def _footprint_texts(fp) -> list[tuple[dict, object]]:
    """(row, proto) for every field and free text of a footprint, fields first."""
    out, seen = [], set()
    for item in fp.texts_and_fields:
        p = item.proto
        if isinstance(item, Field):
            row = _field_row(p)
        elif isinstance(item, BoardText):
            row = {**_text_row("text", None, p, p.text), "movable": False}
        else:
            continue
        if row["id"] not in seen:
            seen.add(row["id"])
            out.append((row, p))
    return sorted(out, key=lambda rp: rp[0]["kind"] != "field")


def footprint_texts(board, reference: str) -> dict:
    fp = _footprint(board, reference)
    return {"reference": reference, "footprint_id": fp.id.value, "footprint_layer": layer_name(fp.layer),
            "footprint_position": [mm(fp.position.x), mm(fp.position.y)],
            "footprint_rotation": round(fp.orientation.degrees, 6),
            "texts": [row for row, _ in _footprint_texts(fp)]}


FIELD_MOVE_KEYS = {"reference", "text", "dx", "dy", "x", "y", "rotation", "layer", "visible", "size", "thickness"}


def prepare_fields(board, moves: list[dict]) -> tuple[list, list[str]]:
    """Check footprint field moves against the board and build the edited copies.

    Returns (plan, errors); plan rows are (reference, footprint, row before, edited proto).
    Nothing is sent: edit_footprint_texts and apply_plan do that.

    A move is {"reference", "text" (field name or id, default Reference), "x"/"y" or "dx"/"dy",
    "rotation", "layer", "visible", "size", "thickness"}. Each field is sent on its own with its
    footprint as container, so KiCad edits it in place; sending the whole footprint back would
    make KiCad re-create the footprint and drop its (units ...) block on save (Konnect #941).
    Free texts inside a footprint are refused: KiCad 10.0.7 swaps them into the undo copy on
    every commit while its item-id cache keeps pointing at them, so later edits are silently
    lost.
    """
    by_ref = _footprints_by_reference(board)
    enabled = board.get_enabled_layers()
    errors, plan, seen = [], [], set()
    for n, move in enumerate(moves):
        ref, name = move.get("reference"), move.get("text", "Reference")
        where = f"move {n} ({ref} {name})"
        unknown = set(move) - FIELD_MOVE_KEYS
        if unknown:
            errors.append(f"{where}: unknown keys {sorted(unknown)}")
            continue
        fps = by_ref.get(ref, [])
        if len(fps) != 1:
            errors.append(f"{where}: " + ("no footprint with this reference" if not fps else
                                          f"{len(fps)} footprints share this reference; annotate the board first"))
            continue
        fp = fps[0]
        entries = _footprint_texts(fp)
        match = [(r, p) for r, p in entries if (r["name"] or "").lower() == str(name).lower()] or \
                [(r, p) for r, p in entries if r["id"] == name]
        if not match:
            errors.append(f"{where}: no such field; it has {[r['name'] or r['id'] for r, _ in entries]}")
            continue
        row, live = match[0]
        if row["kind"] != "field":
            errors.append(f"{where}: free text {row['text']!r} inside the footprint cannot be moved reliably "
                          "through KiCad's API (KiCad drops repeated edits of them); move a field instead")
            continue
        if row["id"] in seen:
            errors.append(f"{where}: this field is already moved by an earlier move")
            continue
        seen.add(row["id"])
        if row["locked"]:
            errors.append(f"{where}: locked")
            continue
        p = type(live)()
        p.CopyFrom(live)
        t = p.text.text
        mode, xy = _position(move, where, errors)
        if mode == "abs":
            t.position.x_nm, t.position.y_nm = nm(xy[0]), nm(xy[1])
        elif mode == "rel":
            t.position.x_nm += nm(xy[0])
            t.position.y_nm += nm(xy[1])
        _style(t, move, where, errors)
        if move.get("layer"):
            try:
                lid = layer_id(move["layer"])
            except ValueError:
                errors.append(f"{where}: unknown layer {move['layer']!r}")
                continue
            if is_copper(lid) or lid not in enabled:
                errors.append(f"{where}: {move['layer']} is copper or not enabled")
                continue
            p.text.layer = lid
        if move.get("visible") is not None:
            p.visible = bool(move["visible"])
        plan.append((ref, fp, row, p))
    return plan, errors


def field_requests(plan: list) -> list[tuple[str, list]]:
    """One UpdateItems request per footprint, the footprint as container."""
    requests: dict[str, list] = {}
    for _, fp, _, p in plan:
        requests.setdefault(fp.id.value, []).append(p)
    return list(requests.items())


def verify_fields(board, plan: list) -> dict:
    """Read the moved fields back; the footprints must be where they were."""
    if not plan:
        return {"moved": 0, "verified": True, "failed": [], "items": []}
    now = {f.id.value: f for f in board.get_footprints()}
    rows = []
    for ref, fp, row, p in plan:
        live_fp = now.get(fp.id.value)
        after = next((r for r, _ in _footprint_texts(live_fp) if r["id"] == row["id"]), None) if live_fp else None
        untouched = live_fp is not None and (
            live_fp.position.x == fp.position.x and live_fp.position.y == fp.position.y
            and live_fp.orientation.degrees == fp.orientation.degrees and live_fp.layer == fp.layer
            and len(live_fp.definition.pads) == len(fp.definition.pads))
        rows.append({"reference": ref, "field": row["name"], "before": row, "after": after,
                     "verified": after == _field_row(p), "footprint_untouched": untouched})
    return {"moved": len(rows), "verified": all(r["verified"] and r["footprint_untouched"] for r in rows),
            "failed": [f"{r['reference']} {r['field']}" for r in rows if not (r["verified"] and r["footprint_untouched"])],
            "items": rows}


def edit_footprint_texts(board, moves: list[dict], message: str = "Viaduct: move footprint text") -> dict:
    """Move or restyle footprint fields, each its own way, in one commit, without touching the
    footprints (see prepare_fields). Every move is checked first; one bad move means nothing
    is written."""
    if not moves:
        raise ValueError("pass at least one move")
    plan, errors = prepare_fields(board, moves)
    _refuse(errors)
    _update(board, field_requests(plan), message)
    return verify_fields(board, plan)


def move_footprint_text(board, reference: str, text: str = "Reference", x: float | None = None,
                        y: float | None = None, dx: float | None = None, dy: float | None = None,
                        rotation: float | None = None, layer: str | None = None, visible: bool | None = None,
                        size: float | None = None, thickness: float | None = None) -> dict:
    """Move or restyle one field of a placed footprint (a batch of one)."""
    move = {"reference": reference, "text": text, **{k: v for k, v in (
        ("x", x), ("y", y), ("dx", dx), ("dy", dy), ("rotation", rotation), ("layer", layer),
        ("visible", visible), ("size", size), ("thickness", thickness)) if v is not None}}
    result = edit_footprint_texts(board, [move], f"Viaduct: move {reference} {text}")
    item = result["items"][0]
    return {"reference": reference, "before": item["before"], "after": item["after"],
            "verified": item["verified"], "footprint_untouched": item["footprint_untouched"]}


# ---------- reference placement ----------

def _shape_geom(s):
    """A board or footprint graphic as the area it covers (mm), stroke included."""
    a = s.attributes
    width, filled = mm(a.stroke.width), a.fill.filled
    if isinstance(s, BoardSegment):
        return silk.stroke(LineString([(mm(s.start.x), mm(s.start.y)), (mm(s.end.x), mm(s.end.y))]), width)
    if isinstance(s, BoardArc):
        pts = geometry.arc_points((mm(s.start.x), mm(s.start.y)), (mm(s.mid.x), mm(s.mid.y)),
                                  (mm(s.end.x), mm(s.end.y)))
        return silk.stroke(LineString(pts), width)
    if isinstance(s, BoardBezier):
        p = [(mm(v.x), mm(v.y)) for v in (s.start, s.control1, s.control2, s.end)]
        pts = [tuple((1 - t) ** 3 * p[0][k] + 3 * (1 - t) ** 2 * t * p[1][k] + 3 * (1 - t) * t ** 2 * p[2][k]
                     + t ** 3 * p[3][k] for k in (0, 1)) for t in (i / 16 for i in range(17))]
        return silk.stroke(LineString(pts), width)
    if isinstance(s, BoardCircle):
        c = (mm(s.center.x), mm(s.center.y))
        area = Point(c).buffer(((mm(s.radius_point.x) - c[0]) ** 2 + (mm(s.radius_point.y) - c[1]) ** 2) ** 0.5)
    elif isinstance(s, BoardRectangle):
        x1, y1, x2, y2 = mm(s.top_left.x), mm(s.top_left.y), mm(s.bottom_right.x), mm(s.bottom_right.y)
        area = shapely_box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    elif isinstance(s, BoardPolygon):
        area = unary_union([_poly(p) for p in s.polygons])
    else:
        return None
    return silk.stroke(area if filled else area.boundary, width) if not area.is_empty else None


def _text_area(text_msg):
    """The area a kiapi Text covers, from its position, size, stroke and angle."""
    a = text_msg.attributes
    x, y = mm(text_msg.position.x_nm), mm(text_msg.position.y_nm)
    r = silk.text_box(text_msg.text, x, y, mm(a.size.y_nm), mm(a.stroke_width.value_nm), 0)
    return affinity.rotate(r, -a.angle.value_degrees, origin=(x, y))


def _silk_side(layer: int) -> str | None:
    return {"F.SilkS": "F", "B.SilkS": "B"}.get(layer_name(layer))


def silk_scene(board, include_hidden: bool = False) -> silk.SilkScene:
    """Everything reference placement must avoid, read from the live board.

    Labels are the Reference fields on a silkscreen layer (hidden ones only with
    include_hidden); other visible silk text stays where it is and is avoided.
    """
    scene = silk.SilkScene(_outline(board), [])
    for side in "FB":
        for d in (scene.pads, scene.silk, scene.bodies, scene.texts, scene.anchors):
            d[side] = []
    model = read_model(board, with_zones=False)
    for c in model.items:
        for L, side in (("F.Cu", "F"), ("B.Cu", "B")):
            if c.kind == "pad" and L in c.geom:
                scene.pads[side].append(c.geom[L])

    for fp in board.get_footprints():
        side = "B" if layer_name(fp.layer) == "B.Cu" else "F"
        courtyard, fab = [], []
        for s in fp.definition.shapes:
            L = layer_name(s.layer)
            g = _shape_geom(s)
            if g is None:
                continue
            if L.endswith(".CrtYd"):
                courtyard.append(g)
            elif L.endswith(".Fab"):
                fab.append(g)
            elif _silk_side(s.layer):
                scene.silk[_silk_side(s.layer)].append(g)
        pads = [c.geom[L] for c in model.items if c.kind == "pad" and c.ref and c.ref.split(".")[0]
                == fp.reference_field.text.value for L in c.geom]
        anchor = next((unary_union(g).bounds for g in (courtyard, pads, fab) if g), None)
        if anchor is None:
            x, y = mm(fp.position.x), mm(fp.position.y)
            anchor = (x - 0.5, y - 0.5, x + 0.5, y + 0.5)
        ref = fp.reference_field.text.value
        scene.anchors[side].append((ref, anchor))
        if fab:
            scene.bodies[side].append(unary_union(fab).envelope)

        for item in fp.texts_and_fields:
            p = item.proto
            if isinstance(item, Field):
                t, layer, visible = p.text.text, p.text.layer, p.visible
                tside = _silk_side(layer)
                if tside is None:
                    continue
                if p.name == "Reference":
                    if visible or include_hidden:
                        a = t.attributes
                        scene.labels.append(silk.Label(ref, tside, t.text, anchor, mm(a.size.y_nm),
                                                       mm(a.stroke_width.value_nm)))
                    continue
                if visible:
                    scene.texts[tside].append(_text_area(t))
            elif isinstance(item, BoardText) and _silk_side(p.layer):
                scene.texts[_silk_side(p.layer)].append(_text_area(p.text))

    for s in board.get_shapes():
        side = _silk_side(s.layer)
        g = _shape_geom(s) if side else None
        if g is not None:
            scene.silk[side].append(g)
    texts = [t for t in board.get_text() if _silk_side(t.proto.layer)]
    for t, b in zip(texts, board.get_item_bounding_box(texts) if texts else []):
        if b is not None:
            x1, y1, x2, y2 = _box(b)
            scene.texts[_silk_side(t.proto.layer)].append(Polygon([(x1, y1), (x2, y1), (x2, y2), (x1, y2)]))
    return scene


def place_references(board, rules: silk.Rules, references: list[str] | None = None,
                     include_hidden: bool = False, hide_unplaced: bool = True, apply: bool = False) -> dict:
    """Find a readable spot for each Reference (silk.place) and, with apply, move them all in
    one commit; references that fit nowhere are hidden when hide_unplaced."""
    scene = silk_scene(board, include_hidden)
    known = {lb.ref for lb in scene.labels}
    if references:
        missing = sorted(set(references) - known)
        if missing:
            raise ValueError(f"no Reference on a silkscreen layer for {missing}"
                             + ("" if include_hidden else " (hidden ones need include_hidden=true)"))
    rows = silk.place(scene, rules, references)
    planned = silk.moves(rows, hide_unplaced)
    out = {"placed": sum(r["placed"] for r in rows), "unplaced": [r["reference"] for r in rows if not r["placed"]],
           "hidden": hide_unplaced, "rows": rows, "moves": planned, "applied": False}
    if apply and planned:
        result = edit_footprint_texts(board, planned, "Viaduct: place references")
        out.update(applied=True, verified=result["verified"], failed=result["failed"])
    return out


# ---------- plane fanout ----------

def fanout_net_rules(board, nets: list[str], default: fanout.NetRule) -> dict[str, fanout.NetRule]:
    """Track width and via size per net from its effective netclass, default where unset."""
    by_name = {n.name: n for n in board.get_nets()}
    found = [by_name[n] for n in nets if n in by_name]
    classes = board.get_netclass_for_nets(found) if found else {}
    out = {}
    for n in nets:
        nc = classes.get(n)
        val = lambda attr, d: mm(getattr(nc, attr)) if nc is not None and getattr(nc, attr) else d
        out[n] = fanout.NetRule(val("track_width", default.track_width), val("via_diameter", default.via_diameter),
                                val("via_drill", default.via_drill))
    return out
