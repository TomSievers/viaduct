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

Which tool is which, with the Konnect issues: [docs/konnect.md](docs/konnect.md).

## What it does

- **Verified batch edits.** A copper plan (deletes, tracks, vias, zones, text moves) is
  checked against the live board, then applied as one KiCad commit, so one Ctrl+Z undoes it.
  The result is read back from KiCad. [Copper plans](docs/plans.md)
- **Real connectivity.** `net_islands` splits a net into the groups of copper that actually
  touch, zone fills included, so cut-off pads show up by name.
- **Plane fanout before autorouting.** `plan_fanout` joins every SMD pad of a plane net to
  its plane with a short locked stub and via, sharing a nearby via or pad where it can, so
  Freerouting only has signals left to route. [Plane fanout](docs/fanout.md)
- **Text and silkscreen.** Moves footprint fields, board text, shapes and dimensions without
  touching footprints or copper, and places every Reference where it is readable.
  [Text and silkscreen](docs/text-and-graphics.md)
- **Board setup.** Netclasses, text variables, origins and copper layers live on the open
  board; the stackup, layer names, mask and paste and other settings the API cannot reach,
  in the files while KiCad has the project closed. [Board setup](docs/board-setup.md)
- **A hidden editor.** `open_headless` runs a PCB editor nobody sees, on a copy of the
  project, so work can be tried out before it touches the user's board.
  [Headless editing](docs/headless.md)

Routing helpers (point-to-point routing, multilayer autorouting) come next and build on the
same checked-plan path. What KiCad's API still lacks is tracked in [TODO.md](TODO.md).

## Tools

All coordinates and sizes are millimetres. Tools marked (file) edit the project files and
need the project closed in KiCad.

**Board and copper**: [plans](docs/plans.md), [fanout](docs/fanout.md)

| Tool | What it does |
|---|---|
| `status` | KiCad version, open board, copper layer count, item counts |
| `list_copper` | Pads, tracks, arcs, vias or zones with ids, filtered by net, layer or area |
| `list_zones` | Zones with net, layers, priority, clearance, min thickness, island mode |
| `net_islands` | Groups of a net's copper that are joined; more than one group means a broken net |
| `check_plan` | Checks a plan without writing: clearances per netclass, holes, board edge, nets, layers |
| `apply_plan` | Re-checks, applies as one commit, refills zones, reads back |
| `plan_fanout` | Joins every SMD pad of a plane net to its plane; the plan goes to `apply_plan` |
| `set_zone_properties` | Name, priority, clearance, min thickness, island removal mode |
| `refill_zones`, `save_board` | As named |

**Text and silkscreen**: [details](docs/text-and-graphics.md)

| Tool | What it does |
|---|---|
| `list_footprint_texts` | A footprint's fields and texts with ids, layer, position, rotation, size |
| `move_footprint_text` | Moves or restyles a footprint's Reference, Value or other field, not the footprint |
| `move_footprint_texts` | The same for many fields at once, each its own way, in one commit |
| `place_references` | Finds a readable spot for every Reference and hides those that fit nowhere; dry run by default |
| `list_board_graphics` | Free board text, text boxes, shapes and dimensions with ids and bounding boxes |
| `move_board_graphics` | Shifts free board items by dx/dy, optionally onto another non-copper layer |
| `edit_board_graphics` | Moves many free board items at once, each its own way, in one commit |
| `set_board_text` | Places one free board text at an absolute position, rotation and size |

**Board setup**: [details](docs/board-setup.md)

| Tool | What it does |
|---|---|
| `get_board_setup` | Copper layer count, text variables, origins, text and graphics defaults |
| `set_net_class` | Creates a netclass or sets its clearance, track width, via, microvia and diff pair sizes |
| `set_text_variables` | Adds, changes or removes project text variables |
| `set_board_origin` | Moves the grid or drill/place origin |
| `set_copper_layers` | Adds copper layers (never removes; needs `confirm`) |
| `read_setup_files` | Board setup from the files: stackup, layers, mask and paste, severities, teardrops, defaults, netclasses |
| `set_stackup` | Layer thickness, material, εr, loss tangent, color, finish, impedance control, edge connector and plating (file) |
| `set_layer_names` | Layer names and copper layer types (file) |
| `set_mask_and_paste` | Solder mask and paste clearances, via tenting, covering, plugging, capping, filling (file) |
| `set_project_settings` | Violation severities, teardrop, length-tuning and text and graphics defaults (file) |
| `edit_netclass_file` | Removes a netclass or unsets its fields so they come from Default (file) |

**Editor and server**: [headless editing](docs/headless.md)

| Tool | What it does |
|---|---|
| `open_headless` | Opens a board in a hidden PCB editor; every tool then targets it |
| `close_headless` | Closes it, optionally saving; tools target the user's KiCad again |
| `headless_status` | Whether a hidden editor is open, on which board |
| `reload_server` | Rebuilds and loads the newest Viaduct code into the running server |

## Install

Linux and Windows, with pip or Nix: [docs/install.md](docs/install.md). In short, from a
checkout:

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
claude mcp add --scope user viaduct -e VIADUCT_SOURCE=$PWD -- $PWD/.venv/bin/viaduct
```

KiCad 10 with **Preferences → Plugins → Enable KiCad API** on, and the board open in the PCB
editor. The hidden editor is supported on Linux and experimental on Windows.

### Updating a running server

`reload_server` picks up new code without restarting Claude:
- With Nix, it runs `nix build` in `VIADUCT_SOURCE` first, tests included; a failed build
  reloads nothing. A pip install has no build step.
- It re-imports the tools from its `src` and tells the client the tool list changed.
- `rebuild=false` skips the build and loads the working tree as it is.
- An open hidden editor stays open.

New dependencies (reinstall), or a change to how the server starts (`main`), still need a restart.

## Development

```sh
nix develop -c sh -c 'PYTHONPATH=src python -m pytest'   # with Nix
.venv/bin/pip install pytest && .venv/bin/python -m pytest   # with a pip install
```

`model`, `plan`, `connectivity`, `fanout` and `silk` are plain Python and shapely and are
tested without KiCad. `kicad_io` is the only module that talks to KiCad.
