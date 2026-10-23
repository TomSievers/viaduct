"""Board setup KiCad's API cannot reach, written straight into the project and board files.

KiCad rewrites both files from memory when it saves, so an edit made while KiCad has the
project open is silently undone. Every write therefore refuses while one of KiCad's lock
files (~<name>.kicad_pcb.lck, ~<name>.kicad_pro.lck) sits beside the files. Writes are
atomic (temporary file, then rename) and touch only the settings asked for: the rest of the
board file stays byte for byte as KiCad wrote it.

Formats follow KiCad 10 (pcb_io_kicad_sexpr.cpp, board_stackup.cpp); boards written by an
older KiCad are refused rather than guessed at. Values are millimetres unless a project-file
setting is stored otherwise (graphics_defaults.dimensions is in nanometres).
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from pathlib import Path

from viaduct import sexpr
from viaduct.headless import lock_file
from viaduct.sexpr import Node, fmt, num, quote, yes

SEVERITIES = ("error", "warning", "ignore")
TEARDROP_TARGETS = ("td_round_shape", "td_rect_shape", "td_track_end")
# netclass fields that belong to the board; a named class without one inherits Default's
NETCLASS_PCB_KEYS = ("clearance", "track_width", "via_diameter", "via_drill", "microvia_diameter",
                     "microvia_drill", "diff_pair_width", "diff_pair_gap", "diff_pair_via_gap", "pcb_color")
SETUP_ORDER = ("stackup", "pad_to_mask_clearance", "solder_mask_min_width", "pad_to_paste_clearance",
               "pad_to_paste_clearance_ratio", "allow_soldermask_bridges_in_footprints", "tenting",
               "covering", "plugging", "capping", "filling", "zone_defaults", "aux_axis_origin",
               "grid_origin", "pcbplotparams")
VIA_SIDES = ("tenting", "covering", "plugging")
VIA_FLAGS = ("capping", "filling")
COPPER_LAYER_TYPES = ("signal", "power", "mixed", "jumper")
FINISHES = ("None", "ENIG", "ENEPIG", "HAL SnPb", "HAL lead-free", "Hard gold", "Immersion tin",
            "Immersion nickel", "Immersion silver", "Immersion gold", "HT_OSP", "OSP", "User defined")
DIELECTRIC_TYPES = ("core", "prepreg")
# KiCad's defaults for a board without a stored stackup (BOARD_STACKUP::BuildDefaultStackupList)
COPPER_NM, MASK_NM = 35_000, 10_000


class ProjectOpen(RuntimeError):
    pass


# ---------- files ----------

def files(board: str) -> tuple[Path, Path]:
    pcb = Path(board).expanduser().resolve()
    if pcb.suffix != ".kicad_pcb":
        raise ValueError(f"expected a .kicad_pcb file, got {pcb.name}")
    if not pcb.exists():
        raise FileNotFoundError(pcb)
    return pcb, pcb.with_suffix(".kicad_pro")


def locks(pcb: Path, pro: Path) -> list[dict]:
    out = []
    for f in (pcb, pro):
        lk = lock_file(f)
        if lk.exists():
            try:
                owner = lk.read_text().strip()
            except OSError as e:
                owner = f"unreadable: {e}"
            out.append({"lock": str(lk), "owner": owner})
    return out


def ensure_closed(pcb: Path, pro: Path) -> None:
    held = locks(pcb, pro)
    if held:
        raise ProjectOpen(
            "KiCad has this project open (" + ", ".join(h["lock"] for h in held) + "). It rewrites the "
            "files from memory when it saves, which would undo this edit. Close the project in KiCad "
            "(and close_headless if the hidden editor holds it), then retry. If no KiCad is running, the "
            "lock is stale and can be deleted.")


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, old: str, new: str) -> None:
    if _read(path) != old:
        raise RuntimeError(f"{path.name} changed while it was being edited; nothing was written, retry")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _diff(a, b, path: str = "") -> list[dict]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in list(a) + [k for k in b if k not in a]:
            out += _diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))
        return out
    return [] if a == b else [{"setting": path, "before": a, "after": b}]


def _commit(path: Path, old: str, new: str, changes: list[dict], confirm: bool, verify) -> dict:
    """Shared dry-run / write / read-back step for every edit."""
    pcb, pro = path.with_suffix(".kicad_pcb"), path.with_suffix(".kicad_pro")
    if not changes:
        return {"changed": False, "file": str(path), "note": "already set"}
    if not confirm:
        return {"changed": False, "file": str(path), "would_change": changes,
                "kicad_has_project_open": bool(locks(pcb, pro)),
                "note": "dry run: pass confirm=true to write (a file edit is not in KiCad's undo history)"}
    ensure_closed(pcb, pro)
    _write(path, old, new)
    return {"changed": True, "file": str(path), "changes": changes, "verified": verify(_read(path))}


# ---------- project file ----------

def _project(pro: Path) -> tuple[str, dict]:
    if not pro.exists():
        raise FileNotFoundError(f"no project file next to the board: {pro}")
    text = _read(pro)
    return text, json.loads(text)


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def project_settings(data: dict) -> dict:
    ds = data.get("board", {}).get("design_settings", {})
    options = ds.get("teardrop_options") or [{}]
    teardrops = {"options": options[0]}
    for p in ds.get("teardrop_parameters", []):
        teardrops[p.get("td_target_name")] = {k: v for k, v in p.items() if k != "td_target_name"}
    return {"rule_severities": ds.get("rule_severities", {}), "teardrops": teardrops,
            "tuning_patterns": ds.get("tuning_pattern_settings", {}), "graphics_defaults": ds.get("defaults", {})}


def _patch(current: dict, patch: dict, path: str) -> dict:
    """current with patch merged in; only settings the file already has, with the same type."""
    if not isinstance(patch, dict):
        raise ValueError(f"{path} is a group of settings: pass an object")
    out = copy.deepcopy(current)
    for k, v in patch.items():
        p = f"{path}.{k}"
        if k not in current:
            raise ValueError(f"{p} is not a setting in this project file; known: {sorted(current)}")
        c = current[k]
        if isinstance(c, dict):
            out[k] = _patch(c, v, p)
        elif isinstance(c, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{p} takes true or false")
            out[k] = v
        elif isinstance(c, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{p} takes a number")
            if isinstance(c, int) and not float(v).is_integer():
                raise ValueError(f"{p} takes a whole number")
            out[k] = int(v) if isinstance(c, int) else float(v)
        elif isinstance(c, str):
            if not isinstance(v, str):
                raise ValueError(f"{p} takes text")
            out[k] = v
        else:
            raise ValueError(f"{p} cannot be set with this tool")
    return out


def set_project_settings(board: str, rule_severities: dict | None = None, teardrops: dict | None = None,
                         tuning_patterns: dict | None = None, graphics_defaults: dict | None = None,
                         confirm: bool = False) -> dict:
    _, pro = files(board)
    text, data = _project(pro)
    new = copy.deepcopy(data)
    ds = new["board"]["design_settings"]
    if rule_severities:
        bad = {k: v for k, v in rule_severities.items() if v not in SEVERITIES}
        if bad:
            raise ValueError(f"severities must be one of {SEVERITIES}: {bad}")
        ds["rule_severities"] = _patch(ds["rule_severities"], rule_severities, "rule_severities")
    if teardrops:
        for target, values in teardrops.items():
            if target == "options":
                ds["teardrop_options"][0] = _patch(ds["teardrop_options"][0], values, "teardrops.options")
                continue
            i = next((i for i, p in enumerate(ds["teardrop_parameters"]) if p.get("td_target_name") == target), None)
            if i is None:
                raise ValueError(f"teardrops takes options or one of {TEARDROP_TARGETS}, not {target!r}")
            current = ds["teardrop_parameters"][i]
            if "td_target_name" in values:
                raise ValueError("td_target_name cannot be changed")
            ds["teardrop_parameters"][i] = _patch(current, values, f"teardrops.{target}")
    if tuning_patterns:
        ds["tuning_pattern_settings"] = _patch(ds["tuning_pattern_settings"], tuning_patterns, "tuning_patterns")
    if graphics_defaults:
        ds["defaults"] = _patch(ds["defaults"], graphics_defaults, "graphics_defaults")
    after = project_settings(new)
    changes = _diff(project_settings(data), after)
    return _commit(pro, text, _dump(new), changes, confirm,
                   lambda t: project_settings(json.loads(t)) == after)


def file_net_classes(data: dict) -> list[dict]:
    ns = data.get("net_settings", {})
    return [{k: c[k] for k in ("name", "priority", *NETCLASS_PCB_KEYS) if k in c} for c in ns.get("classes", [])]


def edit_net_class(board: str, name: str, remove: bool = False, unset: list[str] | None = None,
                   confirm: bool = False) -> dict:
    """Remove a netclass, or unset fields so it inherits them from Default again."""
    if name == "Default":
        raise ValueError("the Default netclass can neither be removed nor lose a value")
    if remove == bool(unset):
        raise ValueError("pass either remove=true or the fields to unset")
    _, pro = files(board)
    text, data = _project(pro)
    new = copy.deepcopy(data)
    ns = new["net_settings"]
    cls = next((c for c in ns["classes"] if c["name"] == name), None)
    if cls is None:
        raise ValueError(f"no netclass named {name!r} in {pro.name}")
    changes = []
    if remove:
        ns["classes"] = [c for c in ns["classes"] if c["name"] != name]
        changes.append({"setting": f"net_classes.{name}", "before": cls, "after": None})
        # as Board Setup does: patterns of a removed class go to Default
        for p in ns.get("netclass_patterns", []):
            if p.get("netclass") == name:
                p["netclass"] = "Default"
                changes.append({"setting": f"netclass_patterns.{p.get('pattern')}", "before": name, "after": "Default"})
        for net, classes in list(ns.get("netclass_assignments", {}).items()):
            if name in classes:
                left = [c for c in classes if c != name]
                if left:
                    ns["netclass_assignments"][net] = left
                else:
                    del ns["netclass_assignments"][net]
                changes.append({"setting": f"netclass_assignments.{net}", "before": classes, "after": left or None})
    else:
        unknown = set(unset) - set(NETCLASS_PCB_KEYS)
        if unknown:
            raise ValueError(f"cannot unset {sorted(unknown)}; board fields are {NETCLASS_PCB_KEYS}")
        for f in unset:
            if f in cls:
                changes.append({"setting": f"net_classes.{name}.{f}", "before": cls[f], "after": None})
                del next(c for c in ns["classes"] if c["name"] == name)[f]
    expected = file_net_classes(new)
    return _commit(pro, text, _dump(new), changes, confirm,
                   lambda t: file_net_classes(json.loads(t)) == expected)


# ---------- board file ----------

def _board(pcb: Path) -> tuple[str, Node]:
    text = _read(pcb)
    root = sexpr.parse(text)
    gen = root.first("generator_version")
    if gen is None or not gen.atoms[0].startswith("10."):
        raise ValueError(f"{pcb.name} was not written by KiCad 10 (generator_version "
                         f"{gen.atoms[0] if gen else 'missing'}); open and save it in KiCad 10 first")
    if root.first("setup") is None:
        raise ValueError(f"{pcb.name} has no (setup) section")
    return text, root


def _splice(text: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def _put(text: str, parent: Node, item: list, order: tuple[str, ...]) -> tuple[int, int, str]:
    """An edit that replaces parent's child named item[0], or inserts it where KiCad would."""
    name = item[0]
    existing = parent.first(name)
    if existing is not None:
        depth = sexpr.indent_of(text, existing).count("\t")
        return existing.start, existing.end, fmt(item, depth)
    later = [n for n in parent.lists() if n.name in order and order.index(n.name) > order.index(name)]
    if later:
        indent = sexpr.indent_of(text, later[0])
        return later[0].start, later[0].start, fmt(item, indent.count("\t")) + "\n" + indent
    last = parent.lists()[-1]
    indent = sexpr.indent_of(text, last)
    return last.end, last.end, "\n" + indent + fmt(item, indent.count("\t"))


