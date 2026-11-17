"""Read a massing from a Rhino .3dm file with rhino3dm (no Rhino license needed).

Conventions:
- Each object's layer name is its department (a "department" user string overrides it).
- Objects are closed planar curves (floor outlines) or closed polysurfaces/extrusions
  (floor-plate volumes). For volumes the outline is taken from the edges at the bottom.
- Plates whose bottoms lie within ``min_ftf`` of each other are put on one level.
- Level height is the gap to the next level; the top level uses its volume height,
  or ``default_height`` for curves.
Model units are converted to metres.
"""
from __future__ import annotations

from pathlib import Path

import rhino3dm as r3
from shapely.geometry import Polygon

from .massing import Department, Level, Massing, Plate

_UNIT_TO_M = {
    r3.UnitSystem.Millimeters: 0.001, r3.UnitSystem.Centimeters: 0.01, r3.UnitSystem.Meters: 1.0,
    r3.UnitSystem.Inches: 0.0254, r3.UnitSystem.Feet: 0.3048,
}


def _curve_points(c) -> list[tuple[float, float, float]]:
    if isinstance(c, r3.PolylineCurve):
        pts = [c.Point(i) for i in range(c.PointCount)]
    elif isinstance(c, r3.LineCurve):
        pts = [c.Line.From, c.Line.To]
    else:
        nc = c.ToNurbsCurve()
        if nc.Degree == 1:
            pts = [nc.Points[i].Location if hasattr(nc.Points[i], "Location") else nc.Points[i]
                   for i in range(len(nc.Points))]
            pts = [r3.Point3d(p.X, p.Y, p.Z) for p in pts]
        else:
            t0, t1 = c.Domain.T0, c.Domain.T1
            pts = [c.PointAt(t0 + (t1 - t0) * i / 64) for i in range(65)]
    return [(p.X, p.Y, p.Z) for p in pts]


def _ring(pts):
    if len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) < 1e-6 and abs(pts[0][1] - pts[-1][1]) < 1e-6:
        pts = pts[:-1]
    return [(p[0], p[1]) for p in pts]


def _chain(segments, tol):
    """Chain 2D segments into one closed loop of points."""
    segs = list(segments)
    if not segs:
        return []
    loop = list(segs.pop(0))
    while segs:
        end = loop[-1]
        for i, s in enumerate(segs):
            if abs(s[0][0] - end[0]) < tol and abs(s[0][1] - end[1]) < tol:
                loop.extend(s[1:]); segs.pop(i); break
            if abs(s[-1][0] - end[0]) < tol and abs(s[-1][1] - end[1]) < tol:
                loop.extend(list(reversed(s))[1:]); segs.pop(i); break
        else:
            break
    return loop


def _brep_outline(brep, tol):
    bb = brep.GetBoundingBox()
    zmin = bb.Min.Z
    segs = []
    for i in range(len(brep.Edges)):
        e = brep.Edges[i]
        pts = _curve_points(e)
        if all(abs(p[2] - zmin) < tol for p in pts):
            segs.append([(p[0], p[1]) for p in pts])
    return _ring(_chain(segs, tol)), zmin, bb.Max.Z - zmin


def read_3dm(path: str | Path, *, min_ftf: float = 2.4, default_height: float = 4.3,
             departments: dict[str, Department] | None = None, **massing_kwargs) -> Massing:
    f = r3.File3dm.Read(str(path))
    if f is None:
        raise ValueError(f"cannot read {path}")
    scale = _UNIT_TO_M.get(f.Settings.ModelUnitSystem, 1.0)
    tol = max(f.Settings.ModelAbsoluteTolerance, 1e-4) * 10
    layers = {i: f.Layers[i].Name for i in range(len(f.Layers))}
    raw = []  # (dept, polygon, z, height)
    for obj in f.Objects:
        g, a = obj.Geometry, obj.Attributes
        dept = a.GetUserString("department") or layers.get(a.LayerIndex, "Default")
        if isinstance(g, r3.Extrusion):
            g = g.ToBrep(True)
        if isinstance(g, r3.Brep):
            ring, z, h = _brep_outline(g, tol)
        elif isinstance(g, r3.Curve) and g.IsClosed:
            pts = _curve_points(g)
            ring, z, h = _ring(pts), pts[0][2], None
        else:
            continue
        if len(ring) >= 3 and Polygon(ring).is_valid:
            raw.append((dept, [(x * scale, y * scale) for x, y in ring], z * scale, h * scale if h else None))
    if not raw:
        raise ValueError("no floor plates found (closed planar curves or closed volumes)")
    raw.sort(key=lambda r: r[2])
    groups: list[list] = []
    for r in raw:
        if groups and r[2] - groups[-1][0][2] < min_ftf:
            groups[-1].append(r)
        else:
            groups.append([r])
    levels = []
    for i, grp in enumerate(groups):
        z = grp[0][2]
        if i + 1 < len(groups):
            h = groups[i + 1][0][2] - z
        else:
            h = max((r[3] for r in grp if r[3]), default=default_height)
        levels.append(Level(name=f"L{i + 1}", elevation=z, height=h,
                            plates=[Plate(department=r[0], polygon=r[1]) for r in grp]))
    names = sorted({r[0] for r in raw})
    departments = departments or {}
    for n in names:
        departments.setdefault(n, Department(program=n))
    return Massing(departments=departments, levels=levels, **massing_kwargs)
