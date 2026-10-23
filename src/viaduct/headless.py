"""A hidden PCB editor: KiCad's pcbnew on a virtual X display (Xvfb).

KiCad's API is only served by its GUI, and kicad-cli has no server mode, so
"headless" means a real pcbnew nobody can see. Its socket lives under a short
private TMPDIR: that keeps it apart from the user's own KiCad
(/tmp/kicad/api.sock) and under the ~108 character limit for socket paths,
past which KiCad silently creates no socket at all.

KiCad 11 plans a built-in headless mode (KiCad 10 never will). Once it is released,
start() should check the installed KiCad version and use it on 11 or newer,
keeping this Xvfb path for KiCad 10.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

PROJECT_FILES = (".kicad_pcb", ".kicad_pro", ".kicad_prl")
TABLES = ("sym-lib-table", "fp-lib-table")


def lock_file(board: Path) -> Path:
    return board.with_name(f"~{board.name}.lck")


class Headless:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.root: Path | None = None
        self.board: Path | None = None
        self.source: Path | None = None
        self.log: Path | None = None
        atexit.register(self.stop)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def socket(self) -> str | None:
        return f"ipc://{self.root}/t/kicad/api.sock" if self.root else None

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
        pcbnew = os.environ.get("VIADUCT_PCBNEW") or shutil.which("pcbnew")
        xvfb = os.environ.get("VIADUCT_XVFB_RUN") or shutil.which("xvfb-run")
        if not pcbnew or not xvfb:
            raise RuntimeError(f"need pcbnew and xvfb-run on PATH (pcbnew={pcbnew}, xvfb-run={xvfb})")

        self.root = Path(tempfile.mkdtemp(prefix="vd", dir="/tmp"))
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
        self.log = self.root / "pcbnew.log"
        env = dict(os.environ, TMPDIR=str(self.root / "t"))
        env.pop("KICAD_API_SOCKET", None)
        self.proc = subprocess.Popen(
            [xvfb, "-a", "-s", "-screen 0 1920x1080x24", pcbnew, str(self.board)],
            env=env, stdout=self.log.open("w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)

        sock = self.root / "t" / "kicad" / "api.sock"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sock.exists():
                return {"socket": self.socket, "board": str(self.board), "copy_of": str(src) if copy else None,
                        "seconds": round(timeout - (deadline - time.monotonic()), 1)}
            if self.proc.poll() is not None:
                break
            time.sleep(0.5)
        tail = self.log.read_text(errors="replace")[-1500:] if self.log.exists() else ""
        self.stop()
        raise RuntimeError(f"hidden pcbnew did not open its API socket within {timeout:.0f}s "
                           f"(a dialog may be blocking it). Log tail:\n{tail}")

    def stop(self, keep_files: bool = False) -> dict:
        """Stop the hidden editor. With keep_files the private copy stays on disk."""
        board, root = self.board, self.root
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if root and not keep_files:
            shutil.rmtree(root, ignore_errors=True)
        copied = bool(root and board and root in board.parents)
        self.proc = self.root = self.board = self.source = self.log = None
        return {"stopped": True,
                "board": str(board) if board and (keep_files or not copied) else None,
                "files_removed": bool(root) and not keep_files}

    def status(self) -> dict:
        return {"running": self.running, "board": str(self.board) if self.board else None,
                "copy_of": str(self.source) if self.source and self.source != self.board else None,
                "socket": self.socket if self.running else None}