def _value(node: Node | None, default):
    if node is None:
        return default
    v = node.atoms[0]
    if isinstance(default, bool):
        return v == "yes"
    return float(v)


def mask_and_paste(root: Node) -> dict:
    s = root.first("setup")
    out = {"pad_to_mask_clearance": _value(s.first("pad_to_mask_clearance"), 0.0),
           "solder_mask_min_width": _value(s.first("solder_mask_min_width"), 0.0),
           "pad_to_paste_clearance": _value(s.first("pad_to_paste_clearance"), 0.0),
           "pad_to_paste_clearance_ratio": _value(s.first("pad_to_paste_clearance_ratio"), 0.0),
           "allow_soldermask_bridges_in_footprints": _value(s.first("allow_soldermask_bridges_in_footprints"), False)}
    return out


def via_protection(root: Node) -> dict:
    s = root.first("setup")
    out = {}
    for side in VIA_SIDES:
        n = s.first(side)
        if n is not None and n.atoms:
            raise ValueError(f"({side} ...) is in an older format; save the board in KiCad 10 first")
        out[side] = {k: _value(n.first(k) if n else None, False) for k in ("front", "back")}
    for flag in VIA_FLAGS:
        out[flag] = _value(s.first(flag), False)
    return out


def set_mask_and_paste(board: str, mask: dict | None = None, vias: dict | None = None,
                       confirm: bool = False) -> dict:
    pcb, _ = files(board)
    text, root = _board(pcb)
    setup = root.first("setup")
    before = {"mask_and_paste": mask_and_paste(root), "via_protection": via_protection(root)}
    after = copy.deepcopy(before)
    edits = []
    for k, v in (mask or {}).items():
        if k not in before["mask_and_paste"]:
            raise ValueError(f"mask_and_paste takes {sorted(before['mask_and_paste'])}, not {k!r}")
        if k == "allow_soldermask_bridges_in_footprints":
            if not isinstance(v, bool):
                raise ValueError(f"{k} takes true or false")
            atom = yes(v)
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"{k} takes a number")
            if k == "solder_mask_min_width" and v < 0:
                raise ValueError("solder_mask_min_width cannot be negative")
            if k == "pad_to_paste_clearance_ratio" and not -1 < v <= 1:
                raise ValueError("pad_to_paste_clearance_ratio is a fraction (-0.1 is -10%)")
            v, atom = float(v), num(v)
        after["mask_and_paste"][k] = v
        edits.append(_put(text, setup, [k, atom], SETUP_ORDER))
    for k, v in (vias or {}).items():
        if k in VIA_SIDES:
            if not isinstance(v, dict) or set(v) - {"front", "back"} or \
                    not all(isinstance(x, bool) for x in v.values()):
                raise ValueError(f"{k} takes {{\"front\": bool, \"back\": bool}}")
            after["via_protection"][k].update(v)
            sides = after["via_protection"][k]
            edits.append(_put(text, setup, [k, ["front", yes(sides["front"])], ["back", yes(sides["back"])]],
                              SETUP_ORDER))
        elif k in VIA_FLAGS:
            if not isinstance(v, bool):
                raise ValueError(f"{k} takes true or false")
            after["via_protection"][k] = v
            edits.append(_put(text, setup, [k, yes(v)], SETUP_ORDER))
        else:
            raise ValueError(f"via_protection takes {VIA_SIDES + VIA_FLAGS}, not {k!r}")
    changes = _diff(before, after)
    new = _splice(text, edits)
    return _commit(pcb, text, new, changes, confirm,
                   lambda t: {"mask_and_paste": mask_and_paste(sexpr.parse(t)),
                              "via_protection": via_protection(sexpr.parse(t))} == after)


