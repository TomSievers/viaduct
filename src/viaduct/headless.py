"""A hidden PCB editor: KiCad's pcbnew where nobody sees it.

KiCad's API is only served by its GUI, and kicad-cli has no server mode, so "headless" means a
real pcbnew nobody can see:

- Linux: on a virtual X display (Xvfb, through xvfb-run).
- Windows: started with its windows hidden (STARTUPINFO SW_HIDE). Windows has no virtual
  display to hide it on; a window KiCad opens later in its own way could still show.

Each KiCad process serves one API socket: the first takes KiCad's default (/tmp/kicad/api.sock,
%TEMP%\\kicad\\api.sock), later ones api-<pid>.sock next to it, which nothing finds on its own.
By default the hidden editor opens the real board on the default socket, the only one Konnect
and other clients can find. That needs the socket free, so with a KiCad already open on another
board the editor is refused rather than started out of reach.

copy=True is the exception: a private copy on a private socket, kept apart from the user's KiCad.
That socket lives under a short temp dir (TMPDIR on Linux, TMP/TEMP on Windows), under the
~108 character limit for socket paths on Linux, past which KiCad silently creates no socket.

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
STOP_TIMEOUT = 10.0     # seconds a hidden editor gets to exit, before SIGKILL and again after it


def lock_file(board: Path) -> Path:
    return board.with_name(f"~{board.name}.lck")


def default_socket(windows: bool = WINDOWS) -> str:
    """KiCad's default API socket: where the first KiCad process listens."""
    return f"ipc://{tempfile.gettempdir()}\\kicad\\api.sock" if windows else "ipc:///tmp/kicad/api.sock"


def hidden_env(tmpdir: Path | None = None, windows: bool = WINDOWS) -> dict:
    """The environment for the hidden pcbnew. Its temp dir decides where KiCad puts its API
    socket: tmpdir if given (a private socket), otherwise KiCad's default socket, whatever
    TMPDIR this server happens to run with. On Linux, xvfb-run only sets DISPLAY: in a Wayland
    session GTK would still connect to the user's compositor (WAYLAND_DISPLAY,
    GDK_BACKEND=wayland) and show the editor on their screen, so every route to Wayland is
    removed."""
    if windows:
        env = dict(os.environ)
        if tmpdir:
            env.update(TMP=str(tmpdir), TEMP=str(tmpdir))
    else:
        env = dict(os.environ, TMPDIR=str(tmpdir or "/tmp"), GDK_BACKEND="x11", XDG_SESSION_TYPE="x11")
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


def open_boards(socket: str | None, timeout_ms: int = 1000) -> list[Path] | None:
    """The boards open on a socket (None = where kipy looks by default): None if nothing answers
    there, an empty list if a KiCad answers without a board, or while it is still loading one."""
    try:
        from kipy import KiCad
        from kipy.proto.common.types.base_types_pb2 import DOCTYPE_PCB
        kicad = KiCad(socket_path=socket, timeout_ms=timeout_ms)
        kicad.ping()
    except Exception:
        return None
    try:
        docs = kicad.get_open_documents(DOCTYPE_PCB)
    except Exception:
        return []
    return [(Path(d.project.path) / d.board_filename).resolve() for d in docs]


def pid_sockets(windows: bool = WINDOWS) -> set[Path]:
    """The api-<pid>.sock files of KiCad processes that did not get the default socket. On
    Windows they are named pipes, which leave no file to find."""
    if windows:
        return set()
    return {Path(p) for p in glob.glob(default_socket(windows)[len("ipc://"):].replace("api.sock", "api-*.sock"))}


def stranded_editors(windows: bool = WINDOWS) -> dict[str, list[str]]:
    """KiCad processes answering on an api-<pid>.sock: out of reach of every client that uses
    the default socket. Sockets of dead processes stay behind as files and do not answer."""
    found = {}
    for sock in sorted(pid_sockets(windows)):
        boards = open_boards(f"ipc://{sock}", timeout_ms=300)
        if boards is not None:
            found[str(sock)] = [str(b) for b in boards]
    return found


