# Viaduct

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
- **Board setup the API allows live.** More copper layers, zone properties, refill, save.

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

## Tools

| Tool | What it does |
|---|---|
| `status` | KiCad version, open board, copper layer count, item counts |
| `list_copper` | Pads, tracks, arcs, vias or zones with ids, filtered by net, layer or area |
| `list_zones` | Zones with net, layers, priority, clearance, min thickness, island mode |
| `net_islands` | Groups of a net's copper that are joined; more than one group means a broken net |
| `check_plan` | Checks a plan without writing: clearances per netclass, holes, board edge, nets, layers |
| `apply_plan` | Re-checks, applies as one commit, refills zones, reads back |
| `set_copper_layers` | Adds copper layers (never removes; needs `confirm`) |
| `set_zone_properties` | Name, priority, clearance, min thickness, island removal mode |
| `refill_zones`, `save_board` | As named |
| `open_headless` | Opens a board in a hidden PCB editor; every tool then targets it |
| `close_headless` | Closes it, optionally saving; tools target the user's KiCad again |
| `headless_status` | Whether a hidden editor is open, on which board |

All coordinates and sizes are millimetres.

## Guarding against concurrent edits

`check_plan` returns a `board_token`, a fingerprint of every pad, track and via (zone fills
excluded, since refills change them). Pass it to `apply_plan`: if anyone moved, added,
deleted or re-netted copper since the check, nothing is written and the plan has to be
re-checked. The commit itself is atomic, so there is never a half-applied plan to save.

## Headless editing

KiCad's API is only served by its GUI, and `kicad-cli` has no server mode. `open_headless`
therefore runs a real `pcbnew` on a virtual X display (`xvfb-run`), nobody sees it:

- By default it works on a **private copy** of the project (board, project file, library
  tables), so the user's KiCad can stay open on the original. `copy=false` opens the file
  itself and is refused while KiCad has it open: a second editor would hang on an
  invisible "file is locked" dialog.
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

## Plan format

```json
{
  "delete": ["<track, arc, via or zone uuid>"],
  "tracks": [{"net": "GND", "layer": "F.Cu", "start": [128.55, 107.6], "end": [129.55, 107.6], "width": 0.2}],
  "vias":   [{"net": "GND", "at": [108.56, 113.2], "diameter": 0.6, "drill": 0.3}],
  "zones":  [{"net": "GND", "layer": "In1.Cu", "name": "GND_In1",
              "outline": [[100.3, 100.3], [159.7, 100.3], [159.7, 137.7], [100.3, 137.7]],
              "priority": 0, "clearance": 0.2, "min_thickness": 0.2}]
}
```

Pass it inline (`plan`) or as a path (`plan_file`). Checks use each net's netclass
clearance (the larger of the two nets), 0.25 mm hole-to-hole and 0.3 mm copper-to-edge
by default. Zones are not obstacles: they refill around new copper.

## Requirements

- KiCad 9.0.5 or newer with **Preferences → Plugins → Enable KiCad API** on, and the
  board open in the PCB editor.
- Nix, or Python 3.11+ with `kicad-python`, `shapely`, `numpy` and `mcp`.
- For headless editing: `pcbnew` on PATH (or `VIADUCT_PCBNEW`) and `xvfb-run` (the Nix
  package adds it; otherwise `VIADUCT_XVFB_RUN`).

## Install

```sh
nix build                     # result/bin/viaduct, runs the tests during the build
claude mcp add --scope user viaduct -- $PWD/result/bin/viaduct
```

The server finds KiCad's socket the same way `kicad-python` does; set
`KICAD_API_SOCKET` to override.

## Development

```sh
nix develop -c sh -c 'PYTHONPATH=src python -m pytest'
```

`model`, `plan` and `connectivity` are plain Python and shapely and are tested without
KiCad. `kicad_io` is the only module that talks to KiCad.