# layer table

def layer_table(root: Node) -> list[dict]:
    rows = []
    for n in root.first("layers").lists():
        a = [sexpr.unquote(x) for x in n.items if isinstance(x, str)]
        rows.append({"id": int(a[0]), "layer": a[1], "type": a[2], "name": a[3] if len(a) > 3 else a[1], "_node": n})
    return rows


def _layers_view(root: Node) -> dict:
    return {r["layer"]: {"type": r["type"], "name": r["name"]} for r in layer_table(root)}


def set_layer_names(board: str, layers: dict, confirm: bool = False) -> dict:
    """layers: {"In1.Cu": {"name": "GND", "type": "power"}, "User.1": {"name": "Notes"}}."""
    pcb, _ = files(board)
    text, root = _board(pcb)
    rows = {r["layer"]: r for r in layer_table(root)}
    before = _layers_view(root)
    after = copy.deepcopy(before)
    edits = []
    for layer, change in layers.items():
        if layer not in rows:
            raise ValueError(f"{layer} is not enabled on this board; enabled: {sorted(rows)}")
        if not isinstance(change, dict) or set(change) - {"name", "type"}:
            raise ValueError(f"{layer} takes {{\"name\": ..., \"type\": ...}}")
        row = after[layer]
        if "type" in change:
            if not layer.endswith(".Cu"):
                raise ValueError(f"only copper layers have a type; {layer} has none to set")
            if change["type"] not in COPPER_LAYER_TYPES:
                raise ValueError(f"copper layer type must be one of {COPPER_LAYER_TYPES}")
            row["type"] = change["type"]
        if "name" in change:
            name = (change["name"] or layer).strip()
            if not name or '"' in name:
                raise ValueError(f"invalid layer name {change['name']!r}")
            row["name"] = name
    names = [r["name"] for r in after.values()]
    dupes = {n for n in names if names.count(n) > 1} | {r["name"] for L, r in after.items()
                                                       if r["name"] != L and r["name"] in after}
    if dupes:
        raise ValueError(f"layer names must be unique: {sorted(dupes)}")
    for layer in layers:
        r, node = after[layer], rows[layer]["_node"]
        item = [str(rows[layer]["id"]), quote(layer), r["type"]] + ([quote(r["name"])] if r["name"] != layer else [])
        edits.append((node.start, node.end, fmt(item, 0)))
    changes = _diff(before, after)
    return _commit(pcb, text, _splice(text, edits), changes, confirm,
                   lambda t: _layers_view(sexpr.parse(t)) == after)


