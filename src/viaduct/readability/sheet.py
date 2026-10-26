"""One .kicad_sch file read into the shapes the readability rules look at. Read-only.

All values are millimetres in schematic coordinates, where y grows downward. Library symbols
store y upward; placing a symbol flips y, then rotates by its angle (counter-clockwise on
screen) and mirrors (mirror x flips vertically, mirror y horizontally).

The file holds no font metrics, so text widths come from the advance of each character in
KiCad's stroke font (_ADVANCE, measured from KiCad 10.0.7), and a line is the font height
tall. That is exact for the default font; a sheet using an outline (TrueType) font is only
approximated by it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box

from viaduct import sexpr
from viaduct.geometry import arc_points
from viaduct.sexpr import Node

TOL = 1e-3                  # coordinates closer than this are the same point
LINE_SPACING = 1.62         # KiCad's interline pitch, in font heights
LABEL_OFFSET = 0.2          # local label text sits this many font heights above its wire

# character advance in the stroke font, in 21sts of the font width (newstroke's glyph grid)
_ADVANCE = {c: n for n, cs in {
    8: "`", 10: "!',.:;Iij", 11: "l", 12: "^ft", 13: "r", 14: "()[\\]{}", 15: "~",
    16: ' "*JT_vy°', 17: "Lksxz", 18: "?AFVYce", 19: "Eabdghnopqu", 20: "$0123456789SXZ|",
    21: "#BCDGKPR", 22: "/HNOQUwµ", 24: "%MWΩ", 26: "&+-<=>±", 27: "@", 28: "m",
}.items() for c in cs}
_ADVANCE_OTHER = 19         # characters outside the table
_MARKUP = re.compile(r"[~^_]\{([^}]*)\}")   # overbar, superscript, subscript


Pt = tuple[float, float]


@dataclass
class Wire:
    a: Pt
    b: Pt
    uuid: str
    bus: bool = False

    @property
    def line(self) -> LineString:
        return LineString([self.a, self.b])

    @property
    def length(self) -> float:
        return math.dist(self.a, self.b)

    @property
    def horizontal(self) -> bool:
        return abs(self.a[1] - self.b[1]) < TOL

    @property
    def vertical(self) -> bool:
        return abs(self.a[0] - self.b[0]) < TOL


@dataclass
class Text:
    """Visible text: a label, a free text or text box, or a field of a symbol or sheet."""
    kind: str               # label, global_label, hierarchical_label, directive_label, text, text_box, field
    text: str
    at: Pt
    box: Polygon
    size: float             # character height
    owner: str | None = None   # reference or sheet name a field belongs to
    name: str | None = None    # field name (Reference, Value, Sheetname, ...)
    uuid: str | None = None
    # a symbol field's stored layout, so it can be placed again (field_box)
    font: Pt = (1.27, 1.27)    # (height, width)
    justify: tuple[str, str] = ("left", "bottom")
    angle: float = 0.0         # as stored: relative to the symbol's orientation
    unit: int = 1

    @property
    def label(self) -> str:
        if self.kind == "field":
            return f"{self.owner} {self.name}"
        return f"{self.kind} {self.text}"


@dataclass
class Pin:
    """A connection point on a symbol or sheet: wires should meet it at tip, heading outward."""
    owner: str
    number: str
    name: str
    tip: Pt
    outward: Pt             # unit vector from the body toward the tip
    length: float = 0.0
    hidden: bool = False
    on_sheet: bool = False      # a hierarchical sheet pin rather than a symbol pin
    power: bool = False         # the pin of a power symbol or power flag: a net marker, like a label

    @property
    def label(self) -> str:
        return f"{self.owner} pin {self.number}" + (f" ({self.name})" if self.name not in ("", "~") else "")

    @property
    def line(self) -> LineString | Point:
        if self.length < TOL:
            return Point(self.tip)
        base = (self.tip[0] - self.outward[0] * self.length, self.tip[1] - self.outward[1] * self.length)
        return LineString([self.tip, base])


@dataclass
class Symbol:
    ref: str
    lib_id: str
    value: str
    at: Pt
    angle: float
    mirror: str | None
    power: bool
    body: Polygon | None     # bounding box of the drawing
    pins: list[Pin]
    uuid: str
    strokes: MultiLineString = field(default_factory=MultiLineString)   # every line drawn, pins included
    passive: bool = False    # set from the config's [readability.passives] before the rules run
    unit: int = 1

    @property
    def turn(self):
        return orientation(self.angle, self.mirror)


@dataclass
class SheetBox:
    """A hierarchical sheet placed on this sheet."""
    name: str
    file: str
    box: Polygon
    pins: list[Pin]
    uuid: str


@dataclass
class Sheet:
    path: Path
    wires: list[Wire] = field(default_factory=list)
    junctions: list[Pt] = field(default_factory=list)
    no_connects: list[Pt] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    sheets: list[SheetBox] = field(default_factory=list)

    @property
    def labels(self) -> list[Text]:
        return [t for t in self.texts if t.kind.endswith("label")]

    @property
    def pins(self) -> list[Pin]:
        return [p for s in self.symbols for p in s.pins if not p.hidden] + [p for s in self.sheets for p in s.pins]


# ---------- reading ----------

def read(path: str | Path) -> Sheet:
    path = Path(path)
    root = sexpr.parse(path.read_text(encoding="utf-8"))
    if root.name != "kicad_sch":
        raise ValueError(f"{path.name} is not a KiCad schematic")
    lib = {sexpr.unquote(s.items[1]): s for s in (root.first("lib_symbols") or Node()).lists("symbol")}
    sheet = Sheet(path)
    for n in root.lists():
        match n.name:
            case "wire" | "bus":
                pts = [_xy(p) for p in n.first("pts").lists("xy")]
                sheet.wires += [Wire(a, b, _uuid(n), n.name == "bus") for a, b in zip(pts, pts[1:])]
            case "junction":
                sheet.junctions.append(_xy(n.first("at")))
            case "no_connect":
                sheet.no_connects.append(_xy(n.first("at")))
            case "label" | "global_label" | "hierarchical_label" | "directive_label" | "text":
                if not _hidden(n):
                    sheet.texts.append(_label(n))
            case "text_box":
                sheet.texts.append(_text_box(n))
            case "symbol":
                _symbol(n, lib, sheet)
            case "sheet":
                _sheet(n, sheet)
    return sheet


def _num(atom: str) -> float:
    return float(sexpr.unquote(atom))


def _xy(n: Node) -> Pt:
    return _num(n.items[1]), _num(n.items[2])


def _angle(n: Node) -> float:
    return _num(n.items[3]) if len(n.items) > 3 else 0.0


def _uuid(n: Node) -> str:
    u = n.first("uuid")
    return u.atoms[0] if u else ""


def _hidden(n: Node) -> bool:
    eff = n.first("effects")
    for holder in (n, eff):
        if holder is None:
            continue
        h = holder.first("hide")
        if h is not None and h.atoms[:1] != ["no"]:
            return True
        if "hide" in holder.atoms:          # files before KiCad 9
            return True
    return False


def _font(n: Node) -> tuple[float, float]:
    """(height, width) of n's font."""
    size = (n.first("effects") or Node()).first("font")
    size = size.first("size") if size else None
    return (_num(size.items[1]), _num(size.items[2])) if size else (1.27, 1.27)


