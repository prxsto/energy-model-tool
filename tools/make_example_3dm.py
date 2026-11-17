"""Write examples/example_hospital.3dm from the example massing JSON (closed extrusions)."""
import sys
from pathlib import Path
import rhino3dm as r3
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hcem import massing

m = massing.load(Path(__file__).resolve().parents[1] / "examples/example_hospital.json")
f = r3.File3dm()
f.Settings.ModelUnitSystem = r3.UnitSystem.Meters
idx = {}
for name in m.departments:
    layer = r3.Layer(); layer.Name = name
    idx[name] = f.Layers.Add(layer)
for lv in m.levels:
    for pl in lv.plates:
        pts = [r3.Point3d(x, y, lv.elevation) for x, y in pl.polygon] + [r3.Point3d(pl.polygon[0][0], pl.polygon[0][1], lv.elevation)]
        crv = r3.PolylineCurve(pts)
        ext = r3.Extrusion.Create(crv, -lv.height, True)  # negative: extrude up for CCW curves
        if ext.GetBoundingBox().Min.Z < lv.elevation - 1e-6:
            ext = r3.Extrusion.Create(crv, lv.height, True)
        attr = r3.ObjectAttributes(); attr.LayerIndex = idx[pl.department]
        f.Objects.AddExtrusion(ext, attr)
out = Path(__file__).resolve().parents[1] / "examples/example_hospital.3dm"
f.Write(str(out), 7)
print("wrote", out)
