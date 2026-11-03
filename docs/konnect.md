# Viaduct and Konnect

Viaduct works next to [Konnect v0.13.0](https://github.com/mixelpixx/Konnect/releases/tag/v0.13.0).
What that version is missing gets built here, and stays here. Viaduct does not try to avoid
duplicating Konnect, and its tools are not stopgaps to remove when a Konnect release catches up.

## Why

[mixelpixx/Konnect](https://github.com/mixelpixx/Konnect) is archived and read-only, so v0.13.0 is
its last release, under the AGPL-3.0. Development continues as the closed-source
[Konnect-EDA](https://github.com/Orchis-ai-LLC/Konnect-EDA), free until 1.0 and paid for businesses
after that. Viaduct is not tied to what a closed, commercial product may or may not ship. The state
below was checked on 2026-11-03; Konnect-EDA had no release yet.

## What Viaduct adds to v0.13.0

The issue links record that the gap was known. They are on the archived tracker, so they will
not change, and Konnect-EDA's tracker did not take them over.

| Viaduct capability | In Konnect v0.13.0 |
|---|---|
| Batch copper edits in one commit, via deletion (`apply_plan`) | Missing ([#825](https://github.com/mixelpixx/Konnect/issues/825), [#500](https://github.com/mixelpixx/Konnect/issues/500)) |
| Zone deletion (`apply_plan` with zone ids) | Missing ([#431](https://github.com/mixelpixx/Konnect/issues/431)) |
| Live copper-layer expansion (`set_copper_layers`) | Missing ([#721](https://github.com/mixelpixx/Konnect/issues/721)) |
| Netclass edits on a board open in KiCad (`set_net_class`) | `create_netclass` only edits `.kicad_pro`. Its refusal while KiCad holds the project landed after v0.13.0 ([#791](https://github.com/mixelpixx/Konnect/issues/791), [#805](https://github.com/mixelpixx/Konnect/pull/805)). Diff pair fields are missing ([#777](https://github.com/mixelpixx/Konnect/issues/777)) |
| Netclass removal, unsetting netclass fields (`edit_netclass_file`) | Missing ([#882](https://github.com/mixelpixx/Konnect/issues/882)) |
| Setting the physical stackup (`set_stackup`) | Missing ([#716](https://github.com/mixelpixx/Konnect/issues/716)) |
| Moving a footprint's Reference/Value text (`move_footprint_text`) | Missing ([#829](https://github.com/mixelpixx/Konnect/issues/829)) |
| Plane fanout (`plan_fanout`) | `plan_bga_fanout` only covers BGAs |
| Automatic reference placement (`place_references`) | Missing, and ruled out on #829 |
| Checked plans, connectivity and the hidden editor (`check_plan`, `net_islands`, `net_summary`, `open_headless`) | Missing |
| Zone thermal gap and spoke width (plans, `set_zone_properties`) | `add_zone` sets `pad_connection` only |
| Pre-route errors (`check_routable`) | Missing. `run_drc` and `get_drc_violations` judge copper that exists, `check_clearance` measures footprint spacing, and `score_placement` scores placement |
| Schematic readability (`check_readability`) | Not compared; it only reads the schematic files |
| Text variables, origins, layer names, solder mask and paste, via protection, violation severities, and teardrop, length-tuning and graphics defaults | Missing |

## Known limits of v0.13.0

- **Autorouting:** the Freerouting round trip takes two copper layers only
  ([#449](https://github.com/mixelpixx/Konnect/issues/449)), and refuses unlocked routing and arcs,
  so it cannot route a four-layer board after a fanout.
- **Minimum text height:** `set_design_rules` cannot set it
  ([#960](https://github.com/mixelpixx/Konnect/issues/960)), so keep the `sizes` of
  `place_references` at or above the board's minimum yourself.

## Used from Konnect today

Viaduct has no tool of its own for these yet, which is no rule against adding one: reading net
classes and the stackup (`get_netclasses`, `get_board_stackup`), constraints (`set_design_rules`),
predefined sizes (`set_predefined_sizes`), creating netclasses and assigning nets (`create_netclass`,
`assign_net_to_class`), custom rules (`add_design_rule`), schematic work, placement, DRC and exports.