# physical stackup

def _kind(item: dict) -> str:
    t = item["type"]
    if t == "copper":
        return "copper"
    if t in DIELECTRIC_TYPES or item["layer"].startswith("dielectric"):
        return "dielectric"
    return {"Solder Mask": "mask", "Silk Screen": "silk", "Solder Paste": "paste"}.get(
        next((k for k in ("Solder Mask", "Silk Screen", "Solder Paste") if k in t), ""), "other")


def stored_stackup(root: Node) -> dict | None:
    st = root.first("setup").first("stackup")
    if st is None:
        return None
    m = {"layers": [], "copper_finish": None, "dielectric_constraints": False, "edge_connector": None,
         "edge_plating": False}
    for n in st.lists():
        if n.name == "layer":
            item = {"layer": n.atoms[0], "type": None, "sub": [{}]}
            for x in n.items[2:]:
                if isinstance(x, str):
                    if x != "addsublayer":
                        raise ValueError(f"unexpected {x!r} in stackup layer {item['layer']}")
                    item["sub"].append({})
                    continue
                sub, a = item["sub"][-1], x.atoms
                if x.name == "type":
                    item["type"] = a[0]
                elif x.name == "thickness":
                    sub["thickness"] = float(a[0])
                    if "locked" in a[1:]:
                        sub["locked"] = True
                elif x.name in ("epsilon_r", "loss_tangent"):
                    sub[x.name] = float(a[0])
                elif x.name in ("material", "color"):
                    sub[x.name] = a[0]
                else:
                    raise ValueError(f"unexpected ({x.name}) in stackup layer {item['layer']}; not rewriting it")
            m["layers"].append(item)
        elif n.name == "copper_finish":
            m["copper_finish"] = n.atoms[0]
        elif n.name in ("dielectric_constraints", "edge_plating"):
            m[n.name] = n.atoms[0] == "yes"
        elif n.name == "edge_connector":
            m["edge_connector"] = n.atoms[0]
        else:
            raise ValueError(f"unexpected ({n.name}) in the stackup; not rewriting it")
    return m


