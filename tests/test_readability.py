import pytest

from viaduct.readability import ConfigError, check, config as cfgmod, plan_fields, sheet as sh
from viaduct.readability.rules import BY_ID

LIB = """\t(lib_symbols
\t\t(symbol "Device:R"
\t\t\t(symbol "R_0_1"
\t\t\t\t(rectangle (start -1.016 -2.54) (end 1.016 2.54))
\t\t\t)
\t\t\t(symbol "R_1_1"
\t\t\t\t(pin passive line (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
\t\t\t\t(pin passive line (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
\t\t\t)
\t\t)
\t\t(symbol "power:GND"
\t\t\t(power)
\t\t\t(symbol "GND_0_1"
\t\t\t\t(polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27)))
\t\t\t)
\t\t\t(symbol "GND_1_1"
\t\t\t\t(pin power_in line (at 0 0 270) (length 0) (hide yes) (name "GND") (number "1"))
\t\t\t)
\t\t)
\t)
"""


def resistor(ref, x, y, angle=0, mirror=None):
    m = f" (mirror {mirror})" if mirror else ""
    return (f'\t(symbol (lib_id "Device:R") (at {x} {y} {angle}){m} (unit 1) (uuid "u-{ref}")\n'
            f'\t\t(property "Reference" "{ref}" (at {x + 2.54} {y} 0) (effects (font (size 1.27 1.27)) (justify left)))\n'
            f'\t\t(property "Value" "10k" (at {x} {y} 90) (effects (font (size 1.27 1.27)) (hide yes)))\n\t)\n')


def gnd(ref, x, y, angle=0):
    return (f'\t(symbol (lib_id "power:GND") (at {x} {y} {angle}) (unit 1) (uuid "u-{ref}")\n'
            f'\t\t(property "Reference" "{ref}" (at {x} {y + 6} 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
            f'\t\t(property "Value" "GND" (at {x} {y + 4} 0) (effects (font (size 1.27 1.27))))\n\t)\n')


def wire(x1, y1, x2, y2, uid="w"):
    return f'\t(wire (pts (xy {x1} {y1}) (xy {x2} {y2})) (uuid "{uid}"))\n'


def junction(x, y):
    return f'\t(junction (at {x} {y} 0) (diameter 0))\n'


def label(text, x, y, angle=0):
    j = "right bottom" if angle == 180 else "left bottom"
    return f'\t(label "{text}" (at {x} {y} {angle}) (effects (font (size 1.27 1.27)) (justify {j})) (uuid "l-{text}"))\n'


def sheet_file(tmp_path, *items, name="test.kicad_sch"):
    path = tmp_path / name
    path.write_text('(kicad_sch\n\t(version 20260101)\n\t(generator "eeschema")\n' + LIB + "".join(items) + ")\n")
    return path


def run(tmp_path, rule, *items, passives=None, **options):
    s = sh.read(sheet_file(tmp_path, *items))
    (passives or cfgmod.Passives()).mark(s)
    r = BY_ID[rule]
    return list(r.check(s, {**r.options, **options}))


# ---------- reading ----------

@pytest.mark.parametrize("angle,mirror,tip,outward", [
    (0, None, (100, 96.19), (0, -1)),
    (90, None, (96.19, 100), (-1, 0)),          # counter-clockwise on screen
    (180, None, (100, 103.81), (0, 1)),
    (270, None, (103.81, 100), (1, 0)),
    (0, "x", (100, 103.81), (0, 1)),            # mirror x flips vertically
    (90, "y", (103.81, 100), (1, 0)),           # mirror y flips horizontally
])
def test_pin_placement(tmp_path, angle, mirror, tip, outward):
    s = sh.read(sheet_file(tmp_path, resistor("R1", 100, 100, angle, mirror)))
    pin1 = next(p for p in s.symbols[0].pins if p.number == "1")
    assert pin1.tip == pytest.approx(tip)
    assert pin1.outward == pytest.approx(outward)


def test_body_and_fields(tmp_path):
    s = sh.read(sheet_file(tmp_path, resistor("R1", 100, 100, 90)))
    assert s.symbols[0].body.bounds == pytest.approx((97.46, 98.984, 102.54, 101.016))
    assert [t.name for t in s.texts] == ["Reference"]        # hidden Value left out


