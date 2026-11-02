"""Runs the readability rules over every sheet of a schematic hierarchy. Read-only.

The files are read from disk, so changes KiCad has not saved yet are not seen. Each sheet
file is checked once, however many times the hierarchy uses it.
"""

from __future__ import annotations

from pathlib import Path

from shapely.affinity import translate
from shapely.geometry import box
from shapely.ops import unary_union

from viaduct.readability import config as configmod, fields, fix, sheet as sheetmod, space
from viaduct.readability.rules import BY_ID, RULES

_ORDER = {"error": 0, "warning": 1}


def root_of(schematic: str | Path) -> Path:
    path = Path(schematic).expanduser().resolve()
    if path.suffix == ".kicad_pro":
        path = path.with_suffix(".kicad_sch")
    if path.suffix != ".kicad_sch":
        raise ValueError(f"expected a .kicad_sch or .kicad_pro file, got {path.name}")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def hierarchy(root: Path) -> tuple[list[tuple[Path, sheetmod.Sheet | None, str | None]], list[str]]:
    """Every sheet file below root, depth first: (path, parsed sheet or None, read error),
    and the sheet files that are referenced but missing."""
    out, missing, seen = [], [], set()
    stack = [root]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            missing.append(str(path))
            continue
        try:
            sheet = sheetmod.read(path)
        except (ValueError, KeyError, IndexError, AttributeError) as e:
            out.append((path, None, f"could not read: {e!r}"))
            continue
        out.append((path, sheet, None))
        # sheet files are relative to the file that places them
        stack += [(path.parent / s.file).resolve() for s in reversed(sheet.sheets) if s.file]
    return out, missing


def check(schematic: str | Path, rules: list[str] | None = None, sheets: list[str] | None = None,
          config: str | Path | None = None, include_suppressed: bool = False) -> dict:
    root = root_of(schematic)
    if rules is not None:
        unknown = [r for r in rules if r not in BY_ID]
        if unknown:
            raise ValueError(f"unknown rules {unknown} (known: {', '.join(BY_ID)})")
    cfg_path = Path(config).expanduser() if config else configmod.find(root)
    cfg = configmod.load(cfg_path)
    chosen = [r for r in RULES if rules is None or r.id in rules]
    active = [r for r in chosen if cfg.severity(r) != "ignore"]

    findings, suppressed, rows, crashes = [], [], [], []
    files, missing = hierarchy(root)
    for path, sheet, error in files:
        rel = _relative(path, root)
        if sheets is not None and rel not in sheets and path.name not in sheets:
            continue
        if cfg.skips(rel):
            rows.append({"file": rel, "skipped": "ignore_sheets in the config"})
            continue
        if sheet is None:
            rows.append({"file": rel, "error": error})
            continue
        cfg.passives.mark(sheet)
        counts = {"error": 0, "warning": 0, "suppressed": 0}
        for rule in active:
            try:
                found = list(rule.check(sheet, cfg.options_for(rule)))
            except Exception as e:      # one broken rule must not hide the others' findings
                crashes.append({"rule": rule.id, "sheet": rel, "error": repr(e)})
                continue
            for f in found:
                f.severity, f.sheet = cfg.severity(rule), rel
                why = cfg.suppressed_by(f)
                if why:
                    counts["suppressed"] += 1
                    suppressed.append({**f.as_dict(), "reason": why.reason})
                else:
                    counts[f.severity] += 1
                    findings.append(f)
        rows.append({"file": rel, **counts})

    findings.sort(key=lambda f: (_ORDER.get(f.severity, 2), f.sheet, f.rule, f.at))
    out = {
        "root": str(root),
        "config": str(cfg.path) if cfg.path else None,
        "rules": [r.id for r in active],
        "ignored_rules": [r.id for r in chosen if r not in active],
        "sheets": rows,
        "counts": {s: sum(f.severity == s for f in findings) for s in ("error", "warning")},
        "findings": [f.as_dict() for f in findings],
    }
    if missing:
        out["missing_sheets"] = missing
    if crashes:
        out["rule_errors"] = crashes
    if include_suppressed:
        out["suppressed"] = suppressed
    return out


def plan_fixes(schematic: str | Path, sheets: list[str] | None = None, config: str | Path | None = None,
               rules: list[str] | None = None) -> dict:
    """Konnect edits that solve the findings of the default-error rules, per sheet (fix.py),
    and what the rules would still find once they are applied."""
    root = root_of(schematic)
    cfg = configmod.load(Path(config).expanduser() if config else configmod.find(root))
    if rules is None:
        rules = [r for r in fix.SOLVED if cfg.severity(BY_ID[r]) != "ignore"]
    unknown = [r for r in rules if r not in fix.SOLVED]
    if unknown:
        raise ValueError(f"no solver for {unknown} (solvers: {', '.join(fix.SOLVED)})")
    options = {r.id: cfg.options_for(r) for r in RULES}
    active = [r for r in RULES if cfg.severity(r) == "error"]
    files, _ = hierarchy(root)
    out = []
    for path, sheet, error in files:
        rel = _relative(path, root)
        if sheet is None or (sheets is not None and rel not in sheets and path.name not in sheets) or cfg.skips(rel):
            continue
        cfg.passives.mark(sheet)
        f = fix.SheetFix(sheet, options, set(rules)).run()
        steps = f.steps(str(path))
        if not steps and not f.unresolved:
            continue
        remaining = {r.id: n for r in active if (n := len(list(r.check(sheet, options[r.id]))))}
        out.append({"file": rel, "schematic": str(path), "solved": dict(f.solved), "steps": steps,
                    "unresolved": f.unresolved, "errors_remaining": remaining})
    return {"root": str(root), "rules": rules, "sheets": out,
            "konnect_calls": sum(len(s["steps"]) for s in out),
            "unresolved": sum(len(s["unresolved"]) for s in out)}


