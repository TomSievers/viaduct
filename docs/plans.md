# Copper plans

Every copper write goes through a plan: `check_plan` checks it against the live board without
changing anything, and `apply_plan` checks it again and writes it as one KiCad commit (one
Ctrl+Z), refills the zones and reads the result back from KiCad. `plan_fanout` produces its
result as a plan too.

## Format

```json
{
  "delete": ["<track, arc, via or zone uuid>"],
  "tracks": [{"net": "GND", "layer": "F.Cu", "start": [128.55, 107.6], "end": [129.55, 107.6], "width": 0.2}],
  "vias":   [{"net": "GND", "at": [108.56, 113.2], "diameter": 0.6, "drill": 0.3, "locked": true}],
  "zones":  [{"net": "GND", "layer": "In1.Cu", "name": "GND_In1",
              "outline": [[100.3, 100.3], [159.7, 100.3], [159.7, 137.7], [100.3, 137.7]],
              "priority": 0, "clearance": 0.2, "min_thickness": 0.2}],
  "field_moves":   [{"reference": "R5", "text": "Reference", "dx": 0, "dy": -1.2}],
  "graphic_moves": [{"id": "<text, shape or dimension uuid>", "x": 120, "y": 80, "rotation": 90}]
}
```

Pass it inline (`plan`) or as a path (`plan_file`). All values are millimetres. Tracks and
vias with `"locked": true` are created locked, so an autorouter leaves them alone.

## What is checked

- Clearance from new copper to other nets' copper on every layer it touches, using each
  net's netclass clearance (the larger of the two nets), and between new items.
- 0.25 mm hole-to-hole and 0.3 mm copper-to-edge by default (`hole_to_hole`,
  `edge_clearance`). The API cannot read the board's own constraints, so these are arguments.
- Nets and layers exist; deletes name a track, arc, via or zone that is there.
- Zones are not obstacles: they refill around new copper.

`assume_copper_layers` checks a plan for a stackup that is about to be enabled.

## Text and graphic moves

`field_moves` and `graphic_moves` take the entries of `move_footprint_texts` and
`edit_board_graphics` and follow their rules (no copper, fields only, locked items refused;
see [Text and silkscreen](text-and-graphics.md)). They land in the same commit as the copper,
so moving a via and the label next to it is one Ctrl+Z, and a plan whose move fails its check
writes nothing, copper included. A graphic move of an item the plan deletes is a
`move_deleted` violation.

## Guarding against concurrent edits

`check_plan` returns a `board_token`, a fingerprint of every pad, track and via (zone fills
excluded, since refills change them). Pass it to `apply_plan`: if anyone moved, added,
deleted or re-netted copper since the check, nothing is written and the plan has to be
re-checked. A plan with field or graphic moves gets a token that also covers every text and
graphic it moves, so a `dx`/`dy` never stacks on a move someone made in between; without
moves the token is the plain copper fingerprint. The commit itself is atomic, so there is
never a half-applied plan to save.