def test_zero_length_pin_points_away_from_body(tmp_path):
    s = sh.read(sheet_file(tmp_path, gnd("#PWR1", 50, 50)))
    assert s.symbols[0].pins[0].outward == pytest.approx((0, -1))


def test_label_box_sits_above_its_wire(tmp_path):
    s = sh.read(sheet_file(tmp_path, label("SDA", 10, 20), label("SCL", 10, 30, 180)))
    sda, scl = s.labels
    x0, y0, x1, y1 = sda.box.bounds
    assert x0 == pytest.approx(10) and x1 > 12 and y1 < 20
    assert scl.box.bounds[2] == pytest.approx(10) and scl.box.bounds[0] < 8


# ---------- rules ----------

def test_wire_crossing(tmp_path):
    cross = (wire(0, 10, 20, 10, "a"), wire(10, 0, 10, 20, "b"))
    assert len(run(tmp_path, "wire_crossing", *cross)) == 1
    assert run(tmp_path, "wire_crossing", *cross, junction(10, 10)) == []
    assert run(tmp_path, "wire_crossing", wire(0, 10, 20, 10), wire(10, 10, 10, 20)) == []   # a T


def test_four_way_and_missing_junction(tmp_path):
    cross = (wire(0, 10, 20, 10, "a"), wire(10, 0, 10, 20, "b"))
    assert len(run(tmp_path, "four_way_junction", *cross, junction(10, 10))) == 1
    tee = (wire(0, 10, 20, 10), wire(10, 10, 10, 20))
    assert len(run(tmp_path, "missing_junction", *tee)) == 1
    assert run(tmp_path, "missing_junction", *tee, junction(10, 10)) == []
    assert run(tmp_path, "missing_junction", wire(0, 0, 10, 0), wire(10, 0, 10, 10)) == []  # a corner


def test_wire_through_symbol_and_text(tmp_path):
    r = resistor("R1", 100, 100)
    assert len(run(tmp_path, "wire_through_symbol", r, wire(90, 100, 110, 100))) == 1
    assert run(tmp_path, "wire_through_symbol", r, wire(100, 96.19, 100, 90)) == []
    # the Reference field sits right of the body at y=100
    assert len(run(tmp_path, "wire_through_text", r, wire(103.5, 90, 103.5, 110))) == 1
    assert run(tmp_path, "wire_through_text", label("SDA", 10, 20), wire(5, 20, 30, 20)) == []
    assert len(run(tmp_path, "wire_through_text", label("SDA", 10, 20), wire(11, 15, 11, 25))) == 1


@pytest.mark.parametrize("rule,ref", [("pin_exit", "U1"), ("passive_pin_exit", "R1")])
def test_pin_exit(tmp_path, rule, ref):
    r = resistor(ref, 100, 100)                 # pin 1 tip (100, 96.19), leaving up
    assert run(tmp_path, rule, r, wire(100, 96.19, 100, 90)) == []
    side = run(tmp_path, rule, r, wire(100, 96.19, 110, 96.19))
    assert len(side) == 1 and "sideways" in side[0].message
    fan = run(tmp_path, rule, r, wire(100, 96.19, 100, 90), wire(100, 96.19, 90, 96.19))
    assert any("2 wires leave" in f.message for f in fan)
    short = run(tmp_path, rule, r, wire(100, 96.19, 100, 95.5), wire(100, 95.5, 110, 95.5))
    assert len(short) == 1 and "turns" in short[0].message


@pytest.mark.parametrize("ref,passive", [("R1", True), ("C1", True), ("L1", True), ("FB1", True), ("F1", True),
                                         ("RV1", True), ("Y1", True), ("U1", False), ("D1", False), ("LED1", False)])
def test_passives_split_between_pin_exit_rules(tmp_path, ref, passive):
    # each part is checked by exactly one of the two rules
    part = (resistor(ref, 100, 100), wire(100, 96.19, 110, 96.19))
    assert bool(run(tmp_path, "passive_pin_exit", *part)) == passive
    assert bool(run(tmp_path, "pin_exit", *part)) != passive


