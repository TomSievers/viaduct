"""A hidden PCB editor: KiCad's pcbnew where nobody sees it.

KiCad's API is only served by its GUI, and kicad-cli has no server mode, so "headless" means a
real pcbnew nobody can see:

- Linux: on a virtual X display (Xvfb, through xvfb-run).
- Windows: started with its windows hidden (STARTUPINFO SW_HIDE). Windows has no virtual
  display to hide it on; a window KiCad opens later in its own way could still show.

Its API socket lives under a short private temp dir (TMPDIR on Linux, TMP/TEMP on Windows):
that keeps it apart from the user's own KiCad (/tmp/kicad/api.sock, %TEMP%\\kicad\\api.sock)
and, on Linux, under the ~108 character limit for socket paths, past which KiCad silently
creates no socket at all.

KiCad 11 plans a built-in headless mode (KiCad 10 never will). Once it is released,
start() should check the installed KiCad version and use it on 11 or newer,
keeping this path for KiCad 10.
"""

from __future__ import annotations

import atexit
import glob
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

WINDOWS = os.name == "nt"
PROJECT_FILES = (".kicad_pcb", ".kicad_pro", ".kicad_prl")
TABLES = ("sym-lib-table", "fp-lib-table")


def lock_file(board: Path) -> Path:
    return board.with_name(f"~{board.name}.lck")


def hidden_env(tmpdir: Path, windows: bool = WINDOWS) -> dict:
    """The environment for the hidden pcbnew: its temp dir (where KiCad puts its API socket)
    is private. On Linux, xvfb-run only sets DISPLAY: in a Wayland session GTK would still
    connect to the user's compositor (WAYLAND_DISPLAY, GDK_BACKEND=wayland) and show the
    editor on their screen, so every route to Wayland is removed."""
    if windows:
        env = dict(os.environ, TMP=str(tmpdir), TEMP=str(tmpdir))
    else:
        env = dict(os.environ, TMPDIR=str(tmpdir), GDK_BACKEND="x11", XDG_SESSION_TYPE="x11")
    for var in ("KICAD_API_SOCKET", "WAYLAND_DISPLAY", "WAYLAND_SOCKET"):
        env.pop(var, None)
    return env


