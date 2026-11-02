import subprocess
import sys
import time
from pathlib import Path

import pytest
from shapely.geometry import box

from viaduct import geometry
from viaduct import headless as hl
from viaduct.headless import Headless, hidden_env, lock_file
from viaduct.model import BoardModel, Copper


def model(track_end=30.0):
    items = [Copper("p1", "pad", "GND", {"F.Cu": box(9.5, 9.5, 10.5, 10.5)}),
             Copper("t1", "track", "SIG", {"F.Cu": geometry.track(5, 20, track_end, 20, 0.2)}),
             Copper("z1", "zone", "GND", {"F.Cu": box(0, 0, 50, 50)})]
    return BoardModel(["F.Cu", "B.Cu"], items)


def test_fingerprint_is_stable_and_sees_copper_edits():
    assert model().fingerprint() == model().fingerprint()
    assert model().fingerprint() != model(track_end=31.0).fingerprint()


def test_fingerprint_ignores_zone_refills():
    m2 = model()
    m2.items[2].geom["F.Cu"] = box(1, 1, 49, 49)
    assert model().fingerprint() == m2.fingerprint()


@pytest.fixture
def board(tmp_path):
    b = tmp_path / "x.kicad_pcb"
    b.write_text("(kicad_pcb)")
    return b


@pytest.fixture
def no_kicad(monkeypatch):
    """Nothing answers on any socket, whatever runs on this machine."""
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr(hl, "open_boards", lambda socket, timeout_ms=1000: None)


def test_headless_refuses_a_locked_board_nobody_answers_for(board, no_kicad):
    # a hidden editor opening a locked file hangs on an invisible dialog
    lock_file(board).write_text("")
    with pytest.raises(RuntimeError, match="open in KiCad"):
        Headless().start(str(board))


def test_headless_uses_a_board_kicad_already_has_open(board, monkeypatch):
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr(hl, "open_boards", lambda socket, timeout_ms=1000: [board.resolve()])
    lock_file(board).write_text("")          # KiCad's own lock: no reason to refuse
    h = Headless()
    started = h.start(str(board))
    assert started["already_open"] and started["konnect_reachable"] and started["board"] == str(board.resolve())
    assert not h.running and h.root is None


@pytest.mark.parametrize("boards, named", [([Path("/w/other.kicad_pcb")], "other.kicad_pcb"), ([], "no board")])
def test_headless_refuses_when_another_kicad_holds_the_socket(board, monkeypatch, boards, named):
    monkeypatch.delenv("KICAD_API_SOCKET", raising=False)
    monkeypatch.setattr(hl, "open_boards", lambda socket, timeout_ms=1000: boards)
    with pytest.raises(RuntimeError, match=named):
        Headless().start(str(board))


def test_headless_refuses_a_socket_setting_it_could_not_serve(board, monkeypatch):
    monkeypatch.setattr(hl, "open_boards", lambda socket, timeout_ms=1000: None)
    monkeypatch.setenv("KICAD_API_SOCKET", "ipc:///elsewhere/api.sock")
    with pytest.raises(RuntimeError, match="KICAD_API_SOCKET"):
        Headless().start(str(board))


def test_a_copy_ignores_who_holds_the_default_socket(board, monkeypatch):
    # gets past the socket and lock checks; fails later only because pcbnew is missing here
    monkeypatch.setattr(hl, "open_boards", lambda socket, timeout_ms=1000: [Path("/w/other.kicad_pcb")])
    lock_file(board).write_text("")
    monkeypatch.setattr(hl, "find_pcbnew", lambda: None)
    with pytest.raises(RuntimeError, match="pcbnew not found"):
        Headless().start(str(board), copy=True)


def test_socket_follows_the_mode(tmp_path):
    h = Headless()
    assert h.socket is None
    h.root, h.board = tmp_path, tmp_path.parent / "real.kicad_pcb"
    assert not h.is_copy and h.socket == hl.default_socket()
    h.board = tmp_path / "p" / "copy.kicad_pcb"
    assert h.is_copy and str(tmp_path) in h.socket
    h.root = h.board = None