def test_passives_definition_from_config(tmp_path):
    part = (resistor("U5", 100, 100), wire(100, 96.19, 110, 96.19))     # lib_id Device:R
    by_lib = cfgmod.Passives(references=[], lib_ids=["Device:R*"])
    assert run(tmp_path, "passive_pin_exit", *part, passives=by_lib)
    assert run(tmp_path, "pin_exit", *part, passives=by_lib) == []
    too_many_pins = cfgmod.Passives(max_pins=1)
    assert run(tmp_path, "pin_exit", resistor("R1", 100, 100), wire(100, 96.19, 110, 96.19), passives=too_many_pins)

    cfg = cfgmod.parse({"passives": {"references": ["R", "C"], "lib_ids": ["MyLib:Shunt*"], "max_pins": 3}})
    assert (cfg.passives.references, cfg.passives.lib_ids, cfg.passives.max_pins) == (["R", "C"], ["MyLib:Shunt*"], 3)
    assert cfgmod.parse({}).passives.references == cfgmod.PASSIVE_REFERENCES
    with pytest.raises(ConfigError) as e:
        cfgmod.parse({"passives": {"references": ["R1"], "lib_ids": [1], "max_pins": 0, "prefix": []}})
    for part in ("letters only", "list of strings", "at least 1", "unknown key 'prefix'"):
        assert part in str(e.value)


def test_check_reads_passives_from_viaduct_toml(tmp_path):
    root = sheet_file(tmp_path, resistor("SH1", 100, 100), wire(100, 96.19, 110, 96.19))
    pins = ["pin_exit", "passive_pin_exit"]
    assert {f["rule"] for f in check(root, rules=pins)["findings"]} == {"pin_exit"}
    (tmp_path / "viaduct.toml").write_text('[readability.passives]\nreferences = ["SH"]\n')
    assert check(root, rules=pins)["findings"] == []   # now passive, and passive_pin_exit is off


def test_off_by_default_rules_run_once_the_config_turns_them_on(tmp_path):
    root = sheet_file(tmp_path, resistor("R1", 100, 100), wire(100, 96.19, 110, 96.19), wire(0, 0, 101.6, 0))
    r = check(root)
    assert {"long_wire", "passive_pin_exit"} <= set(r["ignored_rules"])
    assert r["findings"] == [] or {f["rule"] for f in r["findings"]}.isdisjoint({"long_wire", "passive_pin_exit"})
    (tmp_path / "viaduct.toml").write_text('[readability.rules.long_wire]\nseverity = "warning"\n\n'
                                           '[readability.rules.passive_pin_exit]\nseverity = "warning"\n')
    found = {f["rule"] for f in check(root)["findings"]}
    assert {"long_wire", "passive_pin_exit"} <= found


def test_wire_over_pin(tmp_path):
    r = resistor("R1", 100, 100)
    found = run(tmp_path, "wire_over_pin", r, wire(90, 96.19, 110, 96.19))
    assert len(found) == 1 and "middle of a wire" in found[0].message
    assert run(tmp_path, "wire_over_pin", r, wire(100, 96.19, 100, 90)) == []


def test_power_orientation(tmp_path):
    assert run(tmp_path, "power_orientation", gnd("#PWR1", 50, 50)) == []
    up = run(tmp_path, "power_orientation", gnd("#PWR1", 50, 50, 180))
    assert len(up) == 1 and "points up" in up[0].message


def test_off_grid_and_long_wire(tmp_path):
    assert len(run(tmp_path, "off_grid", wire(0, 0, 10.1, 0))) == 1
    assert run(tmp_path, "off_grid", wire(0, 0, 12.7, 0)) == []
    joined = (wire(0, 0, 50.8, 0), wire(50.8, 0, 101.6, 0))
    assert len(run(tmp_path, "long_wire", *joined)) == 1
    assert run(tmp_path, "long_wire", *joined, max_length=200.0) == []


def test_wire_bends(tmp_path):
    zigzag = [wire(0, 0, 5, 0), wire(5, 0, 5, 5), wire(5, 5, 10, 5), wire(10, 5, 10, 10), wire(10, 10, 15, 10)]
    assert len(run(tmp_path, "wire_bends", *zigzag)) == 1
    assert run(tmp_path, "wire_bends", *zigzag, max_bends=4) == []


