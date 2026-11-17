"""Build an OpenStudio model from a massing: geometry, envelope, loads, schedules, HVAC."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import openstudio as osd
from shapely.geometry import LineString
from shapely.ops import unary_union

from . import library
from .massing import Massing
from .zoning import Zone, bearing_to_orientation, zone_massing

M = osd.model
FILMS = {"wall": 0.12 + 0.04, "roof": 0.10 + 0.04, "floor": 0.16 + 0.04}  # Rsi + Rse, m2K/W


@dataclass
class BuildReport:
    zones: list[Zone]
    warnings: list[str] = field(default_factory=list)
    window_area: dict[str, float] = field(default_factory=dict)
    wall_area: dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------------------- schedules
_DAYS = {"Wkdy": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"), "Sat": ("Saturday",),
         "Sun": ("Sunday",), "Wknd": ("Saturday", "Sunday"), "Mon": ("Monday",), "Tue": ("Tuesday",),
         "Wed": ("Wednesday",), "Thu": ("Thursday",), "Fri": ("Friday",)}


def _fill_day(day: "M.ScheduleDay", typ: str, values: list[float]) -> None:
    day.clearValues()
    if typ == "Constant" or len(values) == 1:
        day.addValue(osd.Time(0, 24, 0, 0), float(values[0]))
        return
    for h, v in enumerate(values):  # add only where the value changes
        if h == 23 or values[h + 1] != v:
            day.addValue(osd.Time(0, h + 1, 0, 0), float(v))


_LIMITS = {"fraction": ("Fraction", 0.0, 1.0, "Continuous", "Dimensionless"),
           "temperature": ("Temperature", -60.0, 200.0, "Continuous", "Temperature"),
           "activity": ("Activity Level", 0.0, 2000.0, "Continuous", "ActivityLevel")}


class ScheduleCache:
    def __init__(self, model):
        self.model, self.cache, self.limits = model, {}, {}

    def _limits(self, kind: str):
        if kind not in self.limits:
            name, lo, hi, num, unit = _LIMITS[kind]
            lim = M.ScheduleTypeLimits(self.model)
            lim.setName(name); lim.setLowerLimitValue(lo); lim.setUpperLimitValue(hi)
            lim.setNumericType(num); lim.setUnitType(unit)
            self.limits[kind] = lim
        return self.limits[kind]

    def get(self, name: str, kind: str = "fraction") -> "M.ScheduleRuleset":
        if name in self.cache:
            return self.cache[name]
        rows = library.schedules()[name]
        sch = M.ScheduleRuleset(self.model)
        sch.setName(name)
        sch.setScheduleTypeLimits(self._limits(kind))
        for r in rows:
            for dt in r["day_types"]:
                if dt == "Default":
                    _fill_day(sch.defaultDaySchedule(), r["type"], r["values"])
                elif dt == "SmrDsn":
                    d = M.ScheduleDay(self.model); d.setScheduleTypeLimits(self._limits(kind)); _fill_day(d, r["type"], r["values"])
                    sch.setSummerDesignDaySchedule(d)
                elif dt == "WntrDsn":
                    d = M.ScheduleDay(self.model); d.setScheduleTypeLimits(self._limits(kind)); _fill_day(d, r["type"], r["values"])
                    sch.setWinterDesignDaySchedule(d)
                elif dt in _DAYS:
                    rule = M.ScheduleRule(sch)
                    rule.setName(f"{name} {dt}")
                    _fill_day(rule.daySchedule(), r["type"], r["values"])
                    for wd in _DAYS[dt]:
                        getattr(rule, f"setApply{wd}")(True)
                # "Hol" and other day types are not used
        self.cache[name] = sch
        return sch


# ----------------------------------------------------------------------------- envelope
def _material(model, name, k, thick, rho, cp, rough="MediumRough"):
    m = M.StandardOpaqueMaterial(model, rough, thick, k, rho, cp)
    m.setName(name)
    return m


def _opaque(model, name, u_target, films, layers):
    """Layers: list of (name, k, thickness, rho, cp); one with thickness None is sized to hit U."""
    r_fixed = films + sum(t / k for _, k, t, _, _ in layers if t is not None)
    mats = osd.model.MaterialVector()
    made = {}
    for nm, k, t, rho, cp in layers:
        if t is None:
            t = max((1.0 / u_target - r_fixed) * k, 0.003)
        if nm not in made:  # reuse repeated layers so symmetric assemblies stay symmetric
            made[nm] = _material(model, f"{name} {nm}", k, t, rho, cp)
        mats.append(made[nm])
    c = M.Construction(model)
    c.setName(name)
    c.setLayers(mats)
    return c


def constructions(model, climate_zone: str) -> dict:
    env = library.envelope(climate_zone)
    cz = climate_zone
    gyp = ("Gypsum", 0.16, 0.0159, 800, 1090)
    ins = ("Insulation", 0.045, None, 43, 1210)
    out = {
        "wall": _opaque(model, f"Ext Wall SteelFramed CZ{cz}", env["ExteriorWall:SteelFramed"]["u_w_m2k"],
                        FILMS["wall"], [("Cladding", 0.9, 0.02, 1900, 840), ins, gyp]),
        "roof": _opaque(model, f"Roof IEAD CZ{cz}", env["ExteriorRoof:IEAD"]["u_w_m2k"], FILMS["roof"],
                        [("Membrane", 0.16, 0.0095, 1121, 1460), ins, ("Metal Deck", 45.0, 0.0015, 7680, 418)]),
        "exposed_floor": _opaque(model, f"Exposed Floor Mass CZ{cz}", env["ExteriorFloor:Mass"]["u_w_m2k"],
                                 FILMS["floor"], [("Concrete", 1.31, 0.10, 2240, 836), ins]),
        "int_floor": _opaque(model, "Interior Floor", 2.0, 0.22, [("Concrete", 1.31, 0.10, 2240, 836)]),
        "int_wall": _opaque(model, "Interior Wall", 2.0, 0.24, [gyp, ("Air", 0.15, 0.09, 1.2, 1006), gyp]),
    }
    glz = M.SimpleGlazing(model, env["ExteriorWindow:Fixed"]["u_w_m2k"], env["ExteriorWindow:Fixed"]["shgc"])
    glz.setVisibleTransmittance(0.45)
    win = M.Construction(model)
    win.setName(f"Window Fixed CZ{cz}")
    v = osd.model.MaterialVector(); v.append(glz); win.setLayers(v)
    out["window"] = win
    out["f_factor"] = env["GroundContactFloor:Unheated"]["f_w_mk"]
    return out


# ----------------------------------------------------------------------------- build
def _orientation_of(surface, north_deg: float) -> str:
    n = surface.outwardNormal()
    return bearing_to_orientation(math.degrees(math.atan2(n.x(), n.y())) - north_deg)


def build(massing: Massing, epw: str | None = None, programs_file=None) -> tuple["M.Model", BuildReport]:
    model = M.Model()
    report = BuildReport(zones=zone_massing(massing))
    progs = library.programs(programs_file)
    for d in massing.departments.values():
        if d.program not in progs:
            raise KeyError(f"program '{d.program}' not found; available: {sorted(progs)}")

    model.getBuilding().setName(massing.name)
    model.getBuilding().setNorthAxis((-massing.north_deg) % 360.0)
    if epw:
        osd.model.WeatherFile.setWeatherFile(model, osd.EpwFile(osd.toPath(epw)))
    ts = model.getTimestep(); ts.setNumberOfTimestepsPerHour(4)
    sc = model.getSimulationControl()
    sc.setDoZoneSizingCalculation(False); sc.setDoSystemSizingCalculation(False)
    sc.setDoPlantSizingCalculation(False); sc.setRunSimulationforSizingPeriods(False)
    sc.setRunSimulationforWeatherFileRunPeriods(True)

    cons = constructions(model, massing.climate_zone)
    dcs = M.DefaultConstructionSet(model); dcs.setName("hcem constructions")
    ext = M.DefaultSurfaceConstructions(model)
    ext.setWallConstruction(cons["wall"]); ext.setRoofCeilingConstruction(cons["roof"])
    ext.setFloorConstruction(cons["exposed_floor"])
    intr = M.DefaultSurfaceConstructions(model)
    intr.setWallConstruction(cons["int_wall"]); intr.setRoofCeilingConstruction(cons["int_floor"])
    intr.setFloorConstruction(cons["int_floor"])
    sub = M.DefaultSubSurfaceConstructions(model); sub.setFixedWindowConstruction(cons["window"])
    sub.setOperableWindowConstruction(cons["window"])
    dcs.setDefaultExteriorSurfaceConstructions(ext); dcs.setDefaultInteriorSurfaceConstructions(intr)
    dcs.setDefaultExteriorSubSurfaceConstructions(sub)
    model.getBuilding().setDefaultConstructionSet(dcs)

    scheds = ScheduleCache(model)
    space_types: dict[str, M.SpaceType] = {}
    spaces = osd.model.SpaceVector()
    zone_of_space = {}
    for z in report.zones:
        dept = massing.departments[z.department]
        bl = library.zone_blend(dept.program, z.kind, progs)
        pts = osd.Point3dVector()
        for x, y in reversed(list(z.polygon.exterior.coords)[:-1]):  # clockwise
            pts.append(osd.Point3d(x, y, z.z))
        sp = M.Space.fromFloorPrint(pts, z.height, model)
        if not sp.is_initialized():
            report.warnings.append(f"{z.name}: could not create space")
            continue
        sp = sp.get(); sp.setName(z.name)
        tz = M.ThermalZone(model); tz.setName(f"TZ {z.name}"); tz.setMultiplier(z.multiplier)
        sp.setThermalZone(tz)
        st = space_types.get(bl.name)
        if st is None:
            st = _space_type(model, bl, scheds)
            space_types[bl.name] = st
        sp.setSpaceType(st)
        dsoa = M.DesignSpecificationOutdoorAir(model)
        dsoa.setName(f"OA {z.name}")
        dsoa.setOutdoorAirFlowperFloorArea(bl.outdoor_air_per_area(z.height))
        sp.setDesignSpecificationOutdoorAir(dsoa)
        tstat = M.ThermostatSetpointDualSetpoint(model)
        tstat.setHeatingSetpointTemperatureSchedule(scheds.get(bl.schedules["heating_setpoint"], "temperature"))
        tstat.setCoolingSetpointTemperatureSchedule(scheds.get(bl.schedules["cooling_setpoint"], "temperature"))
        tz.setThermostatSetpointDualSetpoint(tstat)
        spaces.append(sp)
        zone_of_space[z.name] = z

    M.intersectSurfaces(spaces)
    M.matchSurfaces(spaces)

    _ground_floors(model, spaces, zone_of_space, cons["f_factor"], report)
    _windows(spaces, massing, zone_of_space, report)
    _hvac(model, massing)

    out = model.getOutputControlFiles(); out.setOutputSQLite(True)
    M.OutputVariable("Zone Ideal Loads Supply Air Total Heating Energy", model).setReportingFrequency("Monthly")
    return model, report


def _space_type(model, bl: library.Blend, scheds: ScheduleCache):
    st = M.SpaceType(model); st.setName(bl.name)
    L = bl.loads
    if L["lighting_w_m2"] > 0:
        d = M.LightsDefinition(model); d.setName(f"{bl.name} Lights")
        d.setWattsperSpaceFloorArea(L["lighting_w_m2"]); d.setFractionRadiant(0.7); d.setFractionVisible(0.2)
        x = M.Lights(d); x.setSpaceType(st); x.setSchedule(scheds.get(bl.schedules["lighting"]))
    if L["equipment_w_m2"] > 0:
        d = M.ElectricEquipmentDefinition(model); d.setName(f"{bl.name} Equipment")
        d.setWattsperSpaceFloorArea(L["equipment_w_m2"]); d.setFractionRadiant(0.5)
        x = M.ElectricEquipment(d); x.setSpaceType(st); x.setSchedule(scheds.get(bl.schedules["equipment"]))
    if L["people_per_m2"] > 0:
        d = M.PeopleDefinition(model); d.setName(f"{bl.name} People")
        d.setPeopleperSpaceFloorArea(L["people_per_m2"])
        x = M.People(d); x.setSpaceType(st)
        x.setNumberofPeopleSchedule(scheds.get(bl.schedules["occupancy"]))
        x.setActivityLevelSchedule(scheds.get(bl.schedules["activity"], "activity"))
    return st


def _ground_floors(model, spaces, zone_of_space, f_factor, report):
    """Ground-contact floors use the F-factor method with each zone's exposed perimeter."""
    ground_zones = [z for z in zone_of_space.values() if abs(z.z) < 1e-6]
    if not ground_zones:
        return
    footprint = unary_union([z.polygon for z in ground_zones])
    outline = footprint.boundary
    for sp in spaces:
        z = zone_of_space[sp.nameString()]
        for srf in sp.surfaces():
            if srf.surfaceType() != "Floor" or srf.outsideBoundaryCondition() != "Ground":
                continue
            ring = LineString([(v.x(), v.y()) for v in srf.vertices()] + [(srf.vertices()[0].x(), srf.vertices()[0].y())])
            exposed = ring.intersection(outline.buffer(0.05)).length
            c = M.FFactorGroundFloorConstruction(model, f_factor, srf.grossArea(), max(exposed, 0.0))
            c.setName(f"F-factor {sp.nameString()}")
            srf.setOutsideBoundaryCondition("GroundFCfactorMethod")
            srf.setConstruction(c)


