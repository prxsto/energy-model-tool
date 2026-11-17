"""Perimeter/core zoning of department floor plates.

Each plate is split into mitred perimeter zones along its exterior edges and one core
zone. Edges shared with another plate on the same level are interior and get no
perimeter zone. Perimeter pieces facing the same compass direction are merged, so a
plate yields at most one zone per orientation (plus pieces that do not touch).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from .massing import Level, Massing

TOL = 0.01  # m


@dataclass
class Zone:
    name: str
    department: str
    level: str
    kind: str  # "perimeter" | "core" | "whole"
    orientation: str | None
    polygon: Polygon
    z: float
    height: float
    multiplier: int = 1
    notes: list[str] = field(default_factory=list)

    @property
    def area(self) -> float:
        return self.polygon.area


def bearing_to_orientation(bearing_deg: float) -> str:
    b = bearing_deg % 360.0
    if b >= 315 or b < 45:
        return "N"
    if b < 135:
        return "E"
    if b < 225:
        return "S"
    return "W"


def edge_orientation(p0, p1, north_deg: float) -> str:
    """Compass orientation of the outward normal of a CCW polygon edge."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    nx, ny = dy, -dx  # outward for CCW
    model_bearing = math.degrees(math.atan2(nx, ny))  # 0 = +Y, clockwise positive
    return bearing_to_orientation(model_bearing - north_deg)


def _clean(poly: Polygon) -> list[tuple[float, float]]:
    poly = orient(poly.simplify(TOL, preserve_topology=True), sign=1.0)
    pts = list(poly.exterior.coords)[:-1]
    out = []
    n = len(pts)
    for i in range(n):  # drop collinear vertices
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-6:
            out.append(b)
    return out


def _intersect(p, d, q, e):
    """Intersection of lines p + t d and q + s e, or None if parallel."""
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-9:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return (p[0] + t * d[0], p[1] + t * d[1])


def split_plate(poly: Polygon, depth: float, exterior: list[bool] | None = None):
    """Return (core polygon or None, [(edge index, perimeter quad)], vertex list).

    Raises ValueError when the plate is too narrow for the requested depth.
    """
    pts = _clean(poly)
    n = len(pts)
    if exterior is None:
        exterior = [True] * n
    offs, dirs = [], []
    for i in range(n):
        p0, p1 = pts[i], pts[(i + 1) % n]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        L = math.hypot(dx, dy)
        nx, ny = -dy / L, dx / L  # inward for CCW
        d = depth if exterior[i] else 0.0
        offs.append((p0[0] + nx * d, p0[1] + ny * d))
        dirs.append((dx / L, dy / L))
    inner = []
    for j in range(n):
        q = _intersect(offs[j - 1], dirs[j - 1], offs[j], dirs[j])
        inner.append(q if q is not None else offs[j])
    core = Polygon(inner)
    ok = core.is_valid and core.area > TOL and core.exterior.is_ccw
    if ok:
        # every inner edge must keep the direction of its outer edge, and stay inside
        for i in range(n):
            a, b = inner[i], inner[(i + 1) % n]
            if (b[0] - a[0]) * dirs[i][0] + (b[1] - a[1]) * dirs[i][1] <= TOL:
                ok = False
                break
        ok = ok and poly.buffer(TOL).contains(core)
    if not ok:
        raise ValueError("plate too narrow for perimeter depth")
    quads = []
    for i in range(n):
        if not exterior[i]:
            continue
        q = Polygon([pts[i], pts[(i + 1) % n], inner[(i + 1) % n], inner[i]])
        if q.is_valid and q.area > TOL:
            quads.append((i, q))
    return core, quads, pts


def _exterior_flags(pts, others: list[Polygon]) -> list[bool]:
    if not others:
        return [True] * len(pts)
    shared = unary_union([o.boundary for o in others]).buffer(0.05)
    flags = []
    for i in range(len(pts)):
        seg = LineString([pts[i], pts[(i + 1) % len(pts)]])
        flags.append(seg.intersection(shared).length < 0.5 * seg.length)
    return flags


def zone_level(level: Level, massing: Massing) -> list[Zone]:
    zones: list[Zone] = []
    shapes = [pl.shape() for pl in level.plates]
    counts: dict[str, int] = {}
    for k, (pl, shp) in enumerate(zip(level.plates, shapes)):
        dept = massing.departments[pl.department]
        counts[pl.department] = counts.get(pl.department, 0) + 1
        tag = f"{level.name}_{pl.department}" + (f"{counts[pl.department]}" if counts[pl.department] > 1 else "")
        common = dict(department=pl.department, level=level.name, z=level.elevation,
                      height=level.height, multiplier=level.multiplier)
        pts = _clean(shp)
        flags = _exterior_flags(pts, [s for j, s in enumerate(shapes) if j != k])
        if dept.perimeter_depth <= 0 or not any(flags):
            zones.append(Zone(f"{tag}_Whole", kind="whole", orientation=None, polygon=Polygon(pts), **common))
            continue
        try:
            core, quads, pts = split_plate(Polygon(pts), dept.perimeter_depth, flags)
        except ValueError as exc:
            z = Zone(f"{tag}_Whole", kind="whole", orientation=None, polygon=Polygon(pts), **common)
            z.notes.append(str(exc))
            zones.append(z)
            continue
        by_dir: dict[str, list[Polygon]] = {}
        for i, q in quads:
            o = edge_orientation(pts[i], pts[(i + 1) % len(pts)], massing.north_deg)
            by_dir.setdefault(o, []).append(q)
        for o in "NESW":
            if o not in by_dir:
                continue
            merged = unary_union(by_dir[o])
            parts = [merged] if merged.geom_type == "Polygon" else list(merged.geoms)
            for m, part in enumerate(parts, 1):
                suffix = f"_{o}" + (f"{m}" if len(parts) > 1 else "")
                part = Polygon(_clean(part))
                zones.append(Zone(f"{tag}_Perim{suffix}", kind="perimeter", orientation=o, polygon=part, **common))
        zones.append(Zone(f"{tag}_Core", kind="core", orientation=None, polygon=Polygon(_clean(core)), **common))
    return zones


def zone_massing(massing: Massing) -> list[Zone]:
    return [z for lv in massing.levels for z in zone_level(lv, massing)]


def summary(zones: list[Zone]) -> str:
    lines = [f"{'zone':38s} {'kind':9s} {'dir':3s} {'area m2':>9s}"]
    for z in zones:
        lines.append(f"{z.name:38s} {z.kind:9s} {z.orientation or '-':3s} {z.area * z.multiplier:9.1f}"
                     + (f"  ({'; '.join(z.notes)})" if z.notes else ""))
    total = sum(z.area * z.multiplier for z in zones)
    lines.append(f"{'total':52s} {total:9.1f}")
    return "\n".join(lines)
