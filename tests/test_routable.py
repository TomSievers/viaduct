import pytest
from shapely.geometry import box

from viaduct import geometry, routable
from viaduct.fanout import NetRule
from viaduct.model import BoardModel, Copper

LAYERS = ["F.Cu", "B.Cu"]


def pad(name, net, x, y, size=0.6, layers=("F.Cu",)):
    half = size / 2
    return Copper(name, "pad", net, {L: box(x - half, y - half, x + half, y + half) for L in layers},
                  ref=name, at=(x, y))


def far_partner(net):
    return pad(f"{net}.far", net, 55, 3 + 4 * ((ord(net) - 65) % 9))


def pair(net, x, y, **kwargs):
    """A pad, plus its partner far away so the net needs a track."""
    return [pad(f"{net}.1", net, x, y, **kwargs), far_partner(net)]


def wall(net, name, x1, y1, x2, y2, width=0.2):
    return Copper(name, "track", net, {L: geometry.track(x1, y1, x2, y2, width) for L in ("F.Cu",)},
                  width=width, locked=True)


def rule(width=0.2, via=0.6, drill=0.3):
    return NetRule(width, via, drill)


def run(items, sizes, clearance=0.2, outline=box(0, 0, 60, 40), **kwargs):
    nets = {c.net for c in items if c.net}
    model = BoardModel(LAYERS, items, [], outline, {n: clearance for n in nets}, clearance, nets)
    rules = {n: sizes[n] if isinstance(sizes.get(n), NetRule) else rule(sizes.get(n, 0.2)) for n in nets}
    return routable.check(model, rules, **kwargs)


def rules_of(out):
    return [f["rule"] for f in out["findings"]]


def row(net, x, y):
    """Neighbours on both sides, so moving along the pad's width cannot help."""
    return [pad(f"{net}.1", net, x, y), far_partner(net)]


def test_clean_board_reports_nothing():
    out = run(pair("A", 10, 10) + pair("B", 20, 10), {})
    assert out["errors"] == 0 and out["checked"] == 4


def test_a_track_too_wide_for_any_spot_on_its_pad_is_blocked_and_says_what_fits():
    items = row("A", 10, 10) + row("B", 9, 10) + row("C", 11, 10)
    out = run(items, {"A": 1.2}, references=["A"])
    f = out["findings"][0]
    assert f["rule"] == "track_start_blocked" and f["items"] == ["A.1"]
    assert {b["item"] for b in f["blockers"]} == {"B.1", "C.1"}
    assert f["fix"] == "largest width that fits is 1.0 mm" or "0.99" in f["fix"]


def test_a_track_that_fits_off_centre_is_no_error():
    # the neighbour is on one side only: the track's end can sit on the pad's far side
    out = run(pair("A", 10, 10) + pair("B", 11, 10), {"A": 1.2}, references=["A"])
    assert "track_start_blocked" not in rules_of(out)


def test_same_net_copper_and_zones_do_not_block():
    zone = Copper("z", "zone", "B", {"F.Cu": box(5, 5, 15, 15)}, ref="pour")
    near = pad("A.3", "A", 11, 10)
    out = run(pair("A", 10, 10) + [near, zone] + pair("B", 40, 25), {"A": 3.0})
    assert out["errors"] == 0


def test_unlocked_copper_can_move_so_it_blocks_nothing():
    track = Copper("t", "track", "B", {"F.Cu": geometry.track(10.5, 5, 10.5, 15, 0.2)}, width=0.2, locked=False)
    assert run(pair("A", 10, 10) + pair("B", 40, 25) + [track], {"A": 1.0})["errors"] == 0
    locked = Copper("t", "track", "B", {"F.Cu": geometry.track(10.4, 5, 10.4, 15, 0.2)}, width=0.2, locked=True)
    left = Copper("u", "track", "B", {"F.Cu": geometry.track(9.6, 5, 9.6, 15, 0.2)}, width=0.2, locked=True)
    assert "track_start_blocked" in rules_of(run(pair("A", 10, 10) + pair("B", 40, 25) + [locked, left], {"A": 1.0}))