# ---------- config ----------

def test_config_parses_severity_options_and_suppress():
    cfg = cfgmod.parse({
        "rules": {"wire_crossing": {"severity": "ignore"}, "long_wire": {"max_length": 100}},
        "ignore_sheets": ["legacy/*"],
        "suppress": [{"rule": "text_*", "item": "R1*", "reason": "ok"}],
    })
    assert cfg.severity(BY_ID["wire_crossing"]) == "ignore"
    assert cfg.severity(BY_ID["long_wire"]) == BY_ID["long_wire"].severity     # options alone keep the default
    assert cfg.options_for(BY_ID["long_wire"]) == {"max_length": 100.0}     # int taken as float
    assert cfg.skips("legacy/old.kicad_sch") and not cfg.skips("main.kicad_sch")
    f = BY_ID["text_overlap"].finding("x", (0, 0), ["R12", "C3"])
    assert cfg.suppressed_by(f).reason == "ok"


def test_config_lists_every_problem():
    with pytest.raises(ConfigError) as e:
        cfgmod.parse({
            "rulez": {},
            "rules": {"no_such_rule": {}, "long_wire": {"severity": "fatal", "max_lenght": 1.0},
                      "wire_crossing": {"buses": 1}},
            "suppress": [{"item": "R1"}],
        })
    msg = str(e.value)
    for part in ("readability.rulez", "no_such_rule", "fatal", "max_lenght", "buses: expected bool", "rule is required"):
        assert part in msg


def test_check_walks_hierarchy_and_applies_config(tmp_path):
    sheet_file(tmp_path, wire(0, 10, 20, 10, "a"), wire(10, 0, 10, 20, "b"), name="child.kicad_sch")
    root = sheet_file(tmp_path, wire(0, 10, 20, 10, "c"), wire(10, 0, 10, 20, "d"),
                      '\t(sheet (at 50 50) (size 20 20) (uuid "s1")\n'
                      '\t\t(property "Sheetname" "child" (at 50 49 0) (effects (font (size 1.27 1.27)) (justify left bottom)))\n'
                      '\t\t(property "Sheetfile" "child.kicad_sch" (at 50 71 0) (effects (font (size 1.27 1.27)) (justify left top)))\n'
                      '\t)\n', name="root.kicad_sch")
    r = check(root, rules=["wire_crossing"])
    assert [s["file"] for s in r["sheets"]] == ["root.kicad_sch", "child.kicad_sch"]
    assert r["counts"] == {"error": 0, "warning": 2}

    (tmp_path / "viaduct.toml").write_text(
        '[readability.rules.wire_crossing]\nseverity = "error"\n\n'
        '[[readability.suppress]]\nrule = "wire_crossing"\nsheet = "child.kicad_sch"\n')
    r = check(root, rules=["wire_crossing"])
    assert r["counts"] == {"error": 1, "warning": 0}
    assert r["sheets"][1]["suppressed"] == 1


def test_example_config_lists_every_rule_at_its_defaults():
    from pathlib import Path
    cfg = cfgmod.load(Path(__file__).parent.parent / "docs" / "viaduct.example.toml")
    for rule in BY_ID.values():
        assert cfg.severity(rule) == rule.severity, rule.id
        assert cfg.options.get(rule.id, {}) == rule.options, rule.id
    assert set(cfg.severities) == set(BY_ID)


def test_text_width_matches_kicad_stroke_font():
    # KiCad 10.0.7 measures it as 23.699 font sizes wide, 0.175 of them box margin
    assert sh.text_width("USB_C_Receptacle_USB2.0_16P", 1.27) == pytest.approx((23.699 - 0.175) * 1.27, abs=0.005)


def test_pin_exit_leaves_power_symbols_to_power_orientation(tmp_path):
    # a wire passing the tip of a GND symbol both ways, like a label on a wire
    assert run(tmp_path, "pin_exit", gnd("#PWR1", 50, 50), wire(40, 50, 50, 50), wire(50, 50, 60, 50)) == []


