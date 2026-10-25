# Headless editing

KiCad's API is only served by its GUI, and `kicad-cli` has no server mode. `open_headless`
therefore runs a real `pcbnew` where nobody sees it. Until `close_headless`, every tool
targets the hidden board.

- **Linux:** on a virtual X display (`xvfb-run`). Needs a native KiCad; the Flatpak is not
  supported yet, because it keeps its own `/tmp`.
- **Windows (experimental, not yet tested on Windows):** `pcbnew.exe` is started with its
  windows hidden (`SW_HIDE`), with `TMP`/`TEMP` pointing at a private dir so its API pipe is
  its own. There is no virtual display, so a window KiCad opens on its own could still show.
  `pcbnew.exe` is found under `Program Files\KiCad\<version>\bin` or set with `VIADUCT_PCBNEW`.

- By default it works on a **private copy** of the project (board, project file, library
  tables), so the user's KiCad can stay open on the original. `copy=false` opens the file
  itself and is refused while KiCad has it open: a second editor would hang on an
  invisible "file is locked" dialog.
- On Linux it never reaches the user's screen: in a Wayland session GTK would otherwise ignore
  Xvfb's `DISPLAY` and open the editor on the user's compositor, so `WAYLAND_DISPLAY` is
  removed and `GDK_BACKEND=x11` is set.
- Its socket lives under a short private temp dir (`/tmp/vdXXXX/t/kicad/api.sock`), apart
  from the user's `/tmp/kicad/api.sock` and under the ~108 character socket path limit,
  past which KiCad silently opens no socket. On Windows it is a named pipe under the private
  `%TEMP%`, so `open_headless` waits for it to answer instead of for a file.
- `open_headless` waits until the board answers (KiCad reports "not ready" while loading),
  and gives up with the editor's log if a dialog blocks it.
- `close_headless(save=true)` keeps a saved copy on disk and returns its path. pcbnew is
  stopped by force, so the lock files it created are removed for it.

Typical flow: plan and verify on a hidden copy, then apply the same plan (with a fresh
`check_plan` token) to the user's board in one commit.

Needs `pcbnew` (on PATH, under Program Files on Windows, or `VIADUCT_PCBNEW`) and on Linux
`xvfb-run` (the Nix package adds it; otherwise install Xvfb or set `VIADUCT_XVFB_RUN`).
Installing: [install.md](install.md).

## KiCad 11

KiCad 11 plans a built-in headless mode; KiCad 10 will never get one, which is why
Viaduct runs the GUI on Xvfb today. Once KiCad 11 is released, `open_headless` will
check the installed KiCad version and use the built-in headless mode on 11 or newer,
keeping the Xvfb method for KiCad 10. The tools' behaviour stays the same either way.