def test_two_pads_that_fit_alone_conflict_together():
    items = pair("A", 10, 10, size=0.2) + pair("B", 10.85, 10, size=0.2)
    out = run(items, {"A": 1.0, "B": 1.0}, clearance=0.1)
    assert rules_of(out) == ["track_start_conflict"]
    assert sorted(out["findings"][0]["items"]) == ["A.1", "B.1"]


def test_the_result_does_not_depend_on_pad_order():
    items = pair("A", 10, 10, size=0.2) + pair("B", 10.85, 10, size=0.2)
    forward = run(items, {"A": 1.0, "B": 1.0}, clearance=0.1)
    backward = run(items[::-1], {"A": 1.0, "B": 1.0}, clearance=0.1)
    assert rules_of(forward) == rules_of(backward)


def test_through_hole_pad_needs_room_on_one_layer():
    left = wall("B", "tl", 9.6, 5, 9.6, 15)
    right = wall("B", "tr", 10.4, 5, 10.4, 15)
    th = pair("A", 10, 10, layers=("F.Cu", "B.Cu"))
    assert run(th + pair("B", 40, 25) + [left, right], {"A": 1.0})["errors"] == 0
    smd = pair("A", 10, 10)
    assert rules_of(run(smd + pair("B", 40, 25) + [left, right], {"A": 1.0})) == ["track_start_blocked"]


def test_conflict_is_avoided_when_a_through_hole_pad_can_use_the_other_layer():
    a = pair("A", 10, 10, size=0.2, layers=("F.Cu", "B.Cu"))
    b = pair("B", 10.85, 10, size=0.2)
    assert "track_start_conflict" not in rules_of(run(a + b, {"A": 1.0, "B": 1.0}, clearance=0.1))


def box_walls(x1, y1, x2, y2, net="W"):
    """A locked ring of track around an area, on the front layer."""
    return [wall(net, "n", x1, y1, x2, y1), wall(net, "e", x2, y1, x2, y2),
            wall(net, "s", x2, y2, x1, y2), wall(net, "w", x1, y2, x1, y1), far_partner(net), pad("W.2", net, 50, 35)]


def test_a_pad_inside_a_closed_ring_with_no_via_spot_is_walled_in():
    # the ring is too small for a 1.2 mm via with its clearance, on every spot
    items = pair("A", 10, 10) + box_walls(9.2, 9.2, 10.8, 10.8)
    out = run(items, {"A": rule(0.2, via=1.2, drill=0.6)})
    assert rules_of(out) == ["pad_walled_in"]
    assert out["findings"][0]["items"] == ["A.1"]


def test_a_via_spot_inside_the_ring_is_a_way_out():
    items = pair("A", 10, 10) + box_walls(8, 8, 12, 12)
    assert run(items, {"A": rule()})["errors"] == 0


def test_same_net_copper_inside_the_ring_is_enough():
    partner = pad("A.3", "A", 10.5, 10)
    items = pair("A", 10, 10) + [partner] + box_walls(9.2, 9.2, 11.4, 10.8)
    assert run(items, {"A": rule(0.2, via=1.2, drill=0.6)})["errors"] == 0


def test_a_gap_in_the_ring_too_narrow_for_the_track_does_not_help():
    ring = [wall("W", "n", 8, 8, 12, 8), wall("W", "e", 12, 8, 12, 12), wall("W", "s", 12, 12, 8, 12),
            wall("W", "w1", 8, 12, 8, 10.2), wall("W", "w2", 8, 9.8, 8, 8), far_partner("W"), pad("W.2", "W", 50, 35)]
    # 0.2 mm wide gap between the two west pieces' ends; no via fits inside either (6 mm via)
    items = pair("A", 10, 10) + ring
    assert rules_of(run(items, {"A": rule(0.2, via=6, drill=3)})) == ["pad_walled_in"]
    wide = [wall("W", "n", 8, 8, 12, 8), wall("W", "e", 12, 8, 12, 12), wall("W", "s", 12, 12, 8, 12),
            wall("W", "w1", 8, 12, 8, 11), wall("W", "w2", 8, 9, 8, 8), far_partner("W"), pad("W.2", "W", 50, 35)]
    assert run(pair("A", 10, 10) + wide, {"A": rule(0.2, via=6, drill=3)})["errors"] == 0


