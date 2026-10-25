import math

from shapely.geometry import box

from viaduct import fanout, geometry, plan
from viaduct.model import BoardModel, Copper, Hole

LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
NETS = {"GND", "+3V3", "SIG"}


def board(items=(), holes=(), zones=None):
    """40 x 30 board, a GND plane on In1 and a +3V3 plane on In2 covering it."""
    zones = zones if zones is not None else [
        Copper("zg", "zone", "GND", {"In1.Cu": box(0.5, 0.5, 39.5, 29.5)}, ref="GND_In1"),
        Copper("zp", "zone", "+3V3", {"In2.Cu": box(0.5, 0.5, 39.5, 29.5)}, ref="3V3_In2"),
    ]
    return BoardModel(LAYERS, [*items, *zones], list(holes), box(0, 0, 40, 30),
                      {n: 0.15 for n in NETS}, 0.15, NETS)


def pad(id_, net, ref, x, y, w=0.6, h=0.6, layer="F.Cu"):
    return Copper(id_, "pad", net, {layer: box(x - w / 2, y - h / 2, x + w / 2, y + h / 2)}, ref=ref, at=(x, y))


def capacitor(x=20, y=15):
    return [pad("c1", "+3V3", "C1.1", x - 0.5, y), pad("c2", "GND", "C1.2", x + 0.5, y)]


def test_plane_layers_finds_inner_zones_only():
    outer = Copper("zt", "zone", "GND", {"F.Cu": box(0, 0, 5, 5)})
    m = board([outer])
    assert fanout.plane_layers(m) == {"GND": ["In1.Cu"], "+3V3": ["In2.Cu"]}


def test_capacitor_vias_go_outwards_and_pass_the_checker():
    m = board(capacitor())
    out = fanout.fanout(m)
    rows = {r["pad"]: r for r in out["rows"]}
    assert rows["C1.1"]["placed"] and rows["C1.2"]["placed"]
    assert rows["C1.1"]["via"][0] < 19.5 and rows["C1.2"]["via"][0] > 20.5   # away from the other pad
    assert rows["C1.2"]["direction"] == 0 and rows["C1.1"]["direction"] == 180
    p = plan.parse(out["plan"])
    assert plan.check(m, p) == []
    assert all(t["locked"] for t in out["plan"]["tracks"]) and all(v["locked"] for v in out["plan"]["vias"])


def test_stub_angles_are_multiples_of_45():
    out = fanout.fanout(board(capacitor()))
    for t in out["plan"]["tracks"]:
        (x1, y1), (x2, y2) = t["start"], t["end"]
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 45
        assert min(angle, 45 - angle) < 0.01


def test_ic_pins_fan_inwards():
    # a 6-pin part: pins on two rows, the GND pin at the top row's middle
    pins = [pad(f"u{i}", "SIG", f"U1.{i}", 18 + i, 13, 0.4, 1.0) for i in range(3)]
    pins += [pad(f"u{i + 3}", "SIG", f"U1.{i + 3}", 18 + i, 17, 0.4, 1.0) for i in range(3)]
    pins[1] = pad("u1", "GND", "U1.1", 19, 13, 0.4, 1.0)
    out = fanout.fanout(board(pins))
    row = next(r for r in out["rows"] if r["pad"] == "U1.1")
    assert row["placed"] and row["via"][1] > 13          # towards the part's centre (y = 15)


def test_through_hole_and_connected_pads_are_skipped():
    tht = Copper("j1", "pad", "GND", {L: box(4.4, 4.4, 5.6, 5.6) for L in LAYERS}, ref="J1.1", drill=0.8, at=(5, 5))
    done = pad("c9", "GND", "C9.1", 10, 10)
    via = Copper("v9", "via", "GND", {L: geometry.via(10, 10, 0.6) for L in LAYERS}, drill=0.3, at=(10, 10))
    zone = Copper("zg", "zone", "GND", {"In1.Cu": box(0.5, 0.5, 39.5, 29.5)}, ref="GND_In1")
    m = board([tht, done, via], [Hole(5, 5, 0.4, "J1.1"), Hole(10, 10, 0.15, "v9")], [zone])
    assert fanout.fanout(m)["rows"] == []
    assert [r["pad"] for r in fanout.fanout(m, include_connected=True)["rows"]] == ["C9.1"]


