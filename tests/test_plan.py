from shapely.geometry import box

from viaduct import connectivity, geometry, plan
from viaduct.model import BoardModel, Copper, Hole

LAYERS = ["F.Cu", "B.Cu"]


def board(extra=()):
    """60 x 40 board: a GND pad, a +3V3 pad, a SIG track and a GND via."""
    items = [
        Copper("p1", "pad", "GND", {"F.Cu": box(9.5, 9.5, 10.5, 10.5)}, ref="C1.2", at=(10, 10)),
        Copper("p2", "pad", "+3V3", {"F.Cu": box(11.5, 9.5, 12.5, 10.5)}, ref="C1.1", at=(12, 10)),
        Copper("t1", "track", "SIG", {"F.Cu": geometry.track(5, 20, 30, 20, 0.2)}, width=0.2),
        Copper("v1", "via", "GND", {L: geometry.via(40, 30, 0.6) for L in LAYERS}, drill=0.3, at=(40, 30)),
        *extra,
    ]
    return BoardModel(LAYERS, items, [Hole(40, 30, 0.15, "v1")], box(0, 0, 60, 40),
                      {"GND": 0.15, "+3V3": 0.15, "SIG": 0.15}, 0.15, {"GND", "+3V3", "SIG"})


def test_clean_plan_has_no_violations():
    p = plan.parse({"tracks": [{"net": "GND", "layer": "F.Cu", "start": [10, 10], "end": [10, 15], "width": 0.25}],
                    "vias": [{"net": "GND", "at": [10, 15]}]})
    assert plan.check(board(), p) == []


def test_clearance_to_other_net_is_reported():
    # GND track whose edge passes 0.075 mm above the +3V3 pad (needs 0.15)
    p = plan.parse({"tracks": [{"net": "GND", "layer": "F.Cu", "start": [9, 10.7], "end": [13, 10.7], "width": 0.25}]})
    found = [v for v in plan.check(board(), p) if v["rule"] == "clearance"]
    assert len(found) == 1 and found[0]["other"] == "C1.1" and abs(found[0]["actual"] - 0.075) < 0.002


def test_same_net_contact_is_allowed():
    p = plan.parse({"tracks": [{"net": "GND", "layer": "F.Cu", "start": [10, 10], "end": [40, 30], "width": 0.25}]})
    v = [x for x in plan.check(board(), p) if x.get("other") in ("C1.2", "v1")]
    assert v == []


def test_via_hits_track_on_any_layer():
    p = plan.parse({"vias": [{"net": "GND", "at": [20, 20.3]}]})
    assert any(v["rule"] == "clearance" and v["other_net"] == "SIG" for v in plan.check(board(), p))


def test_hole_to_hole():
    p = plan.parse({"vias": [{"net": "GND", "at": [40.5, 30]}]})
    assert any(v["rule"] == "hole_to_hole" for v in plan.check(board(), p))


def test_deleting_an_obstacle_clears_the_violation():
    p = plan.parse({"delete": ["t1"], "vias": [{"net": "GND", "at": [20, 20]}]})
    assert plan.check(board(), p) == []


def test_zones_can_be_deleted_and_are_not_obstacles():
    zone = Copper("z1", "zone", "+3V3", {"F.Cu": box(0, 0, 60, 40)}, ref="3V3_top")
    p = plan.parse({"delete": ["z1"], "tracks": [{"net": "GND", "layer": "F.Cu", "start": [30, 5], "end": [35, 5]}]})
    assert plan.check(board([zone]), p) == []


def test_pads_cannot_be_deleted_and_unknown_ids_are_reported():
    rules = {v["rule"] for v in plan.check(board(), plan.parse({"delete": ["p1", "nope"]}))}
    assert rules == {"delete_refused", "delete_missing"}


def test_board_edge_and_unknown_layer():
    p = plan.parse({"tracks": [{"net": "GND", "layer": "In1.Cu", "start": [0.1, 5], "end": [0.1, 8]}]})
    rules = {v["rule"] for v in plan.check(board(), p)}
    assert "layer_not_enabled" in rules
    p2 = plan.parse({"tracks": [{"net": "GND", "layer": "F.Cu", "start": [0.1, 5], "end": [0.1, 8]}]})
    assert {v["rule"] for v in plan.check(board(), p2)} == {"board_edge"}


def test_assumed_layers_allow_inner_copper():
    p = plan.parse({"tracks": [{"net": "SIG", "layer": "In2.Cu", "start": [5, 5], "end": [8, 5]}]})
    assert plan.check(board(), p, layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]) == []


def test_new_items_checked_against_each_other():
    p = plan.parse({"tracks": [{"net": "GND", "layer": "F.Cu", "start": [30, 5], "end": [35, 5]},
                               {"net": "SIG", "layer": "F.Cu", "start": [30, 5.3], "end": [35, 5.3]}]})
    assert any(v["rule"] == "clearance_new" for v in plan.check(board(), p))


def test_legacy_segment_format():
    p = plan.parse({"segs": [{"net": "GND", "layer": "F.Cu", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "w": 0.2}],
                    "vias": [{"net": "GND", "x": 5, "y": 6}]})
    assert (p.tracks[0].x2, p.tracks[0].width, p.vias[0].diameter) == (3, 0.2, 0.6)


def test_islands_split_and_join():
    m = board()
    groups = connectivity.islands(m, "GND")
    assert len(groups) == 2                       # pad and via are not joined
    joined = board([Copper("t2", "track", "GND", {"F.Cu": geometry.track(10, 10, 40, 30, 0.25)})])
    assert len(connectivity.islands(joined, "GND")) == 1


def test_arc_points_follow_the_mid_point():
    pts = geometry.arc_points((1, 0), (0, 1), (-1, 0))
    assert all(y >= -1e-9 for _, y in pts)        # upper half circle through (0, 1)
    assert pts[0] == (1, 0) and abs(pts[-1][0] + 1) < 1e-9