def _windows(spaces, massing: Massing, zone_of_space, report: BuildReport):
    for sp in spaces:
        z = zone_of_space[sp.nameString()]
        wwr = massing.departments[z.department].wwr
        for srf in sp.surfaces():
            if srf.surfaceType() != "Wall" or srf.outsideBoundaryCondition() != "Outdoors":
                continue
            o = _orientation_of(srf, massing.north_deg)
            area = srf.grossArea()
            report.wall_area[o] = report.wall_area.get(o, 0.0) + area * z.multiplier
            target = wwr.get(o, 0.0)
            if target <= 0:
                continue
            sill = min(0.8, max(0.0, z.height * (1 - target) / 2))
            win = srf.setWindowToWallRatio(target, sill, True)
            if win.is_initialized():
                report.window_area[o] = report.window_area.get(o, 0.0) + win.get().grossArea() * z.multiplier
            else:
                report.warnings.append(f"{sp.nameString()}: no {o} window placed on {srf.nameString()} "
                                       f"({area:.1f} m2, non-rectangular or too small)")


def _hvac(model, massing: Massing):
    if massing.hvac != "ideal_loads":
        raise NotImplementedError(f"HVAC '{massing.hvac}' not implemented yet (use 'ideal_loads')")
    for tz in model.getThermalZones():
        il = M.ZoneHVACIdealLoadsAirSystem(model)
        il.addToThermalZone(tz)
        spaces = tz.spaces()
        if spaces and spaces[0].designSpecificationOutdoorAir().is_initialized():
            il.setDesignSpecificationOutdoorAirObject(spaces[0].designSpecificationOutdoorAir().get())
        il.setOutdoorAirEconomizerType("NoEconomizer")
        il.setHeatRecoveryType("None")