def test_a_via_needs_solid_plane_under_it():
    # the GND plane has a hole around the pad: no spot within reach lands on it
    zone = Copper("zg", "zone", "GND", {"In1.Cu": box(0.5, 0.5, 39.5, 29.5).difference(box(17, 12, 23, 18))})
    out = fanout.fanout(board([pad("c2", "GND", "C1.2", 20, 15)], zones=[zone]))
    assert out["rows"] == [{"pad": "C1.2", "net": "GND", "layer": "F.Cu", "spots": 0, "placed": False}]


def test_missing_plane_is_an_error():
    out = fanout.fanout(board(capacitor(), zones=[]), planes={"GND": ["In1.Cu"]})
    assert out["errors"] and "no filled zone" in out["errors"][0]


def test_neighbouring_pads_do_not_get_clashing_vias():
    # two GND and +3V3 caps side by side, 1.4 mm apart: every via keeps its clearance to the others
    items = capacitor(20, 15) + [pad("d1", "+3V3", "C2.1", 19.5, 16.4), pad("d2", "GND", "C2.2", 20.5, 16.4)]
    m = board(items)
    out = fanout.fanout(m)
    assert all(r["placed"] for r in out["rows"])
    assert plan.check(m, plan.parse(out["plan"])) == []


def test_locked_round_trips_through_the_plan():
    p = plan.parse({"tracks": [{"net": "GND", "layer": "F.Cu", "start": [1, 1], "end": [2, 1], "locked": True}],
                    "vias": [{"net": "GND", "at": [2, 1]}]})
    assert p.tracks[0].locked and not p.vias[0].locked


def test_a_top_pour_does_not_count_as_reaching_the_plane():
    # C1.2 touches a GND pour on F.Cu that a through-hole pad ties to In1: it still gets a via
    pour = Copper("zt", "zone", "GND", {"F.Cu": box(1, 1, 25, 20)}, ref="GND_top")
    tht = Copper("j1", "pad", "GND", {L: box(4.4, 4.4, 5.6, 5.6) for L in LAYERS}, ref="J1.1", drill=0.8, at=(5, 5))
    m = board(capacitor() + [pour, tht], [Hole(5, 5, 0.4, "J1.1")])
    assert {r["pad"] for r in fanout.fanout(m)["rows"]} == {"C1.1", "C1.2"}


def test_a_fill_patch_on_a_pad_joins_nothing_else():
    # KiCad leaves a fill patch on a pad its pour cannot reach: that patch is not the pour
    from shapely.geometry import MultiPolygon
    from viaduct import connectivity
    ep = pad("ep", "GND", "U1.9", 20, 15, 2, 2)
    pour = Copper("zt", "zone", "GND", {"F.Cu": MultiPolygon([box(1, 1, 10, 10), box(19, 14, 21, 16)])}, ref="GND_top")
    other = pad("c9", "GND", "C9.1", 5, 5)
    groups = connectivity.islands(board([ep, pour, other], zones=[]), "GND")
    assert sorted(g["pads"] for g in groups) == [["C9.1"], ["U1.9"]]
    assert next(g for g in groups if g["pads"] == ["C9.1"])["zone_area"] == {"zt": 81.0}


def test_a_patch_of_plane_fill_is_not_the_plane():
    # In1's fill keeps a patch on a through-hole pad inside a cut-out: that pad is not on the plane
    from shapely.ops import unary_union
    fill = unary_union([box(0.5, 0.5, 39.5, 29.5).difference(box(14, 9, 26, 21)), box(19.5, 14.5, 20.5, 15.5)])
    tht = Copper("u9", "pad", "GND", {L: box(19.6, 14.6, 20.4, 15.4) for L in LAYERS}, ref="U9.1", drill=0.4,
                 at=(20, 15))
    m = board([tht], [Hole(20, 15, 0.2, "U9.1")], [Copper("zg", "zone", "GND", {"In1.Cu": fill})])
    assert fanout._reached(m, "GND", ["In1.Cu"]) == set()


