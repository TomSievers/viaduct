# Text and silkscreen

Silkscreen and fabrication markers are moved through KiCad's API, each call one commit (one
Ctrl+Z), read back afterwards. Footprints and copper are never touched.

## Moving items

- **Footprint fields** (`move_footprint_text`): the field is sent on its own with the
  footprint as its container, so KiCad edits it in place. Sending the whole footprint back,
  as footprint tools do, makes KiCad delete and re-create it, which loses its `(units ...)`
  block on save ([Konnect #941](https://github.com/mixelpixx/Konnect/issues/941)); moving a
  field this way keeps it.
- **Free texts inside a footprint** (e.g. `${REFERENCE}` on F.Fab) are refused: KiCad
  10.0.7 drops repeated API edits of them (see [TODO.md](../TODO.md)). Move the fields instead.
- **Free board items** (`move_board_graphics`, `set_board_text`): text, text boxes, segments,
  arcs, circles, rectangles, polygons, Béziers and dimensions. Items on copper are refused,
  since moving copper here would skip the clearance checks; so is a copper target layer.
  Locked items move only with `include_locked`.

For a board KiCad does not have open, use `open_headless(copy=false)`, move, then
`save_board`: the same API path, no direct file editing needed.

## Batches

For large boards, `move_footprint_texts` and `edit_board_graphics` take a list of moves, one
per field or item, each with its own position:
- Every move is checked before anything is sent.
- One bad move means nothing is written; all problems are listed.
- The result reports which moves, if any, failed their readback.

On KiCad's 309-footprint Tiny Tapeout demo, 169 References and 573 free graphics each moved in
one call in about 0.2 s. The same moves can also go into a [copper plan](plans.md#text-and-graphic-moves),
in one commit with the copper.

## Placing references

`place_references` finds a readable spot for every Reference (or the ones in `references`).
A spot is next to its own part:
- outside the part's courtyard, at most `max_gap` away;
- closer to that part than to any other.

It also keeps clear of pads, other silkscreen, component bodies, the board edge and the other
references.

Spots close to the part and horizontal at the current size are tried first, then the smaller
`sizes`, then vertical text. References that fit nowhere are hidden (`hide_unplaced`).
Keep the last size at or above the board's minimum text height.

Without `apply` it is a dry run: the rows say where each reference would go, and `moves` is
ready to edit and pass to `move_footprint_texts`. `apply=true` moves them all in one commit.
Positions use an estimate of the stroke font's extent, so run DRC afterwards.
