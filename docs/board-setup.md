# Board setup

Board Setup is split by what KiCad's API can reach: some settings change live on the open
board, the rest are written into the files while the project is closed. What the API lacks
is listed in [TODO.md](../TODO.md).

## On the open board

`get_board_setup` returns the board setup Konnect does not read, plus a `not_in_api` list of
the pages KiCad's API cannot reach (constraints, predefined sizes, netclass assignments,
custom rules and more). Read net classes and the stackup with Konnect's `get_netclasses` and
`get_board_stackup`.

`set_net_class` changes only the fields it is given and reads the class back. KiCad keeps
each net's effective rules until the board is saved, so call `save_board` before `check_plan`
relies on new clearances. Netclass changes are project settings outside the undo history.
The API cannot unset a netclass field, remove a class or assign nets to one: those stay in
`edit_netclass_file` and Konnect's `assign_net_to_class`, which edit the project file while
the project is closed in KiCad.

`set_text_variables`, `set_board_origin` and `set_copper_layers` (adds layers, never removes
them; needs `confirm`) also work live.

## In the files

The (file) tools write the remaining settings into the `.kicad_pcb` and `.kicad_pro`
instead, as Konnect does for its own file writers. They cover the stackup, layer names and
types, solder mask and paste, via protection, violation severities, and teardrop,
length-tuning and graphics defaults, and they can remove a netclass.

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