def default_stackup(root: Node) -> dict:
    """The stackup KiCad builds for a board that stores none."""
    enabled = [r["layer"] for r in layer_table(root)]
    copper = [L for L in enabled if L.endswith(".Cu")]
    general = root.first("general")
    thickness = general.first("thickness") if general else None
    board_nm = int(round(float(thickness.atoms[0] if thickness else 1.6) * 1e6))
    masks = sum(m in enabled for m in ("F.Mask", "B.Mask"))
    diel_nm = (board_nm - COPPER_NM * len(copper) - MASK_NM * masks) // max(1, len(copper) - 1)
    layers = []
    for L, t in (("F.SilkS", "Top Silk Screen"), ("F.Paste", "Top Solder Paste"), ("F.Mask", "Top Solder Mask")):
        if L in enabled:
            layers.append({"layer": L, "type": t, "sub": [{"thickness": MASK_NM / 1e6} if "Mask" in L else {}]})
    for i, L in enumerate(copper):
        layers.append({"layer": L, "type": "copper", "sub": [{"thickness": COPPER_NM / 1e6}]})
        if L != "B.Cu":
            layers.append({"layer": f"dielectric {i + 1}", "type": DIELECTRIC_TYPES[i & 1],
                           "sub": [{"thickness": diel_nm / 1e6, "material": "FR4", "epsilon_r": 4.5,
                                    "loss_tangent": 0.02}]})
    for L, t in (("B.Mask", "Bottom Solder Mask"), ("B.Paste", "Bottom Solder Paste"), ("B.SilkS", "Bottom Silk Screen")):
        if L in enabled:
            layers.append({"layer": L, "type": t, "sub": [{"thickness": MASK_NM / 1e6} if "Mask" in L else {}]})
    return {"layers": layers, "copper_finish": None, "dielectric_constraints": False, "edge_connector": None,
            "edge_plating": False}


