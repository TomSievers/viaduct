from kipy.proto.board import board_types_pb2 as bt

from viaduct import kicad_io


def test_translate_moves_points_but_not_text_size():
    t = bt.BoardText()
    t.text.position.x_nm, t.text.position.y_nm = 1_000_000, 2_000_000
    t.text.attributes.size.x_nm = t.text.attributes.size.y_nm = 800_000
    kicad_io.translate(t, 500_000, -250_000)
    assert (t.text.position.x_nm, t.text.position.y_nm) == (1_500_000, 1_750_000)
    assert (t.text.attributes.size.x_nm, t.text.attributes.size.y_nm) == (800_000, 800_000)


def test_translate_reaches_every_point_of_a_polygon():
    s = bt.BoardGraphicShape()
    pwh = s.shape.polygon.polygons.add()
    for x, y in ((0, 0), (10, 0), (10, 10)):
        n = pwh.outline.nodes.add()
        n.point.x_nm, n.point.y_nm = x, y
    hole = pwh.holes.add().nodes.add()
    hole.arc.start.x_nm = hole.arc.mid.x_nm = hole.arc.end.x_nm = 5
    kicad_io.translate(s, 1, 2)
    assert [(n.point.x_nm, n.point.y_nm) for n in pwh.outline.nodes] == [(1, 2), (11, 2), (11, 12)]
    assert (hole.arc.start.x_nm, hole.arc.mid.y_nm, hole.arc.end.x_nm) == (6, 2, 6)


def test_translate_moves_a_dimension_and_its_text():
    d = bt.Dimension()
    d.aligned.start.x_nm, d.aligned.end.x_nm = 0, 10
    d.text.position.y_nm = 3
    d.text.attributes.size.x_nm = 7
    kicad_io.translate(d, 4, 5)
    assert (d.aligned.start.x_nm, d.aligned.end.x_nm, d.aligned.start.y_nm) == (4, 14, 5)
    assert d.text.position.y_nm == 8 and d.text.attributes.size.x_nm == 7


def test_position_takes_absolute_or_relative_not_both():
    errors = []
    assert kicad_io._position({"x": 1, "y": 2}, "m", errors) == ("abs", (1.0, 2.0))
    assert kicad_io._position({"dx": 0.5}, "m", errors) == ("rel", (0.5, 0.0))
    assert kicad_io._position({"rotation": 90}, "m", errors) == (None, None)
    assert errors == []
    kicad_io._position({"x": 1, "dx": 1}, "a", errors)
    kicad_io._position({"x": 1}, "b", errors)
    assert [e.split(":")[0] for e in errors] == ["a", "b"]


def test_style_sets_rotation_size_and_thickness_and_reports_bad_values():
    t = bt.BoardText().text
    errors = []
    kicad_io._style(t, {"rotation": 45, "size": 0.8, "thickness": 0.12}, "m", errors)
    assert (t.attributes.angle.value_degrees, t.attributes.size.x_nm, t.attributes.stroke_width.value_nm) == \
        (45.0, 800_000, 120_000)
    kicad_io._style(t, {"size": 0, "thickness": True}, "m", errors)
    assert len(errors) == 2 and t.attributes.size.x_nm == 800_000