def _justify(n: Node) -> tuple[str, str]:
    j = (n.first("effects") or Node()).first("justify")
    atoms = j.atoms if j else []
    h = "left" if "left" in atoms else "right" if "right" in atoms else "center"
    v = "top" if "top" in atoms else "bottom" if "bottom" in atoms else "center"
    return h, v


# ---------- text boxes ----------

def plain(text: str) -> str:
    """Text as drawn, without KiCad's overbar, superscript and subscript markup."""
    return _MARKUP.sub(r"\1", text)


def text_width(line: str, width: float) -> float:
    return sum(_ADVANCE.get(c, _ADVANCE_OTHER) for c in line) * width / 21


def text_frame(text: str, height: float, width: float, justify: tuple[str, str]) -> tuple[float, float, float, float]:
    """Box of text in its own frame (x along the text, y down), relative to its anchor."""
    lines = plain(text).split("\n")
    w = max(text_width(line, width) for line in lines)
    h = height * (1 + (len(lines) - 1) * LINE_SPACING)
    x0 = {"left": 0.0, "right": -w, "center": -w / 2}[justify[0]]
    y0 = {"top": 0.0, "bottom": -h, "center": -h / 2}[justify[1]]
    return x0, y0, x0 + w, y0 + h


def _place(frame: tuple[float, float, float, float], at: Pt, angle: float) -> Polygon:
    """A frame box turned by angle (counter-clockwise on screen) about at, then moved there.
    KiCad never draws text upside down: 180 reads as 0 and 270 as 90, with the stored
    justification already the one that applies."""
    a = angle % 180
    poly = box(*frame)
    if a:
        poly = affinity.rotate(poly, -a, origin=(0, 0))
    return affinity.translate(poly, *at)