def _stackup_item(m: dict) -> list:
    out = ["stackup"]
    for it in m["layers"]:
        row = ["layer", quote(it["layer"]), ["type", quote(it["type"])]]
        for i, s in enumerate(it["sub"]):
            if i:
                row.append("addsublayer")
            if "color" in s:
                row.append(["color", quote(s["color"])])
            if "thickness" in s:
                row.append(["thickness", num(s["thickness"])] + (["locked"] if s.get("locked") else []))
            if "material" in s:
                row.append(["material", quote(s["material"])])
                for k in ("epsilon_r", "loss_tangent"):
                    if k in s:
                        row.append([k, num(s[k])])
        out.append(row)
    if m["copper_finish"]:
        out.append(["copper_finish", quote(m["copper_finish"])])
    out.append(["dielectric_constraints", yes(m["dielectric_constraints"])])
    if m["edge_connector"]:
        out.append(["edge_connector", m["edge_connector"]])
    if m["edge_plating"]:
        out.append(["edge_plating", "yes"])
    return out


def stackup_thickness(m: dict) -> float:
    return round(sum(s.get("thickness", 0.0) for it in m["layers"] for s in it["sub"]
                     if _kind(it) in ("copper", "dielectric", "mask")), 6)


def stackup_view(m: dict) -> dict:
    """The stackup keyed by layer, for reading and for change reports."""
    return {"thickness": stackup_thickness(m), "copper_finish": m["copper_finish"],
            "impedance_controlled": m["dielectric_constraints"], "edge_connector": m["edge_connector"],
            "edge_plating": m["edge_plating"],
            "layers": {it["layer"]: {"type": it["type"], **({"sublayers": it["sub"]} if len(it["sub"]) > 1
                                                            else it["sub"][0])} for it in m["layers"]}}


STACKUP_FIELDS = {"thickness": ("copper", "dielectric", "mask"), "material": ("dielectric", "mask", "silk"),
                  "epsilon_r": ("dielectric", "mask"), "loss_tangent": ("dielectric", "mask"),
                  "color": ("dielectric", "mask", "silk"), "type": ("dielectric",)}


