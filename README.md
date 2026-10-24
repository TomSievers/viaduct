# Viaduct

> [!WARNING]
> **Warning: the tool set is not stable.** Tools may be added, changed or removed without
> notice. A tool is removed once the same capability lands in a released version of
> [Konnect](https://github.com/mixelpixx/Konnect).

An MCP server for KiCad that works next to Konnect, over the same official IPC API
(`kicad-python`). It has two jobs:

1. **Features Konnect treats as out of scope.** Konnect deliberately exposes KiCad's own
   capabilities and does not become a second router or PCB editor. Work that needs
   Konnect-owned logic, such as checked copper plans, connectivity analysis, routing
   helpers and a hidden editor, lives here instead.
2. **A testing ground.** New features and AI workflows (tools, skills, guidance) are tried
   here first to see what is actually useful. That includes features Konnect plans but has
   not released yet: they are used here until a Konnect release covers them, and then
   removed from Viaduct.

What it does today:

- **Verified batch edits.** A copper plan (deletes, tracks, vias, zones) is checked
  against the live board, then applied as one KiCad commit, so one Ctrl+Z undoes it.
  The result is read back from KiCad.
- **Real connectivity.** `net_islands` splits a net into the groups of copper that
  actually touch, zone fills included, so cut-off pads show up by name.
- **Board setup on the open board.** Netclass routing rules (clearance, track width, via and
  diff pair sizes) changed live, text variables, origins, more copper layers, zone
  properties, refill, save. Reading net classes and the stackup is left to Konnect.
- **Moving text and graphics.** Footprint Reference/Value fields, free board text, text
  boxes, shapes and dimensions (fab notes, order-number markers, logos), without moving
  footprints or touching copper.
- **Board setup the API cannot reach, in the files.** Stackup, layer names and types, solder
  mask and paste, via protection, violation severities, teardrop, length-tuning and graphics
  defaults, netclass removal, written into `.kicad_pcb` / `.kicad_pro` while KiCad has the
  project closed. See [TODO.md](TODO.md) for what is still missing.

Viaduct leaves what Konnect already covers (schematic work, placement, DRC, exports) to
Konnect. Routing helpers (via placement, fanout, point-to-point routing) come next and
build on the same checked-plan path.

### Overlap with Konnect

Some tools fill gaps Konnect has open issues for. Each is removed once a Konnect release
ships the capability:

| Viaduct capability | Konnect issue |
|---|---|
| Batch copper edits in one commit, via deletion (`apply_plan`) | [#825](https://github.com/mixelpixx/Konnect/issues/825), [#500](https://github.com/mixelpixx/Konnect/issues/500) |
| Zone deletion (`apply_plan` with zone ids) | [#431](https://github.com/mixelpixx/Konnect/issues/431) |
| Live copper-layer expansion (`set_copper_layers`) | [#721](https://github.com/mixelpixx/Konnect/issues/721) |
| Netclass edits on a board open in KiCad (`set_net_class`) | Konnect's `create_netclass` edits `.kicad_pro` and refuses while KiCad holds the project ([#791](https://github.com/mixelpixx/Konnect/issues/791), [#817](https://github.com/mixelpixx/Konnect/issues/817)); writing through `SetNetClasses` is named as a follow-up there but not filed. Diff pair fields: [#777](https://github.com/mixelpixx/Konnect/issues/777) |
| Netclass removal, unsetting netclass fields (`edit_netclass_file`) | [#882](https://github.com/mixelpixx/Konnect/issues/882) |
| Setting the physical stackup (`set_stackup`) | [#716](https://github.com/mixelpixx/Konnect/issues/716) |
| Moving a footprint's Reference/Value text (`move_footprint_text`) | [#829](https://github.com/mixelpixx/Konnect/issues/829) |

Removed because a Konnect release covers them: reading net classes and the physical stackup
(`get_netclasses` and `get_board_stackup`, Konnect v0.13.0). Left to Konnect's file writers and
not duplicated: constraints (`set_design_rules`), predefined sizes (`set_predefined_sizes`),
creating netclasses and assigning nets (`create_netclass`, `assign_net_to_class`) and custom
rules (`add_design_rule`). With no Konnect counterpart or issue: text variables, origins,
layer names, solder mask and paste, via protection, violation severities, teardrop,
length-tuning and graphics defaults.

## Tools

| Tool | What it does |
|---|---|
| `status` | KiCad version, open board, copper layer count, item counts |
| `list_copper` | Pads, tracks, arcs, vias or zones with ids, filtered by net, layer or area |
| `list_zones` | Zones with net, layers, priority, clearance, min thickness, island mode |
| `net_islands` | Groups of a net's copper that are joined; more than one group means a broken net |
| `check_plan` | Checks a plan without writing: clearances per netclass, holes, board edge, nets, layers |
| `apply_plan` | Re-checks, applies as one commit, refills zones, reads back |
| `get_board_setup` | Copper layer count, text variables, origins, text and graphics defaults |
| `set_net_class` | Creates a netclass or sets its clearance, track width, via, microvia and diff pair sizes |
| `set_text_variables` | Adds, changes or removes project text variables |
| `set_board_origin` | Moves the grid or drill/place origin |
| `list_footprint_texts` | A footprint's fields and texts with ids, layer, position, rotation, size |
| `move_footprint_text` | Moves or restyles a footprint's Reference, Value or other field, not the footprint |
| `move_footprint_texts` | The same for many fields at once, each its own way, in one commit |
| `list_board_graphics` | Free board text, text boxes, shapes and dimensions with ids and bounding boxes |
| `move_board_graphics` | Shifts free board items by dx/dy, optionally onto another non-copper layer |
| `edit_board_graphics` | Moves many free board items at once, each its own way (offset, or text position, rotation, size), in one commit |
| `set_board_text` | Places one free board text at an absolute position, rotation and size |
| `set_copper_layers` | Adds copper layers (never removes; needs `confirm`) |
| `read_setup_files` | Board setup from the files: stackup, layers, mask and paste, severities, teardrops, defaults, netclasses |
| `set_stackup` | Layer thickness, material, εr, loss tangent, color, finish, impedance control, edge connector and plating (file) |
| `set_layer_names` | Layer names and copper layer types (file) |
| `set_mask_and_paste` | Solder mask and paste clearances, via tenting, covering, plugging, capping, filling (file) |
| `set_project_settings` | Violation severities, teardrop, length-tuning and text and graphics defaults (file) |
| `edit_netclass_file` | Removes a netclass or unsets its fields so they come from Default (file) |
| `set_zone_properties` | Name, priority, clearance, min thickness, island removal mode |
| `refill_zones`, `save_board` | As named |
| `open_headless` | Opens a board in a hidden PCB editor; every tool then targets it |
| `close_headless` | Closes it, optionally saving; tools target the user's KiCad again |
| `headless_status` | Whether a hidden editor is open, on which board |
| `reload_server` | Rebuilds and loads the newest Viaduct code into the running server |

All coordinates and sizes are millimetres. Tools marked (file) edit the project files and
need the project closed in KiCad; see [Board setup in the files](#board-setup-in-the-files).

## Guarding against concurrent edits

`check_plan` returns a `board_token`, a fingerprint of every pad, track and via (zone fills
excluded, since refills change them). Pass it to `apply_plan`: if anyone moved, added,
deleted or re-netted copper since the check, nothing is written and the plan has to be
re-checked. A plan with field or graphic moves gets a token that also covers every text and
graphic it moves, so a `dx`/`dy` never stacks on a move someone made in between; without
moves the token is the plain copper fingerprint. The commit itself is atomic, so there is
never a half-applied plan to save.

## Headless editing

KiCad's API is only served by its GUI, and `kicad-cli` has no server mode. `open_headless`
therefore runs a real `pcbnew` on a virtual X display (`xvfb-run`), nobody sees it:

- By default it works on a **private copy** of the project (board, project file, library
  tables), so the user's KiCad can stay open on the original. `copy=false` opens the file
  itself and is refused while KiCad has it open: a second editor would hang on an
  invisible "file is locked" dialog.
- It never reaches the user's screen: in a Wayland session GTK would otherwise ignore
  Xvfb's `DISPLAY` and open the editor on the user's compositor, so `WAYLAND_DISPLAY` is
  removed and `GDK_BACKEND=x11` is set.
- Its socket lives under a short private `TMPDIR` (`/tmp/vdXXXX/t/kicad/api.sock`), apart
  from the user's `/tmp/kicad/api.sock` and under the ~108 character socket path limit,
  past which KiCad silently opens no socket.
- `open_headless` waits until the board answers (KiCad reports "not ready" while loading),
  and gives up with the editor's log if a dialog blocks it.
- `close_headless(save=true)` keeps a saved copy on disk and returns its path.

Typical flow: plan and verify on a hidden copy, then apply the same plan (with a fresh
`check_plan` token) to the user's board in one commit.

### KiCad 11

KiCad 11 plans a built-in headless mode; KiCad 10 will never get one, which is why
Viaduct runs the GUI on Xvfb today. Once KiCad 11 is released, `open_headless` will
check the installed KiCad version and use the built-in headless mode on 11 or newer,
keeping the Xvfb method for KiCad 10. The tools' behaviour stays the same either way.

## Board setup

`get_board_setup` returns the board setup Konnect does not read, plus a `not_in_api` list of
the pages KiCad's API cannot reach (constraints, predefined sizes, netclass assignments,
custom rules and more; details in [TODO.md](TODO.md)). Read net classes and the stackup with
Konnect's `get_netclasses` and `get_board_stackup`.

`set_net_class` changes only the fields it is given and reads the class back. KiCad keeps
each net's effective rules until the board is saved, so call `save_board` before `check_plan`
relies on new clearances. Netclass changes are project settings outside the undo history.
The API cannot unset a netclass field, remove a class or assign nets to one: those stay in
`edit_netclass_file` and Konnect's `assign_net_to_class`, which edit the project file while
the project is closed in KiCad.

## Moving text and graphics

Silkscreen and fabrication markers are moved through KiCad's API, each call one commit (one
Ctrl+Z), read back afterwards. For large boards, `move_footprint_texts` and
`edit_board_graphics` take a list of moves, one per field or item, each with its own position:
every move is checked before anything is sent, one bad move means nothing is written (all
problems are listed), and the result reports which moves, if any, failed their readback. On
KiCad's 309-footprint Tiny Tapeout demo, 169 References and 573 free graphics each moved in
one call in about 0.2 s.

- **Footprint fields** (`move_footprint_text`): the field is sent on its own with the
  footprint as its container, so KiCad edits it in place. Sending the whole footprint back,
  as footprint tools do, makes KiCad delete and re-create it, which loses its `(units ...)`
  block on save ([Konnect #941](https://github.com/mixelpixx/Konnect/issues/941)); moving a field this way keeps it.
- **Free texts inside a footprint** (e.g. `${REFERENCE}` on F.Fab) are refused: KiCad
  10.0.7 drops repeated API edits of them (see [TODO.md](TODO.md)). Move the fields instead.
- **Free board items** (`move_board_graphics`, `set_board_text`): text, text boxes, segments,
  arcs, circles, rectangles, polygons, Béziers and dimensions. Items on copper are refused,
  since moving copper here would skip the clearance checks; so is a copper target layer.
  Locked items move only with `include_locked`.

For a board KiCad does not have open, use `open_headless(copy=false)`, move, then
`save_board`: the same API path, no direct file editing needed.

### Board setup in the files

Most of Board Setup is not in KiCad's API (details in [TODO.md](TODO.md)). The (file) tools
write those settings into the `.kicad_pcb` and `.kicad_pro` instead, as Konnect does for
its own file writers:

- **KiCad must not have the project open.** It rewrites both files from memory when it saves,
  which would silently undo the edit. A write refuses while `~<name>.kicad_pcb.lck` or
  `~<name>.kicad_pro.lck` exists, and names the lock. A lock left by a crashed KiCad can be
  deleted. `close_headless` removes the locks its hidden editor created.
- **Dry run first.** Without `confirm=true` a tool only lists the changes; a file edit is not
  in KiCad's undo history.
- **Only the requested settings change.** The board file is edited in place: every other
  byte stays as KiCad wrote it. Writes are atomic, refuse if the file changed meanwhile, and
  are read back.
- **KiCad 10 files only.** A board written by an older KiCad is refused; open and save it in
  KiCad 10 first.
- **Stackup.** A board without a stored stackup gets KiCad's own default (same thicknesses
  and materials KiCad would use) with the changes applied, and the board thickness is set to
  the stackup's total, as Board Setup does.

Each tool was checked against KiCad 10.0.7: KiCad loads the edited files and keeps every
value when it saves the board again; the stackup and netclasses were also read back over
its API.

## Plan format

```json
{
  "delete": ["<track, arc, via or zone uuid>"],
  "tracks": [{"net": "GND", "layer": "F.Cu", "start": [128.55, 107.6], "end": [129.55, 107.6], "width": 0.2}],
  "vias":   [{"net": "GND", "at": [108.56, 113.2], "diameter": 0.6, "drill": 0.3}],
  "zones":  [{"net": "GND", "layer": "In1.Cu", "name": "GND_In1",
              "outline": [[100.3, 100.3], [159.7, 100.3], [159.7, 137.7], [100.3, 137.7]],
              "priority": 0, "clearance": 0.2, "min_thickness": 0.2}],
  "field_moves":   [{"reference": "R5", "text": "Reference", "dx": 0, "dy": -1.2}],
  "graphic_moves": [{"id": "<text, shape or dimension uuid>", "x": 120, "y": 80, "rotation": 90}]
}
```

Pass it inline (`plan`) or as a path (`plan_file`). Checks use each net's netclass
clearance (the larger of the two nets), 0.25 mm hole-to-hole and 0.3 mm copper-to-edge
by default. Zones are not obstacles: they refill around new copper.

`field_moves` and `graphic_moves` take the entries of `move_footprint_texts` and
`edit_board_graphics` and follow their rules (no copper, fields only, locked items refused).
They land in the same commit as the copper, so moving a via and the label next to it is one
Ctrl+Z, and a plan whose move fails its check writes nothing, copper included. A graphic move
of an item the plan deletes is a `move_deleted` violation.

## Requirements

- KiCad 9.0.5 or newer with **Preferences → Plugins → Enable KiCad API** on, and the
  board open in the PCB editor.
- Nix, or Python 3.11+ with `kicad-python`, `shapely`, `numpy` and `mcp`.
- For headless editing: `pcbnew` on PATH (or `VIADUCT_PCBNEW`) and `xvfb-run` (the Nix
  package adds it; otherwise `VIADUCT_XVFB_RUN`).

## Install

```sh
nix build                     # result/bin/viaduct, runs the tests during the build
claude mcp add --scope user -e VIADUCT_SOURCE=$PWD viaduct -- $PWD/result/bin/viaduct
```

The server finds KiCad's socket the same way `kicad-python` does; set
`KICAD_API_SOCKET` to override.

### Updating a running server

`reload_server` picks up new code without restarting Claude: it runs `nix build` in
`VIADUCT_SOURCE` (tests included; a failed build reloads nothing), re-imports the tools from
its `src`, and tells the client the tool list changed. `rebuild=false` skips the build and
loads the working tree as it is. An open hidden editor stays open. New dependencies, or a
change to how the server starts (`main`), still need a restart.

## Development

```sh
nix develop -c sh -c 'PYTHONPATH=src python -m pytest'
```

`model`, `plan` and `connectivity` are plain Python and shapely and are tested without
KiCad. `kicad_io` is the only module that talks to KiCad.