def test_two_pockets_through_one_gap_trap_a_net():
    # three nets in one ring, with one gap that holds one track, and no room for a via
    ring = [wall("W", "n", 7, 6.5, 11, 6.5), wall("W", "e", 11, 6.5, 11, 13.5), wall("W", "s", 11, 13.5, 7, 13.5),
            wall("W", "w1", 7, 13.5, 7, 10.5), wall("W", "w2", 7, 9.5, 7, 6.5), far_partner("W"), pad("W.2", "W", 50, 35)]
    items = pair("A", 9, 8) + pair("B", 9, 10) + pair("C", 9, 12) + ring
    big = rule(0.2, via=30, drill=3)
    out = run(items, {"A": big, "B": big, "C": big})
    trapped = [f for f in out["findings"] if f["rule"] == "nets_trapped"]
    assert len(trapped) == 1
    assert trapped[0]["shortfall"] == 2
    assert sorted(trapped[0]["nets"]) == ["A", "B", "C"]
    assert any(set(limit["between"]) == {"w1", "w2"} or "w1" in limit["between"] for limit in trapped[0]["limits"])


def test_enough_gaps_for_every_net_is_no_error():
    ring = [wall("W", "n", 7, 6.5, 11, 6.5), wall("W", "e", 11, 6.5, 11, 13.5), wall("W", "s", 11, 13.5, 7, 13.5),
            wall("W", "w1", 7, 13.5, 7, 12), wall("W", "w2", 7, 8, 7, 6.5), far_partner("W"), pad("W.2", "W", 50, 35)]
    items = pair("A", 9, 8) + pair("B", 9, 10) + pair("C", 9, 12) + ring
    big = rule(0.2, via=30, drill=3)
    assert run(items, {"A": big, "B": big, "C": big})["errors"] == 0


def test_via_spots_count_as_ways_out_for_a_group():
    ring = box_walls(6, 6, 14, 14)
    items = pair("A", 9, 8) + pair("B", 9, 10) + pair("C", 9, 12) + ring
    assert run(items, {"A": rule(), "B": rule(), "C": rule()})["errors"] == 0


def test_joined_pads_and_single_pad_nets_are_skipped():
    a = pad("A.1", "A", 10, 10)
    stub = Copper("t", "track", "A", {"F.Cu": geometry.track(10, 10, 30, 10, 0.2)}, width=0.2)
    lone = pad("L.1", "L", 11, 10)
    out = run([a, pad("A.2", "A", 30, 10), stub, lone, *pair("B", 40, 25)], {"A": 3.0, "L": 3.0})
    assert (out["already_joined"], out["checked"], out["errors"]) == (2, 2, 0)


def test_board_edge_blocks_a_start_only_past_the_given_limit():
    assert rules_of(run(pair("A", 0.3, 10), {"A": 1.0}, edge_clearance=0.5))[0] == "track_start_blocked"
    assert run(pair("A", 5, 10), {"A": 1.0}, edge_clearance=0.5)["errors"] == 0


def test_refuses_unknown_names():
    with pytest.raises(ValueError, match="NOPE"):
        run(pair("A", 10, 10), {}, nets=["NOPE"])
    with pytest.raises(ValueError, match="U99"):
        run(pair("A", 10, 10), {}, references=["U99"])


def test_a_via_in_the_pad_is_only_a_way_out_when_allowed():
    items = row("A", 10, 10) + row("B", 8.5, 10) + row("C", 11.5, 10)
    sizes = {"A": rule(2.2, via=0.6, drill=0.3)}
    out = run(items, sizes, references=["A"])
    assert rules_of(out) == ["track_start_blocked"]
    assert out["findings"][0]["via_in_pad_fits"] is True
    assert run(items, sizes, references=["A"], via_in_pad=True)["errors"] == 0


def test_a_group_of_routable_pads_at_fine_pitch_is_no_error():
    # 0.5 mm pitch, 0.3 mm pads, 0.2 mm tracks and 0.2 mm clearance: as tight as an LQFP, and routable
    items = [c for i, n in enumerate("ABCDEF") for c in pair(n, 10 + i * 0.5, 10, size=0.3)]
    assert run(items, {n: rule() for n in "ABCDEF"})["errors"] == 0