def set_stackup(board: str, layers: list[dict] | None = None, copper_finish: str | None = None,
                impedance_controlled: bool | None = None, edge_connector: str | None = None,
                edge_plating: bool | None = None, confirm: bool = False) -> dict:
    pcb, _ = files(board)
    text, root = _board(pcb)
    stored = stored_stackup(root)
    model = copy.deepcopy(stored) if stored else default_stackup(root)
    before = stackup_view(model)
    by_name = {it["layer"]: it for it in model["layers"]}
    for change in layers or []:
        change = dict(change)
        name, idx = change.pop("layer", None), change.pop("sublayer", 0)
        if name not in by_name:
            raise ValueError(f"no stackup layer {name!r}; layers: {list(by_name)}")
        it = by_name[name]
        if not 0 <= idx < len(it["sub"]):
            raise ValueError(f"{name} has {len(it['sub'])} sublayer(s)")
        kind = _kind(it)
        for k, v in change.items():
            if k not in STACKUP_FIELDS:
                raise ValueError(f"stackup layers take {sorted(STACKUP_FIELDS)} (plus layer, sublayer), not {k!r}")
            if kind not in STACKUP_FIELDS[k]:
                raise ValueError(f"{k} cannot be set on {name} ({kind})")
            if k == "type":
                if v not in DIELECTRIC_TYPES:
                    raise ValueError(f"dielectric type must be one of {DIELECTRIC_TYPES}")
                it["type"] = v
            elif k in ("material", "color"):
                if not isinstance(v, str) or not v or '"' in v:
                    raise ValueError(f"{k} takes a name")
                it["sub"][idx][k] = v
            else:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (k == "thickness" and v == 0) \
                        or (k == "epsilon_r" and v < 1):
                    raise ValueError(f"{k} on {name} must be a positive number" + (" (at least 1)" if k == "epsilon_r" else ""))
                it["sub"][idx][k] = float(v)
    if copper_finish is not None:
        if copper_finish not in FINISHES:
            raise ValueError(f"copper_finish must be one of {FINISHES}")
        model["copper_finish"] = copper_finish
    if impedance_controlled is not None:
        model["dielectric_constraints"] = bool(impedance_controlled)
    if edge_connector is not None:
        if edge_connector not in ("none", "yes", "bevelled"):
            raise ValueError("edge_connector must be none, yes or bevelled")
        model["edge_connector"] = None if edge_connector == "none" else edge_connector
    if edge_plating is not None:
        model["edge_plating"] = bool(edge_plating)
    after = stackup_view(model)
    changes = _diff(before, after)
    if not stored and changes:
        changes.insert(0, {"setting": "stackup", "before": "not stored (KiCad's default)",
                           "after": "stored in the board file"})
    edits = [_put(text, root.first("setup"), _stackup_item(model), SETUP_ORDER)]
    # KiCad's board thickness is the sum of the stackup
    general = root.first("general")
    thick = general.first("thickness") if general else None
    if thick is not None and abs(float(thick.atoms[0]) - after["thickness"]) > 1e-9:
        edits.append((thick.start, thick.end, fmt(["thickness", num(after["thickness"])], 0)))
    def verify(t: str) -> bool:
        r = sexpr.parse(t)
        g = r.first("general")
        th = g.first("thickness") if g else None
        return stackup_view(stored_stackup(r)) == after and (th is None or float(th.atoms[0]) == after["thickness"])
    return _commit(pcb, text, _splice(text, edits), changes, confirm, verify)


# ---------- reading ----------

def read(board: str) -> dict:
    pcb, pro = files(board)
    held = locks(pcb, pro)
    out = {"board": str(pcb), "project": str(pro) if pro.exists() else None,
           "kicad_has_project_open": bool(held), "locks": held}
    if pro.exists():
        _, data = _project(pro)
        out["project_settings"] = {**project_settings(data), "net_classes": file_net_classes(data)}
    _, root = _board(pcb)
    stored = stored_stackup(root)
    out["board_settings"] = {
        "mask_and_paste": mask_and_paste(root), "via_protection": via_protection(root),
        "layers": _layers_view(root),
        "stackup": {"stored": stored is not None, **stackup_view(stored or default_stackup(root))}}
    return out
