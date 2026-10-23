"""Viaduct MCP server.

Tools work on the board open in KiCad's PCB editor through the IPC API.
Every write is checked against the live copper first and applied as one
KiCad commit, so a single Ctrl+Z undoes it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from viaduct import __version__, connectivity, kicad_io, plan as planmod
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
def check_plan(plan: dict | None = None, plan_file: str | None = None, edge_clearance: float = 0.3,
               hole_to_hole: float = 0.25, assume_copper_layers: int | None = None) -> dict:
    """Check a copper plan against the live board without changing it.

    The plan is JSON in mm: {"delete": [ids], "tracks": [{net, layer, start, end, width}],
    "vias": [{net, at, diameter, drill}], "zones": [{net, layer, outline, name, priority,
    clearance, min_thickness}]}. Pass it inline or as a path to a JSON file.
    assume_copper_layers checks a plan for a stackup that is about to be enabled.
    """
    p = _load_plan(plan, plan_file)
    model = kicad_io.read_model(session.board())   # zones needed so zone deletes resolve
    layers = None
    if assume_copper_layers:
        layers = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, assume_copper_layers - 1)] + ["B.Cu"]
    violations = planmod.check(model, p, edge_clearance, hole_to_hole, layers)
    return {"plan": p.summary(), "ok": not violations, "violations": violations[:100],
            "violation_count": len(violations), "board_token": model.fingerprint()}


@mcp.tool()
def apply_plan(plan: dict | None = None, plan_file: str | None = None, board_token: str | None = None,
               message: str = "Viaduct plan", refill_zones: bool = True,
               edge_clearance: float = 0.3, hole_to_hole: float = 0.25) -> dict:
    """Apply a copper plan as one KiCad commit (one Ctrl+Z), after re-checking it.

    board_token is the value check_plan returned: if the board's copper changed since that
    check (someone edited it meanwhile), nothing is written. Also refuses any plan with a
    violation. Reads the board back afterwards and reports created items that are missing
    or deleted items that are still present.
    """
    p = _load_plan(plan, plan_file)
    board = session.board()
    model = kicad_io.read_model(board)
    if board_token is not None and board_token != model.fingerprint():
        return {"applied": False, "reason": "the board changed since check_plan: re-plan and re-check",
                "board_token_expected": board_token, "board_token_now": model.fingerprint()}
    violations = planmod.check(model, p, edge_clearance, hole_to_hole)
    if violations:
        return {"applied": False, "reason": "plan fails its check", "violation_count": len(violations),
                "violations": violations[:50]}
    result = kicad_io.apply(board, p, message)
    if refill_zones:
        board.refill_zones()
    ok = (result["created"] == result["expected_created"] and not result["created_missing"]
          and not result["deleted_still_present"])
    if len(result["created_ids"]) > 200:
        del result["created_ids"]
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
    """Open a board in a hidden PCB editor (pcbnew on a virtual display); all tools then target it.

    copy=true (default) works on a private copy of the project, so the user's KiCad can stay open
    on the original. copy=false opens the file itself and is refused while KiCad has it open.
    Needs pcbnew and xvfb-run.
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