def test_stopping_keeps_the_real_board_and_removes_the_editors_files(tmp_path):
    real = tmp_path / "real.kicad_pcb"
    real.write_text("(kicad_pcb)")
    private = tmp_path / "vd"
    private.mkdir()
    extra = tmp_path / "api-1.sock"
    extra.write_text("")
    h = Headless()
    h.root, h.board, h.own_sockets = private, real, [extra]
    out = h.stop(keep_files=True)           # keep_files is about copies; the log dir always goes
    assert out["board"] == str(real) and out["files_removed"] is False
    assert real.exists() and not private.exists() and not extra.exists()


def test_stranded_editors_are_the_ones_that_answer(monkeypatch, tmp_path):
    alive, dead = tmp_path / "api-1.sock", tmp_path / "api-2.sock"
    monkeypatch.setattr(hl, "pid_sockets", lambda windows=False: {alive, dead})
    monkeypatch.setattr(hl, "open_boards", lambda s, timeout_ms=1000: [Path("/w/x.kicad_pcb")] if "api-1" in s else None)
    assert hl.stranded_editors() == {str(alive): ["/w/x.kicad_pcb"]}


def test_headless_rejects_non_boards(tmp_path):
    with pytest.raises(ValueError):
        Headless().start(str(tmp_path / "nope.kicad_sch"))


def test_stopping_the_hidden_editor_removes_only_its_own_locks(tmp_path):
    ours, theirs = tmp_path / "~x.kicad_pcb.lck", tmp_path / "~x.kicad_pro.lck"
    ours.write_text("")
    theirs.write_text("")
    h = Headless()
    h.locks = [ours]           # what start() found missing before pcbnew ran
    h.stop()
    assert not ours.exists() and theirs.exists()


