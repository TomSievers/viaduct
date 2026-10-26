"""Runs the readability rules over every sheet of a schematic hierarchy. Read-only.

The files are read from disk, so changes KiCad has not saved yet are not seen. Each sheet
file is checked once, however many times the hierarchy uses it.
"""

from __future__ import annotations

from pathlib import Path

from viaduct.readability import config as configmod, fields, sheet as sheetmod
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


def plan_fields(schematic: str | Path, sheets: list[str] | None = None, config: str | Path | None = None,
                margin: float = 0.25, radius: float = 20.0) -> dict:
    """New positions for the symbol fields wire_through_text and text_overlap flag, per sheet,
    with the clearances the config gives those rules."""
    root = root_of(schematic)
    cfg = configmod.load(Path(config).expanduser() if config else configmod.find(root))
    wire_clearance = cfg.options_for(BY_ID["wire_through_text"])["clearance"]
    text_clearance = cfg.options_for(BY_ID["text_overlap"])["clearance"]
    files, _ = hierarchy(root)
    out = []
    for path, sheet, error in files:
        rel = _relative(path, root)
        if sheet is None or (sheets is not None and rel not in sheets and path.name not in sheets) or cfg.skips(rel):
            continue
        moves, unplaced = fields.plan(sheet, wire_clearance, text_clearance, margin, radius)
        if not moves and not unplaced:
            continue
        edits = {}
        for m in moves:
            e = edits.setdefault((m.text.owner, m.text.unit),
                                 {"reference": m.text.owner, "unit": m.text.unit, "field_placements": {}})
            e["field_placements"][m.text.name] = {"x": m.at[0], "y": m.at[1], "rotation": m.angle}
        out.append({"file": rel, "schematic": str(path), "edits": list(edits.values()), "unplaced": unplaced})
    return {"root": str(root), "sheets": out,
            "fields_moved": sum(len(e["field_placements"]) for s in out for e in s["edits"]),
            "fields_unplaced": sum(len(s["unplaced"]) for s in out)}


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
