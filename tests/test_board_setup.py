import pytest
from kipy.proto.common.types import project_settings_pb2 as ps

from viaduct import kicad_io


def default():
    return kicad_io.patch_netclass(ps.NetClass(name="Default"), {
        "clearance": 0.2, "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3, "priority": 2147483647})


def test_netclass_round_trips_in_mm():
    d = kicad_io.netclass_dict(default())
    assert d["clearance"] == 0.2 and d["track_width"] == 0.25
    assert (d["via_diameter"], d["via_drill"]) == (0.6, 0.3)
    assert d["microvia_diameter"] is None and d["color"] is None


def test_patch_keeps_fields_it_does_not_name():
    nc = kicad_io.patch_netclass(default(), {"track_width": 0.4, "color": "#ff000080"})
    d = kicad_io.netclass_dict(nc)
    assert d["track_width"] == 0.4 and d["clearance"] == 0.2 and d["via_drill"] == 0.3
    assert d["color"] == "#ff000080"


def test_patch_returns_a_copy():
    nc = default()
    kicad_io.patch_netclass(nc, {"clearance": 0.5})
    assert kicad_io.netclass_dict(nc)["clearance"] == 0.2


def test_new_class_only_carries_what_was_set():
    d = kicad_io.netclass_dict(kicad_io.patch_netclass(ps.NetClass(name="HV"), {"clearance": 0.5}))
    assert d["clearance"] == 0.5
    assert all(d[f] is None for f in kicad_io.NETCLASS_FIELDS if f != "clearance")


@pytest.mark.parametrize("values", [
    {"via_diameter": 0.3},                 # drill 0.3 from Default is not smaller
    {"microvia_diameter": 0.2, "microvia_drill": 0.25},
    {"track_width": 0},
    {"clearance": -0.1},
    {"colour": "#ffffff"},
    {"color": "red"},
])
def test_patch_refuses_bad_values(values):
    with pytest.raises(ValueError):
        kicad_io.patch_netclass(default(), values)