def test_hidden_editor_cannot_reach_the_users_wayland_session(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("GDK_BACKEND", "wayland")
    monkeypatch.setenv("KICAD_API_SOCKET", "/tmp/kicad/api.sock")
    env = hidden_env(tmp_path)
    assert "WAYLAND_DISPLAY" not in env and "KICAD_API_SOCKET" not in env
    assert env["GDK_BACKEND"] == "x11" and env["TMPDIR"] == str(tmp_path)


def test_without_a_private_dir_the_editor_lands_on_kicads_default_socket(monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp/nix-shell.abc")     # would move the socket away from everyone
    assert hidden_env()["TMPDIR"] == "/tmp"
    monkeypatch.setenv("TEMP", r"C:\Users\u\AppData\Local\Temp")
    assert hidden_env(windows=True)["TEMP"] == r"C:\Users\u\AppData\Local\Temp"


def test_on_windows_the_hidden_editor_gets_its_own_temp_dir(monkeypatch, tmp_path):
    # KiCad puts its API socket under %TEMP% on Windows: TMP and TEMP both point to the private dir
    monkeypatch.setenv("KICAD_API_SOCKET", r"ipc://C:\Temp\kicad\api.sock")
    env = hidden_env(tmp_path, windows=True)
    assert env["TMP"] == env["TEMP"] == str(tmp_path) and "KICAD_API_SOCKET" not in env


def test_pcbnew_is_found_under_program_files_newest_version_first(monkeypatch, tmp_path):
    for version in ("9.0", "10.0", "8.0"):
        exe = tmp_path / "KiCad" / version / "bin" / "pcbnew.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
    monkeypatch.delenv("VIADUCT_PCBNEW", raising=False)
    monkeypatch.setattr(hl.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert hl.find_pcbnew(windows=True) == str(tmp_path / "KiCad" / "10.0" / "bin" / "pcbnew.exe")
    assert hl.find_pcbnew(windows=False) is None
    monkeypatch.setenv("VIADUCT_PCBNEW", "/opt/kicad/pcbnew")
    assert hl.find_pcbnew(windows=True) == "/opt/kicad/pcbnew"


def test_nothing_answers_on_a_socket_nobody_serves(tmp_path):
    assert not hl.answers(f"ipc://{tmp_path}/none.sock", timeout_ms=200)


def test_missing_xvfb_is_explained(monkeypatch, board, no_kicad):
    monkeypatch.setenv("VIADUCT_PCBNEW", "/bin/true")
    monkeypatch.delenv("VIADUCT_XVFB_RUN", raising=False)
    monkeypatch.setattr(hl.shutil, "which", lambda name: None)
    monkeypatch.setattr(hl, "WINDOWS", False)
    h = Headless()
    with pytest.raises(RuntimeError, match="xvfb"):
        h.start(str(board))
    assert h.root is None          # the private dir is cleaned up again


def test_xvfb_run_gets_a_temp_dir_stop_removes_and_the_editor_keeps_its_own(monkeypatch, tmp_path):
    monkeypatch.setenv("VIADUCT_XVFB_RUN", "/x/xvfb-run")
    monkeypatch.setattr(hl, "WINDOWS", False)
    h = Headless()
    h.root, h.board = tmp_path, tmp_path.parent / "real.kicad_pcb"
    env = hidden_env(windows=False)
    cmd, _ = h._command("/x/pcbnew", env)
    assert env["TMPDIR"] == str(tmp_path)
    assert cmd[cmd.index("env"):] == ["env", "TMPDIR=/tmp", "/x/pcbnew", str(h.board)]
    h.root = h.board = None


def test_stopping_with_a_kept_copy_still_removes_xvfb_runs_files(tmp_path):
    (tmp_path / "xvfb-run.abc").mkdir()
    board = tmp_path / "p" / "x.kicad_pcb"
    board.parent.mkdir()
    board.write_text("(kicad_pcb)")
    h = Headless()
    h.root, h.board = tmp_path, board
    h.stop(keep_files=True)
    assert board.exists() and not (tmp_path / "xvfb-run.abc").exists()


def test_an_older_kicad_is_refused_with_its_version(monkeypatch):
    from kipy.kicad import KiCadVersion
    from viaduct import kicad_io

    class OldKiCad:
        def __init__(self, **_):
            pass

        def get_version(self):
            return KiCadVersion(10, 0, 5, "10.0.5")

    monkeypatch.setattr(kicad_io, "KiCad", OldKiCad)
    with pytest.raises(RuntimeError, match=r"needs KiCad 10\.0\.6 or newer; this is KiCad 10\.0\.5"):
        kicad_io.Session(lambda: None).board()


# a leader that dies on SIGTERM at once, like xvfb-run, and a child that takes longer, like pcbnew
LEADER = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", sys.argv[1], *sys.argv[2:]])
time.sleep(60)
"""
CHILD = """
import os, pathlib, signal, sys, time
def on_term(*_):
    if sys.argv[2] == "exit":
        time.sleep(0.4)
        pathlib.Path(sys.argv[1], "finished").write_text("")
        sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
pathlib.Path(sys.argv[1], "ready").write_text(str(os.getpid()))
time.sleep(60)
pathlib.Path(sys.argv[1], "ran-on").write_text("")
"""


def editor_group(tmp_path, on_term):
    proc = subprocess.Popen([sys.executable, "-c", LEADER, CHILD, str(tmp_path), on_term], start_new_session=True)
    deadline = time.monotonic() + 20
    while not (tmp_path / "ready").exists() or not (tmp_path / "ready").read_text():
        assert time.monotonic() < deadline, "the child never started"
        time.sleep(0.02)
    h = Headless()
    h.proc = proc
    return h


@pytest.mark.skipif(hl.WINDOWS, reason="no process group on Windows")
def test_stopping_waits_for_the_editor_not_just_for_xvfb_run(tmp_path, monkeypatch):
    monkeypatch.setattr(hl, "STOP_TIMEOUT", 3.0)
    h = editor_group(tmp_path, "exit")
    h._kill()
    assert (tmp_path / "finished").exists()


@pytest.mark.skipif(hl.WINDOWS, reason="no process group on Windows")
def test_an_editor_ignoring_sigterm_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(hl, "STOP_TIMEOUT", 0.5)
    h = editor_group(tmp_path, "ignore")
    child = (tmp_path / "ready").read_text()
    h._kill()
    # killed, or at most a zombie waiting to be reaped; a live child would still be sleeping
    stat = Path("/proc", child, "stat")
    assert not stat.exists() or stat.read_text().rpartition(") ")[2].startswith("Z")
