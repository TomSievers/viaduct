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


def test_headless_refuses_a_board_open_in_kicad(tmp_path):
    board = tmp_path / "x.kicad_pcb"
    board.write_text("(kicad_pcb)")
    lock_file(board).write_text("")
    with pytest.raises(RuntimeError, match="open in KiCad"):
        Headless().start(str(board), copy=False)


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


def test_missing_xvfb_is_explained(monkeypatch, tmp_path):
    board = tmp_path / "x.kicad_pcb"
    board.write_text("(kicad_pcb)")
    monkeypatch.setenv("VIADUCT_PCBNEW", "/bin/true")
    monkeypatch.delenv("VIADUCT_XVFB_RUN", raising=False)
    monkeypatch.setattr(hl.shutil, "which", lambda name: None)
    monkeypatch.setattr(hl, "WINDOWS", False)
    h = Headless()
    with pytest.raises(RuntimeError, match="xvfb"):
        h.start(str(board))
    assert h.root is None          # the private dir is cleaned up again


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
