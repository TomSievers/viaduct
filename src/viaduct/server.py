"""Viaduct MCP server.

Tools work on the board open in KiCad's PCB editor through the IPC API.
Every write is checked against the live copper first and applied as one
KiCad commit, so a single Ctrl+Z undoes it.
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.lowlevel import NotificationOptions
from mcp.server.stdio import stdio_server

from viaduct import __version__, connectivity, fanout, kicad_io, plan as planmod, setup_files, silk
from viaduct.headless import Headless

mcp = FastMCP("viaduct", instructions=(
    "Viaduct is experimental and works next to Konnect: it holds features Konnect treats as out of "
    "scope and features still being tried out. Tools may change or disappear between versions; a "
    "tool is removed once Konnect releases the same capability, so prefer Konnect where both exist. "
    "Viaduct edits copper on the board open in KiCad's PCB editor, or in a hidden editor started "
    "with open_headless (then every tool targets the hidden board until close_headless). Units are "
    "millimetres. Always call check_plan before apply_plan, fix every violation, and pass the "
    "board_token check_plan returns: apply_plan refuses a plan that fails its check or that was "
    "checked against a board that has changed since. Writes are single KiCad commits (one undo "
    "step) followed by readback. Use net_islands to find copper not joined to the rest of its net."))

headless = Headless()
session = kicad_io.Session(lambda: headless.socket if headless.running else os.environ.get("KICAD_API_SOCKET"))


def _load_plan(plan: dict | None, plan_file: str | None) -> planmod.Plan:
    if (plan is None) == (plan_file is None):
        raise ValueError("pass exactly one of plan or plan_file")
    data = plan if plan is not None else json.loads(Path(plan_file).expanduser().read_text())
    return planmod.parse(data)


@mcp.tool()
def status() -> dict:
    """Connection check: KiCad version, open board, copper layers and copper item counts."""
    board = session.board()
    return {
        "viaduct": __version__,
        "kicad": session.version(),
        "target": "headless" if headless.running else "live",
        "board": board.name,
        "copper_layers": board.get_copper_layer_count(),
        "tracks": len(board.get_tracks()),
        "vias": len(board.get_vias()),
        "zones": len(board.get_zones()),
        "pads": len(board.get_pads()),
    }


@mcp.tool()
def list_copper(kind: str = "via", net: str | None = None, layer: str | None = None,
                bbox: list[float] | None = None, limit: int = 200) -> dict:
    """List copper items with their ids, for building plans.

    kind: pad, track, arc, via or zone. bbox: [x1, y1, x2, y2] in mm.
    """
    model = kicad_io.read_model(session.board(), with_zones=(kind == "zone"))
    rows = []
    for c in model.items:
        if c.kind != kind or (net is not None and c.net != net) or (layer is not None and layer not in c.geom):
            continue
        b = [round(v, 3) for v in next(iter(c.geom.values())).bounds] if c.geom else None
        if bbox and b and (b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3]):
            continue
        rows.append({"id": c.id, "net": c.net, "layers": c.layers, "ref": c.ref, "at": c.at,
                     "width": c.width, "drill": c.drill, "bbox": b})
    return {"count": len(rows), "items": rows[:limit], "truncated": len(rows) > limit}


@mcp.tool()
def list_zones() -> list[dict]:
    """All zones with id, net, layers, priority, clearance, min thickness and island removal mode."""
    return kicad_io.list_zones(session.board())


@mcp.tool()
def net_islands(net: str = "GND") -> dict:
    """Split a net into groups of copper that actually touch (zone fills included).

    More than one group means the net is broken; every group except "main" lists the pads
    that are cut off. Refill zones first if copper changed since the last fill.
    """
    model = kicad_io.read_model(session.board())
    groups = connectivity.islands(model, net)
    for g in groups:
        g["item_ids"] = g["item_ids"][:50]
    return {"net": net, "groups": len(groups), "islands": groups}


@mcp.tool()
def plan_fanout(planes: dict[str, str | list[str]] | None = None, references: list[str] | None = None,
                fanout_distance: float = 1.5, share_distance: float = 1.0, pads_per_via: int = 4,
                pad_gap: float = 0.1, via_in_pad: bool = False,
                include_connected: bool = False, lock: bool = True, track_width: float | None = None,
                via_diameter: float | None = None, via_drill: float | None = None,
                edge_clearance: float = 0.3, hole_to_hole: float = 0.25, limit: int = 200) -> dict:
    """Plan a via to its plane for every SMD pad of a plane net, before autorouting. Changes nothing.

    planes maps nets to the inner layers of their plane, e.g. {"GND": "In1.Cu", "+3V3": "In2.Cu"};
    by default every net with a filled zone on an inner layer. A pad first shares a connection
    nearby: a track of at most share_distance to a via of its net on the plane, or to a pad
    already joined to it (planned in this run too; one new via serves at most pads_per_via
    pads; share_distance=0 never shares). Otherwise it gets a straight stub to a new through
    via on the plane's fill, at most fanout_distance from the pad's centre: short stubs first,
    inwards on ICs and connectors so signals keep the outside, away from the other pad on
    passives. Tracks run at 0/45/90 degrees, at most one bend of 45.
    Through-hole pads and pads already joined to their plane are skipped (include_connected
    plans them anyway). Sizes come from each net's netclass unless given. The plan's tracks
    and vias are locked (lock=false to leave them free), so Freerouting will not move them.

    Returns rows per pad (placed or not, what it shares, and why not: spots=0 means no legal
    via spot within fanout_distance), the plan, its check and board_token: pass both to
    apply_plan, then refill_zones and net_islands per plane net to confirm every pad reaches
    its plane.
    """
    board = session.board()
    model = kicad_io.read_model(board)
    if planes is not None:
        planes = {n: [ls] if isinstance(ls, str) else list(ls) for n, ls in planes.items()}
    rules = fanout.Rules(fanout_distance=fanout_distance, share_distance=share_distance,
                         pads_per_via=pads_per_via, pad_gap=pad_gap, via_in_pad=via_in_pad,
                         edge_clearance=edge_clearance, hole_to_hole=hole_to_hole)
    nets = list(planes if planes is not None else fanout.plane_layers(model))
    rules.nets = kicad_io.fanout_net_rules(board, nets, rules.default)
    for r in rules.nets.values():
        r.track_width = track_width or r.track_width
        r.via_diameter = via_diameter or r.via_diameter
        r.via_drill = via_drill or r.via_drill
    out = fanout.fanout(model, rules, planes, references, include_connected, lock)
    if out["errors"]:
        return {"ok": False, "errors": out["errors"]}
    p = planmod.parse(out["plan"])
    violations = planmod.check(model, p, edge_clearance, hole_to_hole)
    rows = out["rows"]
    return {"planes": out["planes"],
            "sizes": {n: vars(r) for n, r in rules.nets.items()},
            "pads": len(rows), "placed": sum(r["placed"] for r in rows),
            "shared": sum("shared" in r for r in rows), "new_vias": len(out["plan"]["vias"]),
            "unplaced": [r["pad"] for r in rows if not r["placed"]],
            "rows": rows[:limit], "truncated": len(rows) > limit,
            "plan": out["plan"], "ok": not violations, "violations": violations[:50],
            "board_token": kicad_io.plan_token(board, model, p)}


@mcp.tool()
def check_plan(plan: dict | None = None, plan_file: str | None = None, edge_clearance: float = 0.3,
               hole_to_hole: float = 0.25, assume_copper_layers: int | None = None) -> dict:
    """Check a plan against the live board without changing it.

    The plan is JSON in mm: {"delete": [ids], "tracks": [{net, layer, start, end, width}],
    "vias": [{net, at, diameter, drill}], "zones": [{net, layer, outline, name, priority,
    clearance, min_thickness}], "field_moves": [entries as for move_footprint_texts],
    "graphic_moves": [entries as for edit_board_graphics]}. Pass it inline or as a path to a
    JSON file. Moves are applied in the same commit as the copper, and the returned board_token
    also covers every text and graphic the plan moves. assume_copper_layers checks a plan for a
    stackup that is about to be enabled.
    """
    p = _load_plan(plan, plan_file)
    board = session.board()
    model = kicad_io.read_model(board)   # zones needed so zone deletes resolve
    layers = None
    if assume_copper_layers:
        layers = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, assume_copper_layers - 1)] + ["B.Cu"]
    violations = planmod.check(model, p, edge_clearance, hole_to_hole, layers) + kicad_io.check_moves(board, p)
    return {"plan": p.summary(), "ok": not violations, "violations": violations[:100],
            "violation_count": len(violations), "board_token": kicad_io.plan_token(board, model, p)}


@mcp.tool()
def apply_plan(plan: dict | None = None, plan_file: str | None = None, board_token: str | None = None,
               message: str = "Viaduct plan", refill_zones: bool = True,
               edge_clearance: float = 0.3, hole_to_hole: float = 0.25) -> dict:
    """Apply a plan (copper, field and graphic moves) as one KiCad commit (one Ctrl+Z), after
    re-checking it.

    board_token is the value check_plan returned: if the board's copper, or a text or graphic
    the plan moves, changed since that check (someone edited it meanwhile), nothing is written.
    Also refuses any plan with a violation. Reads the board back afterwards and reports created
    items that are missing, deleted items that are still present, and moves that did not land.
    """
    p = _load_plan(plan, plan_file)
    board = session.board()
    model = kicad_io.read_model(board)
    token_now = kicad_io.plan_token(board, model, p)
    if board_token is not None and board_token != token_now:
        return {"applied": False, "reason": "the board changed since check_plan: re-plan and re-check",
                "board_token_expected": board_token, "board_token_now": token_now}
    violations = planmod.check(model, p, edge_clearance, hole_to_hole) + kicad_io.check_moves(board, p)
    if violations:
        return {"applied": False, "reason": "plan fails its check", "violation_count": len(violations),
                "violations": violations[:50]}
    result = kicad_io.apply(board, p, message)
    if refill_zones:
        board.refill_zones()
    ok = (result["created"] == result["expected_created"] and not result["created_missing"]
          and not result["deleted_still_present"] and result["field_moves"]["verified"]
          and result["graphic_moves"]["verified"])
    if len(result["created_ids"]) > 200:
        del result["created_ids"]
    for key in ("field_moves", "graphic_moves"):
        if len(result[key]["items"]) > 50:
            result[key]["items"] = result[key]["items"][:50]
            result[key]["truncated"] = True
    out = {"applied": True, "verified": ok, "zones_refilled": refill_zones, **result}
    if board_token is None:
        out["warning"] = "applied without board_token: edits made since check_plan were not guarded against"
    return out


@mcp.tool()
def set_copper_layers(count: int, confirm: bool = False) -> dict:
    """Change the number of copper layers on the live board (even number, 2..32).

    Only increases are allowed: removing layers deletes their copper and cannot be undone.
    Without confirm=true this only reports what would change.
    """
    board = session.board()
    current = board.get_copper_layer_count()
    if count % 2 or not 2 <= count <= 32:
        raise ValueError("copper layer count must be even and between 2 and 32")
    if count < current:
        return {"changed": False, "reason": f"refusing to reduce {current} -> {count} copper layers"}
    if count == current or not confirm:
        return {"changed": False, "current": current, "requested": count,
                "note": "pass confirm=true to apply" if count != current else "already set"}
    return {"changed": True, **kicad_io.set_copper_layers(board, count)}


@mcp.tool()
def get_board_setup() -> dict:
    """Board setup Konnect does not read: copper layer count, text variables, grid and drill
    origins, and text and graphics defaults. Read net classes and the physical stackup with
    Konnect's get_netclasses and get_board_stackup.

    `not_in_api` maps the Board Setup settings KiCad's API cannot reach to the tool that writes
    them into the files instead, which needs the project closed in KiCad.
    """
    board = session.board()
    return {
        "copper_layers": board.get_copper_layer_count(),
        "text_variables": kicad_io.text_variables(board),
        "origins": kicad_io.origins(board),
        "graphics_defaults": kicad_io.graphics_defaults(board),
        "not_in_api": {
            "constraints": "Konnect set_design_rules", "predefined sizes": "Konnect set_predefined_sizes",
            "netclass assignments and patterns": "Konnect assign_net_to_class",
            "removing a netclass or unsetting a field": "edit_netclass_file", "stackup": "set_stackup",
            "layer names and types": "set_layer_names", "solder mask, paste, via protection": "set_mask_and_paste",
            "violation severities, teardrops, length tuning, text and graphics defaults": "set_project_settings",
            "custom rules": "Konnect add_design_rule"},
    }


@mcp.tool()
def set_net_class(name: str, clearance: float | None = None, track_width: float | None = None,
                  via_diameter: float | None = None, via_drill: float | None = None,
                  microvia_diameter: float | None = None, microvia_drill: float | None = None,
                  diff_pair_track_width: float | None = None, diff_pair_gap: float | None = None,
                  diff_pair_via_gap: float | None = None, priority: int | None = None,
                  color: str | None = None) -> dict:
    """Create a netclass or change its routing rules (mm). Fields left out keep their value.

    color is #RRGGBB or #RRGGBBAA. A lower priority wins when a net is in several classes.
    KiCad's API cannot unset a field, remove a class or assign nets to one: edit_netclass_file
    and Konnect's assign_net_to_class do that in the project file, with the project closed.
    Changes are project settings outside the undo history. Nets keep their old effective rules
    until save_board, so save before check_plan relies on new values.
    """
    values = {"clearance": clearance, "track_width": track_width, "via_diameter": via_diameter,
              "via_drill": via_drill, "microvia_diameter": microvia_diameter, "microvia_drill": microvia_drill,
              "diff_pair_track_width": diff_pair_track_width, "diff_pair_gap": diff_pair_gap,
              "diff_pair_via_gap": diff_pair_via_gap, "priority": priority, "color": color}
    return kicad_io.set_net_class(session.board(), name, values)


@mcp.tool()
def set_text_variables(variables: dict[str, str] | None = None, remove: list[str] | None = None) -> dict:
    """Add or change project text variables (${NAME} in text), and remove the names in remove.
    Returns all variables afterwards."""
    return kicad_io.set_text_variables(session.board(), variables or {}, remove or ())


@mcp.tool()
def set_board_origin(kind: str, x: float, y: float) -> dict:
    """Move the grid origin or the drill/place origin (kind: grid or drill) to x, y in mm.
    Returns both origins afterwards."""
    return kicad_io.set_origin(session.board(), kind, x, y)


@mcp.tool()
def list_footprint_texts(reference: str) -> dict:
    """A placed footprint's fields (Reference, Value, user fields) and free texts such as the
    ${REFERENCE} on F.Fab: id, layer, board position (mm), rotation, size, visibility."""
    return kicad_io.footprint_texts(session.board(), reference)


@mcp.tool()
def move_footprint_text(reference: str, text: str = "Reference", x: float | None = None, y: float | None = None,
                        dx: float | None = None, dy: float | None = None, rotation: float | None = None,
                        layer: str | None = None, visible: bool | None = None, size: float | None = None,
                        thickness: float | None = None) -> dict:
    """Move a footprint's Reference, Value or other field without moving the footprint.

    text is a field name (Reference, Value, ...) or a field id from list_footprint_texts. Free
    texts inside a footprint (movable: false, e.g. ${REFERENCE} on F.Fab) are refused: KiCad's API
    drops repeated edits of them. Give x and
    y (board mm) or dx and dy. rotation is the angle list_footprint_texts reports; size is the
    character height and width, thickness the stroke (mm). layer must be a non-copper layer, e.g.
    F.Silkscreen <-> F.Fab. One KiCad commit (one Ctrl+Z); pads, tracks and the footprint itself
    are left alone.
    """
    return kicad_io.move_footprint_text(session.board(), reference, text, x, y, dx, dy, rotation, layer, visible,
                                        size, thickness)


@mcp.tool()
def move_footprint_texts(moves: list[dict], limit: int = 200) -> dict:
    """Move or restyle many footprint fields at once, each its own way, in one KiCad commit
    (one Ctrl+Z), without moving any footprint. For tidying the silkscreen of a whole board.

    moves: [{"reference": "R1", "text": "Reference", "x": 10.0, "y": 20.0}, {"reference": "C3",
    "dx": 0, "dy": -1.2, "rotation": 90}, {"reference": "U1", "text": "Value", "layer": "F.Fab",
    "visible": false}, ...]. Keys as in move_footprint_text: text (field name or id, default
    Reference), x/y or dx/dy (mm), rotation, layer (not copper), visible, size, thickness.
    Every move is checked first: one bad move means nothing is written, and all problems are
    listed. Returns verified/failed for all moves and up to `limit` item rows.
    """
    result = kicad_io.edit_footprint_texts(session.board(), moves)
    return {**result, "items": result["items"][:limit], "truncated": len(result["items"]) > limit}


@mcp.tool()
def place_references(references: list[str] | None = None, sizes: list[float | None] | None = None,
                     text_clearance: float = 0.5, pad_clearance: float = 0.15, silk_clearance: float = 0.12,
                     edge_clearance: float = 0.35, max_gap: float = 1.2, rotate: bool = True,
                     include_hidden: bool = False, hide_unplaced: bool = True, apply: bool = False,
                     limit: int = 200) -> dict:
    """Find a readable spot for every Reference on the silkscreen, and hide those that do not fit.

    Each reference goes next to its own part (outside its courtyard, at most max_gap away, closer
    to it than to any other part), clear of pads, other silkscreen, component bodies, the board
    edge and the other references. Spots close to the part and horizontal at the current size
    are tried first; then the smaller sizes, then vertical. sizes are character heights to try,
    in order; null is the current size (default [null, 0.8]; keep the last at or above the
    board's minimum text height). references limits the run to those parts; the others stay put
    and are avoided. Hidden references stay hidden unless include_hidden.

    Without apply this is a dry run: rows say where each reference would go, and moves is ready
    for move_footprint_texts. apply=true moves them all in one KiCad commit (one Ctrl+Z) and
    reads them back. Run DRC afterwards: positions use an estimate of the stroke font's extent.
    """
    rules = silk.Rules(text_clearance=text_clearance, pad_clearance=pad_clearance, silk_clearance=silk_clearance,
                       edge_clearance=edge_clearance, max_gap=max_gap, rotate=rotate)
    if sizes:
        rules.sizes = tuple(sizes)
    out = kicad_io.place_references(session.board(), rules, references, include_hidden, hide_unplaced, apply)
    for key in ("rows", "moves"):
        out["truncated"] = out.get("truncated", False) or len(out[key]) > limit
        out[key] = out[key][:limit]
    return out


@mcp.tool()
def edit_board_graphics(moves: list[dict], include_locked: bool = False, limit: int = 200) -> dict:
    """Move many free board items at once, each its own way, in one KiCad commit (one Ctrl+Z).

    moves: [{"id": ..., "dx": 1.0, "dy": 0}, {"id": <text id>, "x": 120, "y": 80, "rotation": 90,
    "size": 1.2}, {"id": ..., "dx": 0, "dy": 5, "layer": "F.Fab"}, ...]. dx/dy works for any item
    (text, text box, shape, dimension); x/y, rotation and size are for texts. layer must not be
    copper; items on copper are refused, locked ones unless include_locked. Every move is
    checked first: one bad move means nothing is written. Returns verified/failed for all moves
    and up to `limit` item rows.
    """
    result = kicad_io.edit_graphics(session.board(), moves, include_locked)
    return {**result, "items": result["items"][:limit], "truncated": len(result["items"]) > limit}


@mcp.tool()
def list_board_graphics(layer: str | None = None, kind: str | None = None, bbox: list[float] | None = None,
                        contains: str | None = None, limit: int = 200) -> dict:
    """Free board text, text boxes, shapes and dimensions (fab notes, logos drawn as shapes, order
    number placeholders, outlines) with ids and bounding boxes, for move_board_graphics.

    kind: text, textbox, segment, arc, circle, rectangle, polygon, bezier or dimension.
    bbox: [x1, y1, x2, y2] in mm. contains filters text by a substring.
    """
    rows = kicad_io.list_graphics(session.board(), layer, kind, bbox, contains)
    return {"count": len(rows), "items": rows[:limit], "truncated": len(rows) > limit}


@mcp.tool()
def move_board_graphics(ids: list[str], dx: float, dy: float, layer: str | None = None,
                        include_locked: bool = False) -> dict:
    """Shift free board text, text boxes, shapes and dimensions by dx, dy (mm), optionally onto
    another non-copper layer, in one KiCad commit (one Ctrl+Z). Items on copper are refused (they
    are copper); locked items too, unless include_locked. Footprint text moves with move_footprint_text; footprints with Konnect."""
    return kicad_io.move_graphics(session.board(), ids, dx, dy, layer, include_locked)


@mcp.tool()
def set_board_text(text_id: str, x: float | None = None, y: float | None = None, rotation: float | None = None,
                   layer: str | None = None, size: float | None = None) -> dict:
    """Place one free board text at an absolute position (mm) and rotation (degrees), optionally
    on another non-copper layer or with another character size. One KiCad commit."""
    return kicad_io.set_board_text(session.board(), text_id, x, y, rotation, layer, size)


@mcp.tool()
def read_setup_files(board: str) -> dict:
    """Read the board setup KiCad's API cannot reach, from the files of a .kicad_pcb and its
    .kicad_pro: violation severities, teardrop and length-tuning defaults, text and graphics
    defaults, netclasses as stored, solder mask and paste, via protection, layer names and
    types, and the physical stackup (KiCad's default when the board stores none).

    Also reports whether KiCad has the project open: the set_* file tools refuse while it does.
    """
    return setup_files.read(board)


@mcp.tool()
def set_project_settings(board: str, rule_severities: dict | None = None, teardrops: dict | None = None,
                         tuning_patterns: dict | None = None, graphics_defaults: dict | None = None,
                         confirm: bool = False) -> dict:
    """Change Board Setup settings stored in the project file (.kicad_pro next to board).

    rule_severities: {"silk_overlap": "ignore", ...} (error, warning or ignore).
    teardrops: {"options": {...}, "td_round_shape": {...}, "td_rect_shape": {...}, "td_track_end": {...}}.
    tuning_patterns: {"single_track_defaults": {"spacing": 0.8}, ...}.
    graphics_defaults: {"silk_line_width": 0.12, "zones": {"min_clearance": 0.3}, ...}.
    Keys and types must match what read_setup_files shows. KiCad must not have the project
    open. Without confirm=true this is a dry run that lists the changes.
    """
    return setup_files.set_project_settings(board, rule_severities, teardrops, tuning_patterns,
                                            graphics_defaults, confirm)


@mcp.tool()
def edit_netclass_file(board: str, name: str, remove: bool = False, unset: list[str] | None = None,
                       confirm: bool = False) -> dict:
    """Remove a netclass from the project file, or unset fields so it inherits them from Default.

    Removing moves the class's netclass patterns to Default and drops it from net assignments,
    as Board Setup does. unset takes clearance, track_width, via_diameter, via_drill,
    microvia_diameter, microvia_drill, diff_pair_width, diff_pair_gap, diff_pair_via_gap or
    pcb_color. KiCad must not have the project open. Without confirm=true this is a dry run.
    """
    return setup_files.edit_net_class(board, name, remove, unset, confirm)


@mcp.tool()
def set_mask_and_paste(board: str, mask_and_paste: dict | None = None, via_protection: dict | None = None,
                       confirm: bool = False) -> dict:
    """Change solder mask, paste and via protection settings in the board file.

    mask_and_paste: pad_to_mask_clearance, solder_mask_min_width, pad_to_paste_clearance (mm),
    pad_to_paste_clearance_ratio (-0.1 is -10%), allow_soldermask_bridges_in_footprints.
    via_protection: tenting, covering, plugging ({"front": bool, "back": bool}), capping, filling.
    KiCad must not have the project open. Without confirm=true this is a dry run.
    """
    return setup_files.set_mask_and_paste(board, mask_and_paste, via_protection, confirm)


@mcp.tool()
def set_layer_names(board: str, layers: dict, confirm: bool = False) -> dict:
    """Rename layers or set copper layer types in the board file.

    layers: {"In1.Cu": {"name": "GND", "type": "power"}, "User.1": {"name": "Notes"}}. Types are
    signal, power, mixed or jumper (copper only); an empty name restores the default. KiCad
    must not have the project open. Without confirm=true this is a dry run.
    """
    return setup_files.set_layer_names(board, layers, confirm)


@mcp.tool()
def set_stackup(board: str, layers: list[dict] | None = None, copper_finish: str | None = None,
                impedance_controlled: bool | None = None, edge_connector: str | None = None,
                edge_plating: bool | None = None, confirm: bool = False) -> dict:
    """Change the physical stackup in the board file; the board thickness follows the stackup.

    layers: [{"layer": "dielectric 1", "thickness": 1.2, "material": "FR4", "epsilon_r": 4.4,
    "loss_tangent": 0.02, "type": "core"}, {"layer": "F.Cu", "thickness": 0.07}, {"layer":
    "F.Mask", "color": "Black"}]; "sublayer" picks a dielectric sublayer (default 0). Copper
    layers are added live with set_copper_layers, not here. A board without a stored stackup
    gets KiCad's default with the changes applied. edge_connector: none, yes or bevelled.
    KiCad must not have the project open. Without confirm=true this is a dry run.
    """
    return setup_files.set_stackup(board, layers, copper_finish, impedance_controlled, edge_connector,
                                   edge_plating, confirm)


@mcp.tool()
def set_zone_properties(zone_id: str, name: str | None = None, priority: int | None = None,
                        clearance: float | None = None, min_thickness: float | None = None,
                        island_mode: str | None = None) -> dict:
    """Edit a zone's name, priority, clearance (mm), min thickness (mm) or island removal mode
    (always, never or area). Refill zones afterwards to see the effect."""
    return kicad_io.update_zone(session.board(), zone_id, name=name, priority=priority,
                                clearance=clearance, min_thickness=min_thickness, island_mode=island_mode)


@mcp.tool()
def refill_zones() -> dict:
    """Refill all zones on the live board."""
    session.board().refill_zones()
    return {"refilled": True}


@mcp.tool()
def save_board() -> dict:
    """Save the board Viaduct is working on (the hidden one while open_headless is active)."""
    board = session.board()
    board.save()
    return {"saved": board.name, "target": "headless" if headless.running else "live"}


@mcp.tool()
def open_headless(board: str, copy: bool = True, timeout: float = 60.0) -> dict:
    """Open a board in a hidden PCB editor; all tools then target it.

    copy=true (default) works on a private copy of the project, so the user's KiCad can stay open
    on the original. copy=false opens the file itself and is refused while KiCad has it open.
    Linux: pcbnew on a virtual display, needs pcbnew and xvfb-run. Windows (experimental): pcbnew
    started with its windows hidden, found under Program Files or set with VIADUCT_PCBNEW.
    """
    started = headless.start(board, copy=copy, timeout=timeout)
    # the socket opens before the board has loaded; KiCad answers "not ready" until then
    deadline = time.monotonic() + timeout
    while True:
        try:
            b = session.board()
            layers = b.get_copper_layer_count()
            break
        except Exception as e:
            if "not ready" not in str(e).lower() or time.monotonic() > deadline:
                headless.stop()
                raise RuntimeError(f"hidden editor started but the board never became ready: {e}") from e
            time.sleep(1)
    return {**started, "opened": b.name, "copper_layers": layers,
            "ready_after_s": round(timeout - (deadline - time.monotonic()), 1)}


@mcp.tool()
def close_headless(save: bool = False, keep_files: bool | None = None) -> dict:
    """Close the hidden editor; tools target the user's KiCad again.

    save=true saves the hidden board first. For a copy, keep_files (default: same as save)
    keeps it on disk and returns its path; otherwise the private copy is deleted.
    """
    if not headless.running:
        return {"stopped": False, "reason": "no headless editor is open"}
    saved = None
    if save:
        session.board().save()
        saved = str(headless.board)
    result = headless.stop(keep_files=save if keep_files is None else keep_files)
    return {**result, "saved": saved}


@mcp.tool()
def headless_status() -> dict:
    """Whether a hidden editor is open, on which board, and whether it is a copy."""
    return headless.status()


# The FastMCP instance serving stdio. After reload_server the tools come from a newer copy of
# this module, whose own `mcp` never runs: each copy hands the live one on.
_live: FastMCP | None = None


def _source(source: str | None) -> Path:
    path = source or os.environ.get("VIADUCT_SOURCE")
    if not path:
        raise ValueError("no source checkout: pass source, or start the server with VIADUCT_SOURCE set "
                         "to the Viaduct repository")
    root = Path(path).expanduser().resolve()
    if not (root / "src" / "viaduct" / "server.py").exists():
        raise ValueError(f"not a Viaduct checkout: {root}")
    return root


def _revision(root: Path) -> str | None:
    try:
        git = subprocess.run(["git", "-C", str(root), "describe", "--always", "--dirty"],
                             capture_output=True, text=True)
    except OSError:          # no git on PATH
        return None
    return git.stdout.strip() or None


@mcp.tool()
async def reload_server(ctx: Context, source: str | None = None, rebuild: bool = True) -> dict:
    """Load the newest Viaduct code into the running server, without restarting the client.

    source is the Viaduct repository (default: VIADUCT_SOURCE). With Nix installed, rebuild=true
    first runs `nix build` there, which runs the tests and refreshes result/bin/viaduct for the
    next start; a failed build reloads nothing. Without Nix (a pip install) there is no build
    step. The tools are then re-imported from source/src and the client is told the tool list
    changed. An open hidden editor stays open. New dependencies still need an install and a
    restart.
    """
    root = _source(source)
    built = None
    if rebuild and (root / "flake.nix").exists() and shutil.which("nix"):
        nix = await anyio.run_process(["nix", "build"], cwd=root, check=False)
        if nix.returncode != 0:
            return {"reloaded": False, "reason": "nix build failed; the running version is unchanged",
                    "log_tail": nix.stderr.decode(errors="replace")[-2000:]}
        built = str((root / "result").resolve())

    old = {name: mod for name, mod in sys.modules.items() if name == "viaduct" or name.startswith("viaduct.")}
    for name in old:
        del sys.modules[name]
    sys.path.insert(0, str(root / "src"))
    try:
        new = importlib.import_module("viaduct.server")
    except Exception as e:
        for name in [n for n in sys.modules if n == "viaduct" or n.startswith("viaduct.")]:
            del sys.modules[name]
        sys.modules.update(old)
        return {"reloaded": False, "reason": f"the new code does not import: {e!r}"}
    finally:
        sys.path.remove(str(root / "src"))

    # the hidden editor stays open: the new module takes over its process and lock files
    atexit.unregister(headless.stop)
    new.headless.__dict__.update(headless.__dict__)
    live = _live or mcp
    new._live = live
    live._tool_manager = new.mcp._tool_manager
    await ctx.session.send_tool_list_changed()
    return {"reloaded": True, "from": __version__, "to": new.__version__, "revision": _revision(root),
            "built": built, "tools": len(live._tool_manager.list_tools()), "headless": new.headless.status()}


async def _serve():
    server = mcp._mcp_server
    async with stdio_server() as (read, write):
        # tools_changed lets reload_server tell the client to fetch the new tool list
        await server.run(read, write, server.create_initialization_options(NotificationOptions(tools_changed=True)))


def main():
    global _live
    _live = mcp
    anyio.run(_serve)


if __name__ == "__main__":
    main()