def _label(n: Node) -> Text:
    text = sexpr.unquote(n.items[1])
    at = n.first("at")
    font, justify, angle = _font(n), _justify(n), _angle(at)
    return Text(n.name, text, _xy(at), label_box(n.name, text, _xy(at), angle, font, justify), font[0],
                uuid=_uuid(n), font=font, justify=justify, angle=angle)


def label_justify(kind: str, angle: float) -> tuple[str, str]:
    """The justification KiCad gives a label turned to angle: the text runs away from the
    connection point, local labels above their wire, the others centred on it."""
    h = "right" if round(angle) % 360 in (180, 270) else "left"
    return h, "bottom" if kind in ("label", "text") else "center"


def label_box(kind: str, text: str, at: Pt, angle: float, font: Pt, justify: tuple[str, str] | None = None) -> Polygon:
    """Where a label or free text draws at angle; justify defaults to label_justify's."""
    h, w = font
    x0, y0, x1, y1 = text_frame(text, h, w, justify or label_justify(kind, angle))
    if kind == "label":
        y0, y1 = y0 - LABEL_OFFSET * h, y1 - LABEL_OFFSET * h
    elif kind != "text":
        # global and hierarchical labels draw a shape: text after an arrow, centred on the wire
        reach = (x1 - x0) + 1.5 * h
        x0, x1 = (0.0, reach) if x0 >= 0 else (-reach, 0.0)
        y0, y1 = -0.75 * h, 0.75 * h
    return _place((x0, y0, x1, y1), at, angle)


def _text_box(n: Node) -> Text:
    x, y = _xy(n.first("at"))
    sw, sh = _xy(n.first("size"))
    return Text("text_box", sexpr.unquote(n.items[1]), (x, y), box(x, y, x + sw, y + sh), _font(n)[0], uuid=_uuid(n))


# ---------- symbols ----------

