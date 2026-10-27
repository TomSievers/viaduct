# Checking a KiCad bug against upstream

Before a KiCad bug from [TODO.md](../TODO.md) is reported, check that it still happens on
KiCad's newest code. `kicad-upstream/` is a separate flake that builds KiCad from source at
the head of two branches, since a fix lands on master first and is then backported:

| Package | Branch | What it tells |
|---|---|---|
| `kicad-master` (default) | master | Fixed for the next major release |
| `kicad-10` | 10.0 | Fixed, or backported, for the next 10.0.x |

It is its own flake so that Viaduct's build never fetches KiCad's sources, and so that moving
KiCad forward does not move Viaduct's pins. Its nixpkgs is locked to the same revision as
Viaduct's, so both use the same KiCad recipe and libraries. The recipe is nixpkgs'
`kicad-unstable` with the source swapped: symbol and footprint libraries are the ones nixpkgs
pins for it, and the 3D models are left out.

```sh
nix run ./kicad-upstream#revisions                 # which commits the lock builds
nix flake update --flake ./kicad-upstream          # move both branches to their current head
nix build ./kicad-upstream#kicad-10 -o result-kicad-10    # or #kicad-master
```

Each build compiles KiCad (the rest comes from the binary cache), which takes a while. The
package is named after the branch and commit, such as `kicad-10.0-f4fb7776ff`, and KiCad's
own version string carries the commit too (`Help → About`, `kicad-cli version`): KiCad's
untagged commits read like `10.99.0-unknown`, and the recipe puts the commit in place of
`unknown`. A release candidate on the 10.0 branch (`10.0.8-rc1`) has no `unknown`, so the
flake adds it first.

## When an update breaks the build

nixpkgs' recipe is written for the commit nixpkgs pins, and the branches move on. Each
package gets its adjustments in `kicadFrom`'s last argument, applied to the KiCad build itself:

- `patches`: a nixpkgs patch that no longer applies, by file name, mapped to a replacement in
  this directory. Master needs `runtime_stock_data_path-master.patch`: it reworked
  `PATHS::GetStockDataPath`, so the same `NIX_KICAD10_STOCK_DATA_PATH` override goes at its
  new place.
- `nativeBuildInputs`, `buildInputs`: tools and libraries a branch now needs. Master's new
  rendering layer (`common/rhi`) compiles Vulkan shaders at build time (glslang,
  spirv-tools) and builds its Vulkan backend from the Vulkan headers.

The build log names the failing step; `nix log` on the `kicad-base` derivation shows it in
full. Master has also dropped the legacy Python scripting (SWIG and wxPython); the recipe still
sets `KICAD_SCRIPTING_WXPYTHON`, which master ignores.

## Running Viaduct against it

The hidden editor starts `VIADUCT_PCBNEW`, or `pcbnew` on PATH, so a reproduction can run on
the upstream build without touching the installed KiCad:

```sh
VIADUCT_PCBNEW=$PWD/result-kicad-10/bin/pcbnew nix develop -c sh -c 'PYTHONPATH=src python repro.py'
```

where `repro.py` opens a copy of a board with `viaduct.headless.Headless` and talks to it
through `kicad_io.Session`, as the server does. For the MCP server, register it with the same
variable (`claude mcp add ... -e VIADUCT_PCBNEW=...`) and use `open_headless`.

kicad-python follows KiCad's API releases; if master has changed the API protocol, use the
kicad-python that master's `api/proto` matches, or send the commands directly as Viaduct does
for `SetNetClasses`.
