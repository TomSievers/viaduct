# Headless editing

KiCad's API is only served by its GUI, and `kicad-cli` has no server mode. `open_headless`
therefore runs a real `pcbnew` where nobody sees it. Until `close_headless`, every tool
targets the hidden board, and Konnect reaches it too.

## Which board and socket

Each KiCad process serves one API socket with one board. The first process gets the default
socket (`/tmp/kicad/api.sock`, `%TEMP%\kicad\api.sock`); later ones get `api-<pid>.sock`
next to it, which clients don't look for. So `open_headless` depends on what is running:

| When opening | What happens | Konnect reaches it |
|---|---|---|
| KiCad has this board open | Uses it, no hidden editor (`already_open`) | Yes |
| No KiCad running | Hidden editor on the real board, on the default socket | Yes |
| KiCad open on another board, or with none | Refused: open the board in that KiCad, close it, or use a copy | – |
| Board locked, but nothing answers for it | Refused: a hidden editor would hang on an invisible "file is locked" dialog | – |
| `copy=true` | Hidden editor on a private copy and socket; KiCad can stay open | **No** |

For a hidden editor on the real board:

- **Konnect needs its socket set explicitly**, because it looks for a socket only when it starts
  and stays unconfigured if KiCad wasn't running then. Set `KICAD_API_SOCKET=ipc:///tmp/kicad/api.sock`
  on its launch command, or `ipc_address` in its config; it then connects on every call.
- **Pass Konnect the `board` path `open_headless` returns.** For any other path Konnect edits that
  file on disk instead, with only a warning.
- **`close_headless(save=false)` discards unsaved edits**, Konnect's included.
- **A KiCad you open meanwhile can't get the default socket.** You get its lock warning if it
  opens the same board. It stays out of reach of Viaduct and Konnect until it is restarted, even
  after the hidden editor closes; `close_headless` warns about this.

Typical flow: `open_headless`, work with Viaduct and Konnect, `close_headless(save=true)`.

- **Linux:** on a virtual X display (`xvfb-run`). Needs a native KiCad; the Flatpak is not
  supported yet, because it keeps its own `/tmp`.
- **Windows (experimental, not yet tested on Windows):** `pcbnew.exe` is started with its
  windows hidden (`SW_HIDE`). There is no virtual display, so a window KiCad opens on its own could still show.
  `pcbnew.exe` is found under `Program Files\KiCad\<version>\bin` or set with `VIADUCT_PCBNEW`.

- `copy=true` works on a **private copy** of the project (board, project file, library tables),
  so the user's KiCad can stay open on the original. The copy has no schematic or custom rules.
- On Linux it never reaches the user's screen: in a Wayland session GTK would otherwise ignore
  Xvfb's `DISPLAY` and open the editor on the user's compositor, so `WAYLAND_DISPLAY` is
  removed and `GDK_BACKEND=x11` is set.
- A copy's socket lives under a short private temp dir (`/tmp/vdXXXX/t/kicad/api.sock`), under
  the ~108 character socket path limit, past which KiCad silently opens no socket. On Windows it
  is a named pipe under a private `%TEMP%`.
- `open_headless` waits until the board answers (KiCad reports "not ready" while loading),
  and gives up with the editor's log if a dialog blocks it. On the default socket it asks which
  board is open rather than trust the socket file: a killed KiCad leaves that file behind (the
  next KiCad takes it over), and an editor stuck on a dialog has one but doesn't answer.
- `close_headless(save=true)` saves, and for a copy keeps it on disk and returns its path. pcbnew
  is stopped by force, so the lock files it created and `xvfb-run`'s temp files are removed for it.

To try a plan while KiCad stays open: plan and verify on a hidden copy, then apply the same plan
(with a fresh `check_plan` token) to the user's board in one commit.

Needs `pcbnew` (on PATH, under Program Files on Windows, or `VIADUCT_PCBNEW`) and on Linux
`xvfb-run` (the Nix package adds it; otherwise install Xvfb or set `VIADUCT_XVFB_RUN`).
Installing: [install.md](install.md).

## KiCad 11

KiCad 11 plans a built-in headless mode; KiCad 10 will never get one, which is why
Viaduct runs the GUI on Xvfb today. Once KiCad 11 is released, `open_headless` will
check the installed KiCad version and use the built-in headless mode on 11 or newer,
keeping the Xvfb method for KiCad 10. The tools' behaviour stays the same either way.