def read_sheet(schematic: str | Path) -> sheetmod.Sheet:
    path = Path(schematic).expanduser().resolve()
    if path.suffix != ".kicad_sch":
        raise ValueError(f"expected one sheet's .kicad_sch file, got {path.name}")
    return sheetmod.read(path)


def query_region(schematic: str | Path, bounds: list[float], kinds: list[str] | None = None,
                 distance: float = 0.0, limit: int = 200) -> dict:
    found = space.Space(read_sheet(schematic)).query(box(*bounds), kinds, distance=distance)
    return {"count": len(found), "items": [i.as_dict() for i in found[:limit]], "truncated": len(found) > limit}


def find_position(schematic: str | Path, near: list[float], size: list[float] | None = None,
                  reference: str | None = None, field: str | None = None, radius: float = 20.0,
                  step: float = 1.27, clearance: float = 0.25, kinds: list[str] | None = None,
                  exclude: list[str] | None = None, limit: int = 5) -> dict:
    """Clear positions near a point for a box (size), a symbol field (reference + field) or a
    whole symbol (reference): its body, drawing and visible text."""
    sheet = read_sheet(schematic)
    sp = space.Space(sheet)
    exclude = list(exclude or [])
    # a field may sit inside a body; a box or a whole symbol may not
    default_kinds = ["wire", "line", "body", "text", "sheet", "pin"]
    variants, anchor = [None], tuple(near)
    if size is not None:
        shape = space.rect(*size)
        what = f"{size[0]:g} x {size[1]:g} mm box, centred on the position"
    elif reference is not None and field is not None:
        t = next((t for t in sheet.texts if t.owner == reference and t.name == field), None)
        sym = fields.symbol_of(sheet, t) if t is not None else None
        if sym is None:
            raise ValueError(f"no visible {field} field on a symbol {reference}")
        shape = lambda x, y, a: sheetmod.field_box(t.text, t.font, t.justify, a, (x, y), sym.turn)
        variants = [t.angle, (t.angle + 90) % 180]
        exclude.append(t.label)
        default_kinds.remove("body")
        what = f"{reference} {field} anchor (x, y) and stored rotation"
    elif reference is not None:
        sym = next((s for s in sheet.symbols if s.ref == reference), None)
        if sym is None:
            raise ValueError(f"no symbol {reference} on this sheet")
        extent = unary_union([g for g in (sym.body, sym.strokes) if g is not None and not g.is_empty]
                             + [t.box for t in sheet.texts if t.owner == reference])
        shape = lambda x, y, v: translate(extent, x - sym.at[0], y - sym.at[1])
        exclude.append(reference)
        what = f"{reference} position (its origin)"
    else:
        raise ValueError("pass size, reference, or reference and field")
    found = sp.search(shape, anchor, radius, step, variants, clearance, kinds or default_kinds, exclude, limit=limit)
    return {"position_means": what, "candidates": [
        {"x": c.x, "y": c.y, **({"rotation": c.variant} if c.variant is not None else {}),
         "distance": round(c.cost, 3), "bounds": [round(v, 3) for v in c.geom.bounds]} for c in found]}


def route_wire(schematic: str | Path, start: list[float], end: list[float], clearance: float = 0.25,
               margin: float = 15.0, exclude: list[str] | None = None) -> dict:
    """An orthogonal path between two points, leaving and entering pins straight outward."""
    sheet = read_sheet(schematic)
    sp = space.Space(sheet)
    outward = lambda p: next((q.outward for q in sheet.pins if abs(q.tip[0] - p[0]) < 1e-3
                              and abs(q.tip[1] - p[1]) < 1e-3), None)
    path = sp.route(tuple(start), tuple(end), outward(start), outward(end), clearance, exclude or (), margin)
    if path is None:
        return {"found": False, "reason": "no path on the 1.27 mm grid within margin mm that keeps clear of "
                                          "bodies, text and pins and meets no other wire's end"}
    return {"found": True, "corners": [list(p) for p in path],
            "wires": [{"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]} for a, b in zip(path, path[1:])]}


def describe(schematic: str | Path | None = None, config: str | Path | None = None) -> dict:
    """Every rule with its description and defaults, and what the config makes of it."""
    cfg_path = Path(config).expanduser() if config else configmod.find(root_of(schematic)) if schematic else None
    cfg = configmod.load(cfg_path)
    return {"config": str(cfg.path) if cfg.path else None,
            "rules": [{**r.describe(), "severity": cfg.severity(r), "options": cfg.options_for(r)} for r in RULES],
            "suppress": [s.__dict__ for s in cfg.suppress], "ignore_sheets": cfg.ignore_sheets,
            "passives": cfg.passives.__dict__}


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root.parent).as_posix()
    except ValueError:
        return str(path)
