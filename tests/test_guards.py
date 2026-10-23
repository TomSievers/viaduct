import pytest
from shapely.geometry import box

from viaduct import geometry
from viaduct.headless import Headless, lock_file
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
