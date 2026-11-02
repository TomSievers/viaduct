# Schematic readability

`check_readability` checks how readable a schematic is, the way ERC checks whether it is
correct: each rule looks for one layout problem, on every sheet of the hierarchy, and
reports where it is, which items are involved and how to fix it. An AI can fix the findings
and run the check again until it is clean. Nothing is changed.

KiCad's API has no schematic access, so the `.kicad_sch` files are read from disk: save in
KiCad first. Each sheet file is checked once, however many times the hierarchy places it.

## Rules

`list_readability_rules` returns the same list with each rule's options and the severity the
config gives it. Rules whose default is `ignore` are off until the config gives them a
severity.

| Rule | Default | Finds |
|---|---|---|
| `wire_through_text` | error | A wire through, or within `clearance` of, a label, field or free text |
| `wire_through_symbol` | error | A wire over a symbol body or sheet box (`power_symbols`) |
| `wire_over_pin` | error | A wire touching a pin other than by ending at its tip |
| `wire_overlap` | error | Wires on top of each other |
| `wire_crossing` | warning | Wires crossing without connecting (`buses`) |
| `missing_junction` | error | Three or more wires meeting without a dot (pin tips are `pin_exit`'s) |
| `four_way_junction` | warning | Four or more wire directions at one dot |
| `pin_exit` | warning | A wire leaving a pin sideways, turning within `min_stub`, or more than one wire per pin (`sheet_pins`); power symbols and passives are left out |
| `passive_pin_exit` | ignore | The same for passive parts, as set in [`[readability.passives]`](#passives) |
| `text_overlap` | error | Text over text, over a line of any symbol's drawing (its own too), or over a sheet box (`clearance`) |
| `symbol_overlap` | error | Symbol bodies or sheet boxes overlapping |
| `small_text` | warning | Visible text under `min_size` mm |
| `diagonal_wire` | warning | Wires neither horizontal nor vertical (`buses`) |
| `off_grid` | warning | Wire ends, junctions, pins, labels off the `grid` |
| `power_orientation` | warning | Supply symbols not pointing up, ground (`down_pattern`) not pointing down |
| `long_wire` | ignore | Straight runs over `max_length` mm |
| `wire_bends` | warning | Paths between two connection points with over `max_bends` bends |

Each rule is a module in `src/viaduct/readability/rules/`; `RULES` in that package's
`__init__.py` is the list that runs. A new rule subclasses `Rule`, sets `id`, `title`,
`severity` and `options` (each option with its default, which also fixes its type), and
yields `Finding`s from `check(sheet, options)`.

## Fixing findings

`plan_readability_fixes` turns the error findings into Konnect calls, sheet by sheet. The
solvers run in order on an in-memory copy of the sheet, each seeing what the ones before it
changed:

| Order | Finding | Solver |
|---|---|---|
| 1 | `wire_overlap` | The overlapping wires become one run, split where the old ones ended |
| 2 | `wire_through_text`, `text_overlap` on a net label | The label turns to the nearest clear orientation; its connection point stays put |
| 3 | `wire_through_symbol`, `wire_through_text` on another net's wire, `wire_over_pin` | The wire path is routed again between its two connection points, leaving pins straight outward; a path with a loose end connects nothing there and is removed |
| 4 | `missing_junction` | A dot wherever three or more wires meet afterwards |
| 5 | `text_overlap`, `wire_through_text` on a symbol field | The field moves to the nearest clear spot, in either orientation, that still reads as its symbol's |

Each sheet's `steps` are Konnect calls with their arguments, in the order to apply them:
`batch_add_wire`, `batch_delete_schematic_wire` (it prunes the dots deleted wires leave
unjustified, judged with the new wires already in place), `batch_add_junction`,
`batch_rotate_labels`, then `edit_schematic_component` per symbol. `errors_remaining` is what
the error rules would still find once they are applied; run `check_readability` afterwards to
confirm. No step changes a net: the plans were checked against KiCad's netlist before and
after.

Listed under `unresolved`, with a reason and often a suggestion, instead of solved:
- a pin on a wire's middle: a junction connects it, or the wire is routed away, and only the
  designer knows which is meant;
- `symbol_overlap`: a clear spot for one symbol is suggested, but moving it leaves its wires;
- free text over a wire or text: a clear spot is suggested, but Konnect cannot move text;
- two labels with the same name on the same point: `batch_rotate_labels` cannot tell them apart;
- a field, label or wire with no clear position or route nearby: the parts need more room
  (a field's length is given), or the text shortened, or the wire replaced by net labels.

### Search tools

The solvers are thin layers over one indexed view of a sheet (`space.py`), and the same view
is exposed for working out fixes the solvers leave, without testing every item by hand:

- `query_schematic_region`: everything in or near a rectangle, by kind (wire, line, body,
  text, sheet, pin, point), with ids to pass on.
- `find_clear_position`: the nearest positions where a box, a symbol's field or a whole
  symbol fits clearance clear of the chosen kinds. Candidates are tried nearest first and the
  search stops at the first that cannot be beaten, so it takes milliseconds.
- `route_schematic_wire`: an orthogonal path on the 1.27 mm grid between two points that keeps
  clear of bodies, text and pins, never runs along or ends on another wire, may cross wires
  but never turns on one, and leaves and enters pins straight outward.

## Config

Severities, options, skipped sheets and accepted findings live in the `[readability]`
table of `viaduct.toml` next to the root schematic, or in the file passed as `config`.
[viaduct.example.toml](viaduct.example.toml) lists every rule at its defaults.

```toml
[readability]
ignore_sheets = ["legacy/*.kicad_sch"]

[readability.rules.wire_crossing]
severity = "ignore"            # error, warning or ignore

[readability.rules.long_wire]
max_length = 120.0

[[readability.suppress]]
rule = "text_overlap"          # a rule id or a glob
sheet = "power.kicad_sch"      # optional
item = "R12"                   # optional: a reference, label text or uuid in the finding
reason = "value inside the divider on purpose"
```

### Passives

`pin_exit` and `passive_pin_exit` split the parts between them: `[readability.passives]`
says which parts are passive, so both rules always agree. A part is passive when its
reference prefix (the letters before the number: `FB` in `FB3`) is in `references`, or its
library symbol matches a glob in `lib_ids`, and it has at most `max_pins` visible pins.

```toml
[readability.passives]
references = ["R", "C", "L", "FB", "F", "RT", "RV", "Y"]   # the default
lib_ids = ["MyLib:Shunt*"]          # default: none
max_pins = 2                        # the default
```

The default covers resistors, capacitors, inductors, ferrite beads, fuses, thermistors,
varistors and crystals. The pin limit keeps a three-pin potentiometer (`RV`) or a four-pin
crystal (`Y`) with the active parts. Prefixes match whole: `RV1` is not an `R`, and `LED1`
is not an `L`.

The whole file is checked before a run. An unknown key, rule or option, a severity that is
not one of the three, or a value of the wrong type refuses it with every problem listed, so
a typo cannot quietly switch a rule off. Suppressed findings are counted per sheet;
`include_suppressed=true` lists them with their reason.

## Limits

- Text widths use the advance of each character in KiCad's stroke font, measured from
  KiCad 10.0.7, so they match the default font; an outline (TrueType) font is approximated.
- For `wire_through_symbol` and `symbol_overlap`, a body is the bounding box of its drawing,
  so a wire through the empty corner of a triangular op-amp symbol counts as through the
  body. `text_overlap` tests the drawn lines instead.
- Pin names and numbers, bus entries and images are not checked yet.
- The rules see geometry, not meaning: whether functional blocks are grouped or signals
  flow left to right is not judged.