def orientation(angle: float, mirror: str | None):
    """The screen-space turn and mirror of a placed symbol, as a function on offsets."""
    a = round(angle) % 360
    def turn(x: float, y: float) -> Pt:
        for _ in range(a // 90):
            x, y = y, -x                # 90 degrees counter-clockwise on screen (y down)
        if mirror == "x":
            y = -y
        elif mirror == "y":
            x = -x
        return x, y
    return turn


def _units(lib_symbol: Node, unit: int, style: int) -> list[Node]:
    """The library sub-symbols drawn for one unit and body style (0 means shared by all)."""
    out = []
    for sub in lib_symbol.lists("symbol"):
        try:
            u, s = (int(v) for v in sexpr.unquote(sub.items[1]).rsplit("_", 2)[1:])
        except ValueError:
            continue
        if u in (0, unit) and s in (0, style):
            out.append(sub)
    return out


def _graphic_lines(g: Node) -> list[Pt]:
    """The line a library graphic draws, as points (a closed shape repeats its first point)."""
    match g.name:
        case "rectangle":
            (x0, y0), (x1, y1) = _xy(g.first("start")), _xy(g.first("end"))
            return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        case "polyline" | "bezier":         # a Bézier as its control polygon
            return [_xy(p) for p in g.first("pts").lists("xy")]
        case "circle":
            (cx, cy), r = _xy(g.first("center")), _num(g.first("radius").items[1])
            return [(cx + r * math.cos(a * math.pi / 12), cy + r * math.sin(a * math.pi / 12)) for a in range(25)]
        case "arc":
            return arc_points(*(_xy(g.first(k)) for k in ("start", "mid", "end")))
    return []


def _symbol(n: Node, lib: dict[str, Node], sheet: Sheet) -> None:
    lib_id = sexpr.unquote(n.first("lib_id").items[1])
    name = n.first("lib_name")
    ls = lib.get(sexpr.unquote(name.items[1]) if name else lib_id)
    at = n.first("at")
    pos, angle = _xy(at), _angle(at)
    mirror = n.first("mirror").atoms[0] if n.first("mirror") else None
    unit = int(n.first("unit").atoms[0]) if n.first("unit") else 1
    style = n.first("body_style") or n.first("convert")
    style = int(style.atoms[0]) if style else 1
    turn = orientation(angle, mirror)

    def place(p: Pt) -> Pt:             # library point (y up) to sheet point
        dx, dy = turn(p[0], -p[1])
        return pos[0] + dx, pos[1] + dy

    props = {sexpr.unquote(p.items[1]): p for p in n.lists("property")}
    ref = sexpr.unquote(props["Reference"].items[2]) if "Reference" in props else "?"
    value = sexpr.unquote(props["Value"].items[2]) if "Value" in props else ""
    body, pins, lines = None, [], []
    if ls is not None:
        subs = _units(ls, unit, style)
        lines = [[place(p) for p in _graphic_lines(g)] for sub in subs for g in sub.lists()]
        lines = [line for line in lines if len(line) > 1]
        if lines:
            xs, ys = [c[0] for line in lines for c in line], [c[1] for line in lines for c in line]
            body = box(min(xs), min(ys), max(xs), max(ys))
        for sub in subs:
            for p in sub.lists("pin"):
                pat = p.first("at")
                th = math.radians(_angle(pat))
                ox, oy = turn(-math.cos(th), math.sin(th))   # outward: away from the body, then y flipped
                number = sexpr.unquote(p.first("number").items[1]) if p.first("number") else ""
                pname = sexpr.unquote(p.first("name").items[1]) if p.first("name") else ""
                length = _num(p.first("length").items[1]) if p.first("length") else 0.0
                tip = place(_xy(pat))
                if length < TOL and body is not None:
                    # a zero-length pin's angle is often arbitrary (old power symbols): it
                    # points away from the body, along the axis it leaves by
                    c = body.centroid
                    dx, dy = tip[0] - c.x, tip[1] - c.y
                    if abs(dx) > TOL or abs(dy) > TOL:
                        ox, oy = (math.copysign(1, dx), 0) if abs(dx) > abs(dy) else (0, math.copysign(1, dy))
                pins.append(Pin(ref, number, pname, tip, (round(ox, 9), round(oy, 9)), length, _hidden(p)))
    power = ls is not None and ls.first("power") is not None
    for pin in pins:
        pin.power = power
    strokes = MultiLineString(lines + [list(p.line.coords) for p in pins if p.length >= TOL and not p.hidden])
    sheet.symbols.append(Symbol(ref, lib_id, value, pos, angle, mirror, power, body, pins, _uuid(n), strokes,
                                unit=unit))

    for fname, p in props.items():
        text = sexpr.unquote(p.items[2])
        if _hidden(p) or text in ("", "~"):
            continue
        font, fat, justify = _font(p), p.first("at"), _justify(p)
        anchor, a = _xy(fat), _angle(fat)
        sheet.texts.append(Text("field", text, anchor, field_box(text, font, justify, a, anchor, turn), font[0],
                                owner=ref, name=fname, font=font, justify=justify, angle=a, unit=unit))


def field_box(text: str, font: Pt, justify: tuple[str, str], angle: float, at: Pt, turn) -> Polygon:
    """Where a symbol field draws: its frame turned by the field's own stored angle, then by
    the symbol's orientation (turn), anchored at at."""
    frame = _place(text_frame(text, font[0], font[1], justify), (0, 0), angle)
    corners = [turn(x, y) for x, y in frame.exterior.coords]
    xs, ys = [c[0] + at[0] for c in corners], [c[1] + at[1] for c in corners]
    return box(min(xs), min(ys), max(xs), max(ys))


# ---------- hierarchical sheets ----------

def _sheet(n: Node, sheet: Sheet) -> None:
    x, y = _xy(n.first("at"))
    w, h = _xy(n.first("size"))
    rect = box(x, y, x + w, y + h)
    props = {sexpr.unquote(p.items[1]): p for p in n.lists("property")}
    name = sexpr.unquote(props["Sheetname"].items[2]) if "Sheetname" in props else "?"
    file = sexpr.unquote(props["Sheetfile"].items[2]) if "Sheetfile" in props else ""
    pins = []
    for p in n.lists("pin"):
        px, py = _xy(p.first("at"))
        # outward is away from the edge the pin sits on
        edges = {(-1, 0): abs(px - x), (1, 0): abs(px - x - w), (0, -1): abs(py - y), (0, 1): abs(py - y - h)}
        pins.append(Pin(name, sexpr.unquote(p.items[1]), "", (px, py), min(edges, key=edges.get), on_sheet=True))
    sheet.sheets.append(SheetBox(name, file, rect, pins, _uuid(n)))
    for fname, p in props.items():
        text = sexpr.unquote(p.items[2])
        if _hidden(p) or not text:
            continue
        hh, ww = _font(p)
        fat = p.first("at")
        sheet.texts.append(Text("field", text, _xy(fat), _place(text_frame(text, hh, ww, _justify(p)), _xy(fat), _angle(fat)),
                                hh, owner=name, name=fname))
