# Installing Viaduct

Viaduct is a Python package with one command, `viaduct`, which Claude Code starts as an MCP
server. It runs on Linux and Windows; on both, the tools work on the board open in your own
KiCad. The hidden editor (`open_headless`) is the one part that differs per platform; see
[The hidden editor per platform](#the-hidden-editor-per-platform).

## What you need

- **KiCad 10** (tested with 10.0.7), with the API switched on: **Preferences → Preferences →
  Plugins → Enable KiCad API** (restart KiCad if Viaduct can't connect afterwards). The file
  tools only edit KiCad 10 files; KiCad 9.0.5 or newer may work for the live tools but is
  untested.
- **Python 3.11 or newer** (tested with 3.12 and 3.14).
- **Claude Code**, for the `claude mcp add` step.
- **git**, to get the code and to update it.

Install from a git checkout, as an editable install (`-e`): `reload_server` then loads new
code straight from that checkout, without reinstalling.

## Linux

```sh
git clone https://github.com/TomSievers/viaduct.git ~/viaduct
cd ~/viaduct
python3 -m venv .venv
.venv/bin/pip install -e .
claude mcp add --scope user viaduct -e VIADUCT_SOURCE=$HOME/viaduct -- $HOME/viaduct/.venv/bin/viaduct
```

For the hidden editor, also install Xvfb (Debian and Ubuntu: `sudo apt install xvfb`; Fedora:
`sudo dnf install xorg-x11-server-Xvfb`) and use a KiCad whose `pcbnew` is on PATH
(distribution package or the KiCad PPA, not the Flatpak; see below).

### With Nix

```sh
nix build                     # result/bin/viaduct, runs the tests during the build
claude mcp add --scope user viaduct -e VIADUCT_SOURCE=$PWD -- $PWD/result/bin/viaduct
```

The Nix package brings `xvfb-run` along. `reload_server` rebuilds with `nix build` first.

## Windows

In PowerShell, with Python from python.org or `winget install Python.Python.3.12`:

```powershell
git clone https://github.com/TomSievers/viaduct.git $HOME\viaduct
cd $HOME\viaduct
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e .
claude mcp add --scope user viaduct -e VIADUCT_SOURCE=$HOME\viaduct -- $HOME\viaduct\.venv\Scripts\viaduct.exe
```

KiCad's own installer is all the hidden editor needs: `pcbnew.exe` is found under
`Program Files\KiCad\<version>\bin` (the newest version wins), or set `VIADUCT_PCBNEW` to it.

## Check that it works

Open a board in KiCad's PCB editor, start Claude Code and ask it to run Viaduct's `status`
tool. It should report the KiCad version, the board and its item counts. `claude mcp list`
shows whether Claude Code started the server at all.

## Updating

```sh
git pull
```

Then ask Claude to run `reload_server`: the tools are reloaded from the checkout and appear
in the running session, no restart needed. After a change to the dependencies in
`pyproject.toml`, run `pip install -e .` again and restart Claude Code.

## The hidden editor per platform

KiCad only serves its API from its GUI, so `open_headless` runs a real `pcbnew` where you
don't see it ([details](headless.md)):

| Platform | How | Status |
|---|---|---|
| Linux, native KiCad | `pcbnew` on a virtual display (`xvfb-run`) | Supported, tested |
| Linux, Flatpak KiCad | – | Not supported yet (untested): the Flatpak keeps its own `/tmp`, where the hidden editor's private socket would be out of reach. Live tools work with the Flatpak. |
| Windows | `pcbnew.exe` started with its windows hidden | Experimental, not yet tested on Windows |

On Windows there is no virtual display, so a window KiCad opens on its own could still
appear. If it does, or `open_headless` times out, please report it, with the log tail the
error message includes.

## Settings

| Variable | What it does |
|---|---|
| `VIADUCT_SOURCE` | The checkout `reload_server` loads new code from |
| `KICAD_API_SOCKET` | KiCad's API socket, if it is not in the default place (`/tmp/kicad/api.sock`, `%TEMP%\kicad\api.sock`, or the Flatpak's) |
| `VIADUCT_PCBNEW` | The `pcbnew` executable for the hidden editor |
| `VIADUCT_XVFB_RUN` | The `xvfb-run` executable for the hidden editor (Linux) |

Pass them to the server with `-e NAME=value` on `claude mcp add`.

## Troubleshooting

- **"Failed to connect to KiCad"**: KiCad isn't running, has no board open in the PCB editor,
  or the API is off (see above, and restart KiCad after switching it on).
- **`claude mcp list` shows viaduct as failed**: run the `viaduct` command from the line you
  gave `claude mcp add` in a terminal; it prints the Python error. An `ImportError` usually
  means the install step was run with a different Python than the venv's.
- **`open_headless` times out**: most likely a dialog is waiting in the hidden editor, such
  as a question KiCad asks on its first start. Open the same board once in your normal KiCad
  and answer any question there, then try again; the error includes the editor's log tail.
