# Viaduct and Konnect

Viaduct does not duplicate a released Konnect feature. Each Viaduct tool falls in one of
three groups, and this page says which, with the Konnect issue where there is one. Checked
against Konnect's open issues and pull requests on 2026-10-25.

## Stopgaps for planned Konnect features

These fill gaps Konnect has open issues for. Each is removed once a Konnect release ships
the capability:

| Viaduct capability | Konnect issue |
|---|---|
| Batch copper edits in one commit, via deletion (`apply_plan`) | [#825](https://github.com/mixelpixx/Konnect/issues/825), [#500](https://github.com/mixelpixx/Konnect/issues/500) |
| Zone deletion (`apply_plan` with zone ids) | [#431](https://github.com/mixelpixx/Konnect/issues/431) |
| Live copper-layer expansion (`set_copper_layers`) | [#721](https://github.com/mixelpixx/Konnect/issues/721) |
| Netclass edits on a board open in KiCad (`set_net_class`) | Konnect's `create_netclass` edits `.kicad_pro` and refuses while KiCad holds the project ([#791](https://github.com/mixelpixx/Konnect/issues/791), [#817](https://github.com/mixelpixx/Konnect/issues/817)); writing through `SetNetClasses` is named as a follow-up there but not filed. Diff pair fields: [#777](https://github.com/mixelpixx/Konnect/issues/777) |
| Netclass removal, unsetting netclass fields (`edit_netclass_file`) | [#882](https://github.com/mixelpixx/Konnect/issues/882) |
| Setting the physical stackup (`set_stackup`) | [#716](https://github.com/mixelpixx/Konnect/issues/716) |
| Moving a footprint's Reference/Value text (`move_footprint_text`) | [#829](https://github.com/mixelpixx/Konnect/issues/829) |

## Out of scope for Konnect

Konnect's architecture ([#590](https://github.com/mixelpixx/Konnect/issues/590)) exposes
KiCad's own capabilities and an accepted Freerouting round trip; it "does not become a second
router". It also retired its own automatic placement planners in favour of AI-directed use of
small native operations ([#602](https://github.com/mixelpixx/Konnect/issues/602)). Planners
that own the layout decisions therefore stay in Viaduct:

- **Plane fanout** (`plan_fanout`). No Konnect issue or PR asks for it. Its result is plain
  tracks and vias, which Konnect's planned batch edits (#825, #500) could apply. Its
  multilayer autorouting ([#449](https://github.com/mixelpixx/Konnect/issues/449)) would then
  route the signals; it has no fanout step of its own.
- **Automatic reference placement** (`place_references`). On #829 the maintainer limits
  Konnect's tool to editing a given field, "not automatic silk placement". Once #829 ships,
  the moves could be applied through Konnect's tool instead. `sizes` should stay at or above
  the board's minimum text height, which Konnect's `set_design_rules` gains in
  [#965](https://github.com/mixelpixx/Konnect/pull/965) ([#960](https://github.com/mixelpixx/Konnect/issues/960)).
- **Checked plans, connectivity and the hidden editor** (`check_plan`, `net_islands`,
  `open_headless`): Konnect-owned logic by the same rule.

## No Konnect counterpart or issue

Text variables, origins, layer names, solder mask and paste, via protection, violation
severities, and teardrop, length-tuning and graphics defaults.

Schematic readability (`check_readability`) is tried out here: it only reads the schematic
files and edits nothing, so it stays clear of Konnect's schematic tools. Konnect's issues
were not yet searched for a counterpart; if it fits Konnect, it moves there once the rules
have proven useful.

## Left to Konnect

- **Removed because a Konnect release covers them:** reading net classes and the physical
  stackup (`get_netclasses` and `get_board_stackup`, Konnect v0.13.0).
- **Left to Konnect's file writers and not duplicated:** constraints (`set_design_rules`),
  predefined sizes (`set_predefined_sizes`), creating netclasses and assigning nets
  (`create_netclass`, `assign_net_to_class`) and custom rules (`add_design_rule`).
- **Never duplicated:** schematic work, placement, DRC and exports.
