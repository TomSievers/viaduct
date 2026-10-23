# TODO: board setup the KiCad API does not cover yet

Live board setup (through KiCad's IPC API, on the open board) covers: copper layer count
(`set_copper_layers`), netclass values (`set_net_class`), text variables, origins, and
reading the stackup and netclasses (Konnect's `get_board_stackup`, `get_netclasses`).
Everything below is missing from the API, or broken in it, as of **KiCad 10.0.7** with
**kicad-python 0.5**.

Until KiCad exposes them, most of these settings are written into the files instead, while
KiCad has the project closed (the lock files `~<name>.kicad_pcb.lck` and
`~<name>.kicad_pro.lck` must be absent). Each item says which tool does that today: a
Viaduct file tool, or Konnect's where Konnect already has one, since Viaduct does not
duplicate a released Konnect feature. Each item moves to a live tool once KiCad (or
kicad-python) supports it.

Checked by listing the API commands compiled into `_pcbnew.kiface` and `libkiapi.so`
(10.0.7), and by running each command against a hidden editor.

## Missing from the API

| Board Setup page | API status | File workaround |
|---|---|---|
| Constraints (minimum clearance, track width, annular width, via and hole sizes, copper to edge, hole to hole, silkscreen, zone fill, length tuning tolerances) | `BoardDesignRules` is an empty message with no command | Konnect `set_design_rules`; annular width and hole clearance minimums are [#776](https://github.com/mixelpixx/Konnect/issues/776) |
| Pre-defined sizes (track, via, diff pair lists) | none | Konnect `set_predefined_sizes` |
| Net class assignments and patterns | no field in `NetClass`, no command | Konnect `assign_net_to_class` |
| Removing a net class, unsetting a netclass field | needs replace-mode `SetNetClasses`, which hangs KiCad (below) | Viaduct `edit_netclass_file`; Konnect tracks removal in [#882](https://github.com/mixelpixx/Konnect/issues/882) |
| Physical stackup (thicknesses, dielectrics, εr, loss tangent, colors, finish, impedance control, edge connector, edge plating) | `UpdateBoardStackup` declared "not yet implemented", no handler in pcbnew | Viaduct `set_stackup`; Konnect tracks it in [#716](https://github.com/mixelpixx/Konnect/issues/716) |
| Board editor layers (user names, copper layer types) | no setter; `GetBoardLayerByName` exists in KiCad 10 but kicad-python 0.5 does not wrap it | Viaduct `set_layer_names` |
| Solder mask and paste, via tenting/covering/plugging/capping/filling | none | Viaduct `set_mask_and_paste` |
| Text and graphics defaults (incl. dimension and zone defaults for new items) | `GetGraphicsDefaults` reads line widths and text sizes only; no setter | Viaduct `set_project_settings(graphics_defaults=...)` |
| Violation severity per DRC check | none (`DrcSeverity` is only used by `InjectDrcError`) | Viaduct `set_project_settings(rule_severities=...)` |
| Teardrop defaults | per-item only | Viaduct `set_project_settings(teardrops=...)` |
| Length-tuning pattern defaults | none | Viaduct `set_project_settings(tuning_patterns=...)` |
| Custom rules (`.kicad_dru`) | none, including reload | Konnect `add_design_rule` for per-layer clearance and width; the rest is [#881](https://github.com/mixelpixx/Konnect/issues/881) |
| Per-layer zone defaults (`(setup (zone_defaults ...))`) | none | none yet |

Also missing from the API:
- Netclass via stacks take one size and drill for all layers (stated in the proto).
- Viaduct's `check_plan` takes `edge_clearance` and `hole_to_hole` as arguments, since it
  cannot read the board's constraints live.

## Bugs found while building the board setup tools

- **KiCad 10.0.7: `SetNetClasses` with `MMM_REPLACE` hangs pcbnew.** Once any net's effective
  netclass has been looked up (`GetNetClassForNets`, which every `check_plan` does), a
  replace-mode `SetNetClasses` that changes a value leaves pcbnew at full CPU and every
  later API call times out (waited 180 s). Merge mode is fine. Reproduce on a board whose
  nets are assigned to a non-Default class: `GetNetClassForNets`, then `SetNetClasses`
  (replace, all classes, one clearance changed), then `GetNetClassForNets` again. Worth
  reporting upstream; `set_net_class` only uses merge until it is fixed.
- **KiCad 10.0.7: effective netclasses go stale after `SetNetClasses`.**
  `GetNetClassForNets` keeps returning the old clearance until the board is saved; a zone
  refill does not refresh it. `set_net_class` tells the caller to `save_board` before
  `check_plan`. A refresh command, or rebuilding the cache inside `SetNetClasses`, would
  remove that step.
- **kicad-python 0.5: `Board.get_project()` breaks the board.** It passes the board's own
  `DocumentSpecifier` to `Project()`, which sets its type to `DOCTYPE_PROJECT` in place.
  Every later board call then fails with "the requested document … is not open".
  `kicad_io._project` passes a copy instead.
- **kicad-python 0.5: `NetClass.via_diameter` setter** writes `copper_layers[0]` without
  creating it, so it raises `IndexError` on a class with no via stack. Viaduct builds the
  proto directly.
- **kicad-python 0.5 does not wrap `SetNetClasses`** (the command and messages are there);
  Viaduct sends it directly.
- **Fixed in Viaduct: the hidden editor left KiCad's lock files behind.** pcbnew is stopped
  with a signal, so `close_headless` after `open_headless(copy=false)` left
  `~<name>.kicad_pcb.lck` and `~<name>.kicad_pro.lck` next to the user's files: KiCad then
  reported the board as locked and the file tools refused. `Headless.stop` now removes the
  locks that pcbnew created.