def group_gone(pgid: int, timeout: float) -> bool:
    """Wait for every process of a group to be gone. A process its parent has yet to reap still
    counts, so a system that never reaps orphans costs the whole timeout, but nothing worse."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class Headless:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.root: Path | None = None
        self.board: Path | None = None
        self.source: Path | None = None
        self.log: Path | None = None
        self._log_file = None
        self.locks: list[Path] = []
        self.own_sockets: list[Path] = []
        atexit.register(self.stop)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def is_copy(self) -> bool:
        return bool(self.root and self.board and self.root in self.board.parents)

    @property
    def socket(self) -> str | None:
        if not self.root:
            return None
        if not self.is_copy:
            return default_socket()
        return f"ipc://{self.root}\\t\\kicad\\api.sock" if WINDOWS else f"ipc://{self.root}/t/kicad/api.sock"

    def _command(self, pcbnew: str, env: dict) -> tuple[list[str], dict]:
        """How to start pcbnew hidden on this platform: the command and Popen options. On Linux
        env is the editor's: this moves its TMPDIR, which decides where its socket goes, onto the
        command, and points xvfb-run's at root. xvfb-run only removes its temp dir if it is still
        running after its command exits, and stopping the group ends it first, so stop() removes
        that dir with root."""
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
        editor = ["env", f"TMPDIR={env['TMPDIR']}", pcbnew, str(self.board)]
        env["TMPDIR"] = str(self.root)
        return [xvfb, "-a", "-s", "-screen 0 1920x1080x24", *editor], {"start_new_session": True}

    def _socket_up(self) -> bool:
        if not self.is_copy:
            # a file proves nothing here: a killed KiCad leaves its socket behind, and one stuck
            # on a dialog has a socket but does not answer
            return self.board in (open_boards(default_socket()) or [])
        if WINDOWS:   # a named pipe: there is no file to wait for
            return answers(self.socket)
        return (self.root / "t" / "kicad" / "api.sock").exists()

    def _refuse_taken_socket(self) -> None:
        """Refuse when the hidden editor would not get the default socket."""
        configured = os.environ.get("KICAD_API_SOCKET")
        if configured and configured != default_socket():
            raise RuntimeError(f"KICAD_API_SOCKET is {configured}, but a hidden editor can only listen on "
                               f"{default_socket()}. Unset it, or open a copy (copy=true)")
        boards = open_boards(default_socket())
        if boards is not None:
            held = ", ".join(str(b) for b in boards) or "no board in its PCB editor"
            raise RuntimeError(f"another KiCad holds the default API socket ({held}). A hidden editor would be out "
                               "of reach of Konnect. Open the board in that KiCad, close it, or open a copy "
                               "(copy=true), which only Viaduct can reach")

    def start(self, board: str, copy: bool = False, timeout: float = 60.0) -> dict:
        if self.running:
            raise RuntimeError(f"a headless editor is already open on {self.board}; close it first")
        src = Path(board).expanduser().resolve()
        if src.suffix != ".kicad_pcb" or not src.exists():
            raise ValueError(f"not a .kicad_pcb file: {src}")
        if not copy:
            if src in (open_boards(None) or []):
                socket = os.environ.get("KICAD_API_SOCKET") or default_socket()
                return {"already_open": True, "socket": socket, "board": str(src), "copy_of": None,
                        "konnect_reachable": socket == default_socket()}
            self._refuse_taken_socket()
            if lock_file(src).exists():
                raise RuntimeError(f"{src.name} is open in KiCad ({lock_file(src).name} exists) but not on the "
                                   "default API socket. Close it there, or open a copy (copy=true): two editors "
                                   "on one file would fight over it, and the hidden one would hang on an "
                                   "invisible 'file is locked' dialog. A lock left by a crash can be deleted")
        pcbnew = find_pcbnew()
        if not pcbnew:
            raise RuntimeError("pcbnew not found: put KiCad's bin directory on PATH or set VIADUCT_PCBNEW to "
                               "the pcbnew executable")

        # /tmp, not $TMPDIR, on Linux: the socket path must stay short
        self.root = Path(tempfile.mkdtemp(prefix="vd", dir=None if WINDOWS else "/tmp"))
        if copy:
            (self.root / "t").mkdir()
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
        env = hidden_env(self.root / "t" if copy else None)
        try:
            cmd, options = self._command(pcbnew, env)
        except RuntimeError:
            self.stop()
            raise
        self._log_file = self.log.open("w")
        sockets_before = pid_sockets()
        self.proc = subprocess.Popen(cmd, env=env, stdout=self._log_file,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._socket_up():
                return {"socket": self.socket, "board": str(self.board), "copy_of": str(src) if copy else None,
                        "konnect_reachable": not copy,
                        "seconds": round(timeout - (deadline - time.monotonic()), 1)}
            if self.proc.poll() is not None:
                break
            if not copy and (new := pid_sockets() - sockets_before):
                self.own_sockets = list(new)
                self.stop()
                raise RuntimeError("another KiCad holds the default API socket without answering, so the hidden "
                                   "editor was out of reach and has been closed. Close that KiCad, or open a "
                                   "copy (copy=true)")
            time.sleep(0.5)
        tail = self.log.read_text(errors="replace")[-1500:] if self.log.exists() else ""
        if not copy:
            self.own_sockets = list(pid_sockets() - sockets_before)
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
            # xvfb-run exits at once and pcbnew outlives it for a moment, still holding the socket:
            # stopped means the whole group is gone, so a client or a new editor finds it free
            pgid = self.proc.pid
            os.killpg(pgid, signal.SIGTERM)
            deadline = time.monotonic() + STOP_TIMEOUT
            try:
                self.proc.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
            if not group_gone(pgid, deadline - time.monotonic()):
                os.killpg(pgid, signal.SIGKILL)
                self.proc.wait(timeout=STOP_TIMEOUT)
                group_gone(pgid, STOP_TIMEOUT)
        except ProcessLookupError:
            pass

    def stop(self, keep_files: bool = False) -> dict:
        """Stop the hidden editor. With keep_files the private copy stays on disk. A left-behind
        default socket stays too: the next KiCad takes it over."""
        board, root, copied = self.board, self.root, self.is_copy
        self._kill()
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        for lk in self.locks:
            lk.unlink(missing_ok=True)
        self.locks = []
        for sock in self.own_sockets:
            sock.unlink(missing_ok=True)
        self.own_sockets = []
        keep = keep_files and copied
        if root and keep:
            for leftover in root.glob("xvfb-run.*"):
                shutil.rmtree(leftover, ignore_errors=True)
        if root and not keep:
            # Windows keeps a file locked while a process still holds it: give it a moment
            for _ in range(10 if WINDOWS else 1):
                shutil.rmtree(root, ignore_errors=True)
                if not root.exists():
                    break
                time.sleep(0.5)
        self.proc = self.root = self.board = self.source = self.log = None
        return {"stopped": True,
                "board": str(board) if board and (keep or not copied) else None,
                "files_removed": copied and not keep}

    def status(self) -> dict:
        return {"running": self.running, "board": str(self.board) if self.board else None,
                "copy_of": str(self.source) if self.source and self.source != self.board else None,
                "socket": self.socket if self.running else None}
