"""Shape builders shared by the KiCad reader and the plan checker (all mm)."""

from __future__ import annotations

import math

from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry

ARC_STEP_DEG = 10.0


def track(x1: float, y1: float, x2: float, y2: float, width: float) -> BaseGeometry:
    if (x1, y1) == (x2, y2):
        return Point(x1, y1).buffer(width / 2)
    return LineString([(x1, y1), (x2, y2)]).buffer(width / 2)


def via(x: float, y: float, diameter: float) -> BaseGeometry:
    return Point(x, y).buffer(diameter / 2)


def arc_points(start, mid, end) -> list[tuple[float, float]]:
    """Points along the circular arc through start, mid and end."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:                     # collinear: a straight line
        return [start, end]
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))
    # sweep from a0 to a1 in the direction that passes through am
    ccw = (am - a0) % (2 * math.pi) < (a1 - a0) % (2 * math.pi)
    sweep = (a1 - a0) % (2 * math.pi) if ccw else -((a0 - a1) % (2 * math.pi))
    n = max(2, int(abs(math.degrees(sweep)) / ARC_STEP_DEG) + 1)
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def polygon(outline: list[tuple[float, float]], holes: list[list[tuple[float, float]]] = ()) -> BaseGeometry:
    p = Polygon(outline, holes)
    return p if p.is_valid else p.buffer(0)