def test_missing_junction_leaves_pin_tips_to_pin_exit(tmp_path):
    r = resistor("R1", 100, 100)                # KiCad draws no dot where wires meet a pin tip
    assert run(tmp_path, "missing_junction", r, wire(100, 96.19, 90, 96.19), wire(100, 96.19, 110, 96.19)) == []


def test_text_overlap_on_lines_not_bounding_box(tmp_path):
    r = resistor("R1", 100, 100)                # body 98.984..101.016 x 97.46..102.54
    inside = '\t(text "x" (at 100 100 0) (effects (font (size 0.5 0.5))) (uuid "t1"))\n'
    across = '\t(text "across" (at 100 100 0) (effects (font (size 1.27 1.27))) (uuid "t2"))\n'
    assert run(tmp_path, "text_overlap", r, inside) == []
    found = run(tmp_path, "text_overlap", r, across)
    assert [f.items for f in found if "drawing" in f.message] == [["across", "R1"]]


def test_wire_through_text_clearance_spares_the_labels_own_wire(tmp_path):
    glabel = ('\t(global_label "SDA" (shape input) (at 20 20 0) (effects (font (size 1.27 1.27)) (justify left))'
              ' (uuid "g1"))\n')
    assert run(tmp_path, "wire_through_text", glabel, wire(10, 20, 20, 20)) == []
    # the label's shape spans y 19.05..20.95: a wire 0.1 mm below it is too close
    assert len(run(tmp_path, "wire_through_text", glabel, wire(20, 21.05, 30, 21.05))) == 1
    assert run(tmp_path, "wire_through_text", glabel, wire(20, 21.05, 30, 21.05), clearance=0.0) == []


# ---------- field planning ----------

def _apply(s, moves):
    for m in moves:
        sym = next(y for y in s.symbols if y.ref == m.text.owner and y.unit == m.text.unit)
        m.text.box = sh.field_box(m.text.text, m.text.font, m.text.justify, m.angle, m.at, sym.turn)


def test_plan_moves_a_field_off_a_wire(tmp_path):
    from viaduct.readability import fields
    s = sh.read(sheet_file(tmp_path, resistor("R1", 100, 100), wire(103.5, 90, 103.5, 110)))
    moves, unplaced = fields.plan(s)
    assert [(m.text.owner, m.text.name) for m in moves] == [("R1", "Reference")] and unplaced == []
    _apply(s, moves)
    assert list(BY_ID["wire_through_text"].check(s, BY_ID["wire_through_text"].options)) == []
    assert list(BY_ID["text_overlap"].check(s, BY_ID["text_overlap"].options)) == []


def test_plan_keeps_planned_fields_apart(tmp_path):
    from viaduct.readability import fields
    # two resistors whose references both sit on the same wire
    s = sh.read(sheet_file(tmp_path, resistor("R1", 100, 100), resistor("R2", 100, 110),
                           wire(103.5, 90, 103.5, 120)))
    moves, _ = fields.plan(s)
    assert len(moves) == 2
    _apply(s, moves)
    assert list(BY_ID["text_overlap"].check(s, BY_ID["text_overlap"].options)) == []


def test_plan_reports_fields_that_fit_nowhere(tmp_path):
    from viaduct.readability import fields
    s = sh.read(sheet_file(tmp_path, resistor("R1", 100, 100), wire(103.5, 90, 103.5, 110)))
    moves, unplaced = fields.plan(s, radius=0.5)       # only the symbol's own position to try
    assert moves == [] and [u["reference"] for u in unplaced] == ["R1"]
    assert unplaced[0]["length_mm"] > 0 and "more room" in unplaced[0]["reason"]


def test_plan_fields_gives_konnect_edits(tmp_path):
    root = sheet_file(tmp_path, resistor("R1", 100, 100), wire(103.5, 90, 103.5, 110))
    r = plan_fields(root)
    (sheet,) = r["sheets"]
    (edit,) = sheet["edits"]
    assert edit["reference"] == "R1" and edit["unit"] == 1
    assert set(edit["field_placements"]["Reference"]) == {"x", "y", "rotation"}
    assert r["fields_moved"] == 1 and r["fields_unplaced"] == 0
