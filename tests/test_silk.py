from shapely.geometry import box

from viaduct import silk


def _scene(labels, **kw):
    sc = silk.SilkScene(box(0, 0, 40, 30), labels)
    for d in (sc.pads, sc.silk, sc.bodies, sc.texts, sc.anchors):
        d["F"] = []
    for lb in labels:
        sc.anchors["F"].append((lb.ref, lb.anchor))
    for key, value in kw.items():
        getattr(sc, key)["F"].extend(value)
    return sc


def _label(ref, x, y, w=1.6, h=0.9):
    return silk.Label(ref, "F", ref, (x - w / 2, y - h / 2, x + w / 2, y + h / 2), 1.0, 0.15)


def _box(row, label):
    return silk.text_box(label.text, row["x"], row["y"], row["size"], label.thickness, row["rotation"])


def test_text_box_swaps_extent_when_vertical():
    h = silk.text_box("R12", 5, 5, 1.0, 0.15, 0).bounds
    v = silk.text_box("R12", 5, 5, 1.0, 0.15, 90).bounds
    assert round(h[2] - h[0], 6) == round(v[3] - v[1], 6) and round(h[3] - h[1], 6) == round(v[2] - v[0], 6)


def test_a_lone_part_gets_horizontal_text_at_full_size_next_to_it():
    lb = _label("C1", 20, 15)
    [row] = silk.place(_scene([lb]))
    assert row["placed"] and row["rotation"] == 0 and row["size"] == 1.0
    assert 0 < row["gap"] <= 0.3
    assert not _box(row, lb).intersects(box(*lb.anchor))


def test_text_keeps_clear_of_pads_and_bodies():
    lb = _label("R1", 20, 15)
    pads = [box(19, 12, 21, 14.5)]            # copper right above the part
    body = [box(19, 15.5, 21, 18)]            # and a tall part right below it
    rows = silk.place(_scene([lb], pads=pads, bodies=body))
    r = _box(rows[0], lb)
    assert rows[0]["placed"]
    assert r.distance(pads[0]) >= 0.15 - 1e-9 and not r.intersects(body[0].buffer(-0.05))


def test_neighbouring_references_do_not_touch():
    labels = [_label(f"C{i}", 10 + 2.0 * i, 15) for i in range(5)]
    rows = silk.place(_scene(labels))
    boxes = [_box(r, lb) for r, lb in zip(rows, sorted(labels, key=lambda lb: lb.ref)) if r["placed"]]
    assert len(boxes) == 5
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a.distance(b) >= 0.5 - 1e-9


def test_text_never_sits_nearer_another_part():
    labels = [_label("R1", 20, 15), _label("R2", 22.2, 15)]
    rows = {r["reference"]: r for r in silk.place(_scene(labels))}
    x, y = rows["R2"]["x"], rows["R2"]["y"]
    own = silk._bounds_gap((x, y, x, y), labels[1].anchor)
    other = silk._bounds_gap((x, y, x, y), labels[0].anchor)
    assert own <= other + 0.05


def test_a_boxed_in_part_is_left_unplaced_and_hidden():
    lb = _label("U1", 20, 15)
    walls = [box(15, 10, 25, 20).difference(box(*lb.anchor))]
    rows = silk.place(_scene([lb], pads=walls))
    assert rows == [{"reference": "U1", "placed": False}]
    assert silk.moves(rows) == [{"reference": "U1", "text": "Reference", "visible": False}]
    assert silk.moves(rows, hide_unplaced=False) == []


def test_smaller_size_only_when_the_full_size_does_not_fit():
    lb = _label("C7", 20, 15)
    # a slot above the part just wide enough for "C7" at 0.8, not at 1.0
    keep_out = [box(10, 10, 40, 30).difference(box(19.2, 13.5, 20.8, 14.55))]
    [row] = silk.place(_scene([lb], pads=keep_out), silk.Rules(rotate=False, pad_clearance=0.0))
    assert row["placed"] and row["size"] == 0.8


def test_shrinking_is_preferred_over_rotating():
    lb = _label("C7", 20, 15)
    # above: room for "C7" horizontal at 0.8 only; right: room for it vertical at full size
    slots = box(19.2, 13.5, 20.8, 14.55).union(box(20.85, 14.0, 22.1, 16.0))
    keep_out = [box(10, 10, 40, 30).difference(slots)]
    [row] = silk.place(_scene([lb], pads=keep_out), silk.Rules(pad_clearance=0.0))
    assert row["placed"] and row["size"] == 0.8 and row["rotation"] == 0


def test_references_limits_the_run():
    labels = [_label("R1", 10, 15), _label("R2", 30, 15)]
    rows = silk.place(_scene(labels), references=["R2"])
    assert [r["reference"] for r in rows] == ["R2"]


def test_text_stays_inside_the_board_edge():
    lb = _label("J1", 1.5, 1.0)
    [row] = silk.place(_scene([lb]))
    assert row["placed"]
    assert box(0.35, 0.35, 39.65, 29.65).contains(_box(row, lb))