def find_pcbnew(windows: bool = WINDOWS) -> str | None:
    """VIADUCT_PCBNEW, pcbnew on PATH, or on Windows the newest KiCad under Program Files."""
    found = os.environ.get("VIADUCT_PCBNEW") or shutil.which("pcbnew")
    if found or not windows:
        return found
    # KiCad's installer: Program Files\KiCad\<version>\bin, or per user under LOCALAPPDATA\Programs
    roots = {os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432")} - {None}
    if os.environ.get("LOCALAPPDATA"):
        roots.add(os.path.join(os.environ["LOCALAPPDATA"], "Programs"))
    hits = [h for r in roots for h in glob.glob(os.path.join(r, "KiCad", "*", "bin", "pcbnew.exe"))]

    def version(path: str) -> tuple:
        name = Path(path).parent.parent.name
        return tuple(int(p) if p.isdigit() else 0 for p in name.split("."))
    return max(set(hits), key=version) if hits else None


def answers(socket: str, timeout_ms: int = 1000) -> bool:
    """Whether a KiCad API server answers on socket."""
    try:
        from kipy import KiCad
        KiCad(socket_path=socket, timeout_ms=timeout_ms).ping()
        return True
    except Exception:
        return False


class Headless:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.root: Path | None = None
        self.board: Path | None = None
        self.source: Path | None = None
        self.log: Path | None = None
        self._log_file = None
        self.locks: list[Path] = []
        atexit.register(self.stop)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def socket(self) -> str | None:
        if not self.root:
            return None
        return f"ipc://{self.root}\\t\\kicad\\api.sock" if WINDOWS else f"ipc://{self.root}/t/kicad/api.sock"

    def _command(self, pcbnew: str) -> tuple[list[str], dict]:
        """How to start pcbnew hidden on this platform: the command and Popen options."""
        if WINDOWS:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 0                      # SW_HIDE
            return [pcbnew, str(self.board)], {"startupinfo": info,
                                                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        xvfb = os.environ.get("VIADUCT_XVFB_RUN") or shutil.which("xvfb-run")
        if not xvfb:
            raise RuntimeError("need xvfb-run on PATH (or VIADUCT_XVFB_RUN): it gives the hidden editor a "
                               "virtual display. Install it with your distribution's xvfb package")
        return [xvfb, "-a", "-s", "-screen 0 1920x1080x24", pcbnew, str(self.board)], {"start_new_session": True}

    def _socket_up(self) -> bool:
        if WINDOWS:   # a named pipe: there is no file to wait for
            return answers(self.socket)
        return (self.root / "t" / "kicad" / "api.sock").exists()

    def start(self, board: str, copy: bool = True, timeout: float = 60.0) -> dict:
        if self.running:
            raise RuntimeError(f"a headless editor is already open on {self.board}; close it first")
        src = Path(board).expanduser().resolve()
        if src.suffix != ".kicad_pcb" or not src.exists():
            raise ValueError(f"not a .kicad_pcb file: {src}")
        if not copy and lock_file(src).exists():
            raise RuntimeError(f"{src.name} is open in KiCad ({lock_file(src).name} exists). Close it there, "
                               "or open a copy (copy=true): two editors on one file would fight over it, and "
                               "the hidden one would hang on an invisible 'file is locked' dialog")
        pcbnew = find_pcbnew()
        if not pcbnew:
            raise RuntimeError("pcbnew not found: put KiCad's bin directory on PATH or set VIADUCT_PCBNEW to "
                               "the pcbnew executable")

        # /tmp, not $TMPDIR, on Linux: the socket path must stay short
        self.root = Path(tempfile.mkdtemp(prefix="vd", dir=None if WINDOWS else "/tmp"))
        (self.root / "t").mkdir()
        if copy:
            proj = self.root / "p"
            proj.mkdir()
            for f in src.parent.iterdir():
                if f.name in TABLES or (f.stem == src.stem and f.suffix in PROJECT_FILES):
                    shutil.copy2(f, proj / f.name)
            self.board = proj / src.name
        else:
            self.board = src
        self.source = src
        # pcbnew is stopped by force and leaves its lock files behind: remember the ones it
        # will create, so stop() can remove them and KiCad does not later report the file as locked
        self.locks = [lk for lk in (lock_file(self.board), lock_file(self.board.with_suffix(".kicad_pro")))
                      if not lk.exists()]
        self.log = self.root / "pcbnew.log"
        try:
            cmd, options = self._command(pcbnew)
        except RuntimeError:
            self.stop()
            raise
        self._log_file = self.log.open("w")
        self.proc = subprocess.Popen(cmd, env=hidden_env(self.root / "t"), stdout=self._log_file,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._socket_up():
                return {"socket": self.socket, "board": str(self.board), "copy_of": str(src) if copy else None,
                        "seconds": round(timeout - (deadline - time.monotonic()), 1)}
            if self.proc.poll() is not None:
                break
            time.sleep(0.5)
        tail = self.log.read_text(errors="replace")[-1500:] if self.log.exists() else ""
        self.stop()
        raise RuntimeError(f"hidden pcbnew did not open its API socket within {timeout:.0f}s "
                           f"(a dialog may be blocking it). Log tail:\n{tail}")

    def _kill(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            if WINDOWS:              # pcbnew is the direct child: no process group to signal
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=10)
                return
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def stop(self, keep_files: bool = False) -> dict:
        """Stop the hidden editor. With keep_files the private copy stays on disk."""
        board, root = self.board, self.root
        self._kill()
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        for lk in self.locks:
            lk.unlink(missing_ok=True)
        self.locks = []
        if root and not keep_files:
            # Windows keeps a file locked while a process still holds it: give it a moment
            for _ in range(10 if WINDOWS else 1):
                shutil.rmtree(root, ignore_errors=True)
                if not root.exists():
                    break
                time.sleep(0.5)
        copied = bool(root and board and root in board.parents)
        self.proc = self.root = self.board = self.source = self.log = None
        return {"stopped": True,
                "board": str(board) if board and (keep_files or not copied) else None,
                "files_removed": bool(root) and not keep_files}

    def status(self) -> dict:
        return {"running": self.running, "board": str(self.board) if self.board else None,
                "copy_of": str(self.source) if self.source and self.source != self.board else None,
                "socket": self.socket if self.running else None}