def _angles_ok(tracks):
    for t in tracks:
        (x1, y1), (x2, y2) = t["start"], t["end"]
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 45
        assert min(angle, 45 - angle) < 0.01, t


def test_neighbouring_pads_of_one_net_share_a_via():
    # two GND pins 0.8 mm apart on a connector: one via, the other pin joins it
    items = [pad("j1", "GND", "J2.1", 20, 15, 0.5, 1.2), pad("j2", "GND", "J2.2", 20.8, 15, 0.5, 1.2)]
    m = board(items)
    out = fanout.fanout(m)
    assert len(out["plan"]["vias"]) == 1
    assert sum("shared" in r for r in out["rows"]) == 1 and all(r["placed"] for r in out["rows"])
    assert plan.check(m, plan.parse(out["plan"])) == []
    _angles_ok(out["plan"]["tracks"])


def test_an_existing_via_on_the_plane_is_shared():
    via = Copper("v1", "via", "GND", {L: geometry.via(20.6, 15.6, 0.6) for L in LAYERS}, drill=0.3, at=(20.6, 15.6))
    m = board([pad("c2", "GND", "C1.2", 20, 15), via], [Hole(20.6, 15.6, 0.15, "v1")])
    out = fanout.fanout(m)
    assert out["plan"]["vias"] == [] and out["rows"][0]["shared"] == "via v1"
    _angles_ok(out["plan"]["tracks"])
    assert plan.check(m, plan.parse(out["plan"])) == []


def test_share_distance_zero_gives_every_pad_its_own_via():
    items = [pad("j1", "GND", "J2.1", 20, 15, 0.5, 1.2), pad("j2", "GND", "J2.2", 20.8, 15, 0.5, 1.2)]
    out = fanout.fanout(board(items), fanout.Rules(share_distance=0))
    assert len(out["plan"]["vias"]) == 2


def test_one_new_via_serves_at_most_pads_per_via():
    row = [pad(f"j{i}", "GND", f"J3.{i}", 15 + 0.5 * i, 15, 0.3, 1.2) for i in range(6)]
    m = board(row)
    out = fanout.fanout(m, fanout.Rules(pads_per_via=3, share_distance=1.5))
    assert all(r["placed"] for r in out["rows"]) and len(out["plan"]["vias"]) <= 3
    from collections import Counter
    served = Counter(tuple(r["via"]) for r in out["rows"])
    assert max(served.values()) <= 3 and sum(served.values()) == 6
    assert plan.check(m, plan.parse(out["plan"])) == []


def test_a_pad_with_no_via_spot_shares_its_neighbours_via():
    # the plane is cut away under C1.2 (x < 20.4) but not under C1.3: C1.2 can only share
    fill = box(0.5, 0.5, 39.5, 29.5).difference(box(15, 10, 20.4, 20))
    zones = [Copper("zg", "zone", "GND", {"In1.Cu": fill})]
    m = board([pad("a", "GND", "C1.2", 19.8, 15), pad("b", "GND", "C1.3", 20.6, 15)], zones=zones)
    rows = {r["pad"]: r for r in fanout.fanout(m)["rows"]}
    assert rows["C1.2"]["spots"] == 0 and rows["C1.2"]["placed"] and rows["C1.2"]["shared"]


def test_octilinear_paths_bend_once_at_45():
    assert fanout._octilinear(0, 0, 2, 0) == [[(0, 0), (2, 0)]]
    paths = fanout._octilinear(0, 0, 2, 1)
    assert [p[1] for p in paths] == [(1.0, 1.0), (1.0, 0.0)]


def test_stacked_pads_of_one_net_do_not_block_each_other():
    # USB-C style: A1 and B12 are two GND pads in the same place
    a = pad("a1", "GND", "J1.A1", 20, 15, 0.6, 1.1)
    b = pad("b12", "GND", "J1.B12", 20, 15, 0.6, 1.1)
    m = board([a, b])
    out = fanout.fanout(m)
    assert all(r["placed"] for r in out["rows"]) and len(out["plan"]["vias"]) == 1
    assert plan.check(m, plan.parse(out["plan"])) == []
