import json

import pytest

from viaduct import sexpr, setup_files as sf
from viaduct.headless import lock_file

BOARD = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(4 "In1.Cu" signal)
\t\t(6 "In2.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(25 "Edge.Cuts" user)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t\t(allow_soldermask_bridges_in_footprints no)
\t\t(tenting
\t\t\t(front yes)
\t\t\t(back yes)
\t\t)
\t\t(capping no)
\t\t(filling no)
\t\t(pcbplotparams
\t\t\t(mode 1)
\t\t)
\t)
\t(net 0 "")
)
"""

PROJECT = {
    "board": {"design_settings": {
        "defaults": {"silk_line_width": 0.1, "zones": {"min_clearance": 0.25}, "dimension_precision": 4},
        "rule_severities": {"silk_overlap": "warning", "clearance": "error"},
        "teardrop_options": [{"td_onvia": True}],
        "teardrop_parameters": [{"td_enabled": False, "td_target_name": "td_round_shape"}],
        "tuning_pattern_settings": {"single_track_defaults": {"spacing": 0.6}},
    }},
    "net_settings": {
        "classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2},
                    {"name": "PWR", "clearance": 0.3, "track_width": 0.5}],
        "netclass_assignments": {"+5V": ["PWR"], "SIG": ["PWR", "Other"]},
        "netclass_patterns": [{"netclass": "PWR", "pattern": "+*"}],
    },
}


@pytest.fixture
def board(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(BOARD)
    (tmp_path / "b.kicad_pro").write_text(json.dumps(PROJECT, indent=2) + "\n")
    return pcb


def project(board):
    return json.loads(board.with_suffix(".kicad_pro").read_text())


def test_parse_keeps_positions_and_unquotes():
    root = sexpr.parse(BOARD)
    layer = root.first("layers").lists()[4]
    assert BOARD[layer.start:layer.end] == '(5 "F.SilkS" user "F.Silkscreen")'
    assert layer.atoms == ["F.SilkS", "user", "F.Silkscreen"]
    assert sexpr.unquote(sexpr.quote('a "b" \\c')) == 'a "b" \\c'


def test_numbers_are_written_like_kicad():
    assert [sexpr.num(v) for v in (1.6, 0.035, 0.0, -0.0, 2.0, 0.0001234567)] == \
        ["1.6", "0.035", "0", "0", "2", "0.000123"]


def test_dry_run_writes_nothing(board):
    before = board.read_text()
    r = sf.set_mask_and_paste(str(board), {"pad_to_mask_clearance": 0.05})
    assert not r["changed"] and r["would_change"][0]["after"] == 0.05
    assert board.read_text() == before


def test_mask_edit_touches_only_its_settings(board):
    r = sf.set_mask_and_paste(str(board), {"pad_to_mask_clearance": 0.05, "solder_mask_min_width": 0.1},
                              {"tenting": {"back": False}}, confirm=True)
    assert r["changed"] and r["verified"]
    text = board.read_text()
    assert "\t\t(pad_to_mask_clearance 0.05)\n\t\t(solder_mask_min_width 0.1)\n" \
           "\t\t(allow_soldermask_bridges_in_footprints no)" in text
    assert "(back no)" in text
    # everything outside the edited lists is unchanged
    assert text.replace("0.05)\n\t\t(solder_mask_min_width 0.1)", "0)").replace(
        "(back no)", "(back yes)") == BOARD


def test_writes_refuse_while_kicad_holds_the_project(board):
    lock_file(board.with_suffix(".kicad_pro")).write_text('{"hostname":"h","username":"u"}')
    with pytest.raises(sf.ProjectOpen):
        sf.set_mask_and_paste(str(board), {"pad_to_mask_clearance": 0.05}, confirm=True)
    assert board.read_text() == BOARD


def test_boards_from_older_kicad_are_refused(board):
    board.write_text(BOARD.replace('"10.0"', '"9.0"'))
    with pytest.raises(ValueError, match="KiCad 10"):
        sf.read(str(board))


@pytest.mark.parametrize("copper,diel", [(2, 1.51), (4, 0.48), (6, 0.274)])
def test_default_stackup_matches_kicad(copper, diel):
    inner = "".join(f'\t\t({2 + 2 * i} "In{i}.Cu" signal)\n' for i in range(1, copper - 1))
    text = BOARD.replace('\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)\n', inner)
    m = sf.default_stackup(sexpr.parse(text))
    diels = [it for it in m["layers"] if sf._kind(it) == "dielectric"]
    assert len(diels) == copper - 1
    assert all(d["sub"][0]["thickness"] == diel for d in diels)
    assert [d["type"] for d in diels][:2] == ["core", "prepreg"][:copper - 1]
    assert sf.stackup_thickness(m) == 1.6


def test_stackup_is_stored_and_thickness_follows(board):
    r = sf.set_stackup(str(board), layers=[{"layer": "dielectric 2", "thickness": 0.3, "type": "core"}],
                       copper_finish="ENIG", confirm=True)
    assert r["verified"]
    root = sexpr.parse(board.read_text())
    assert root.first("general").first("thickness").atoms == ["1.42"]
    m = sf.stored_stackup(root)
    assert m["copper_finish"] == "ENIG"
    assert [it["layer"] for it in m["layers"]][:3] == ["F.SilkS", "F.Mask", "F.Cu"]
    # stored once, a second edit rewrites the same block
    sf.set_stackup(str(board), layers=[{"layer": "F.Mask", "color": "Black"}], confirm=True)
    assert board.read_text().count("(stackup") == 1


@pytest.mark.parametrize("change", [
    {"layer": "F.SilkS", "thickness": 0.1},
    {"layer": "F.Cu", "type": "core"},
    {"layer": "dielectric 1", "epsilon_r": 0.5},
    {"layer": "dielectric 9", "thickness": 1},
])
def test_stackup_refuses_bad_changes(board, change):
    with pytest.raises(ValueError):
        sf.set_stackup(str(board), layers=[change])


def test_layer_names_and_types(board):
    sf.set_layer_names(str(board), {"In1.Cu": {"name": "GND", "type": "power"}, "F.SilkS": {"name": ""}},
                       confirm=True)
    text = board.read_text()
    assert '(4 "In1.Cu" power "GND")' in text and '(5 "F.SilkS" user)' in text
    with pytest.raises(ValueError, match="unique"):
        sf.set_layer_names(str(board), {"In2.Cu": {"name": "GND"}})
    with pytest.raises(ValueError, match="type"):
        sf.set_layer_names(str(board), {"F.SilkS": {"type": "power"}})


def test_project_settings_patch_only_known_keys(board):
    r = sf.set_project_settings(str(board), rule_severities={"silk_overlap": "ignore"},
                                teardrops={"td_round_shape": {"td_enabled": True}},
                                graphics_defaults={"zones": {"min_clearance": 0.3}}, confirm=True)
    assert r["verified"]
    ds = project(board)["board"]["design_settings"]
    assert ds["rule_severities"] == {"silk_overlap": "ignore", "clearance": "error"}
    assert ds["teardrop_parameters"][0] == {"td_enabled": True, "td_target_name": "td_round_shape"}
    assert ds["defaults"]["zones"]["min_clearance"] == 0.3 and ds["defaults"]["silk_line_width"] == 0.1


@pytest.mark.parametrize("kwargs", [
    {"rule_severities": {"silk_overlap": "loud"}},
    {"graphics_defaults": {"silk_width": 0.1}},
    {"graphics_defaults": {"dimension_precision": 2.5}},
    {"graphics_defaults": {"zones": 1}},
    {"teardrops": {"td_oval": {"td_enabled": True}}},
])
def test_project_settings_refuse_bad_values(board, kwargs):
    with pytest.raises(ValueError):
        sf.set_project_settings(str(board), confirm=True, **kwargs)


def test_netclass_remove_moves_patterns_to_default(board):
    sf.edit_net_class(str(board), "PWR", remove=True, confirm=True)
    ns = project(board)["net_settings"]
    assert [c["name"] for c in ns["classes"]] == ["Default"]
    assert ns["netclass_patterns"] == [{"netclass": "Default", "pattern": "+*"}]
    assert ns["netclass_assignments"] == {"SIG": ["Other"]}


def test_netclass_unset_inherits_from_default(board):
    sf.edit_net_class(str(board), "PWR", unset=["clearance"], confirm=True)
    assert project(board)["net_settings"]["classes"][1] == {"name": "PWR", "track_width": 0.5}
    with pytest.raises(ValueError):
        sf.edit_net_class(str(board), "Default", unset=["clearance"])
