"""Extract hospital space types, schedules and envelope limits from OpenStudio-Standards.

OpenStudio-Standards is BSD-licensed (see OPENSTUDIO_STANDARDS_LICENSE.md). This script
converts the 90.1-2019 hospital prototype data to SI units and writes it to
src/hcem/data/. Rerun it to refresh the data from a newer checkout:

    python tools/extract_osstd.py /path/to/openstudio-standards
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FT2_PER_M2 = 10.7639104
CFM_TO_M3S = 0.000471947443
GAL_TO_M3 = 0.00378541178
BTU_HFT2F_TO_W_M2K = 5.67826334
BTU_HFTF_TO_W_MK = 1.73073466

TEMPLATE = "90.1-2019"
BUILDING = "Hospital"


def _num(v):
    return None if v in (None, "") else float(v)


def space_types(root: Path) -> dict:
    p = root / "lib/openstudio-standards/standards/ashrae_90_1/ashrae_90_1_2019/data/ashrae_90_1_2019.spc_typ.json"
    rows = json.loads(p.read_text())["space_types"]
    out = {}
    for r in rows:
        if r.get("building_type") != BUILDING or r.get("template") != TEMPLATE:
            continue
        lpd = _num(r.get("lighting_per_area"))
        epd = _num(r.get("electric_equipment_per_area"))
        gpd = _num(r.get("gas_equipment_per_area"))
        occ = _num(r.get("occupancy_per_area"))
        oa_a = _num(r.get("ventilation_per_area"))
        oa_p = _num(r.get("ventilation_per_person"))
        swh = _num(r.get("service_water_heating_peak_flow_per_area"))
        out[r["space_type"]] = {
            "lighting_w_m2": round(lpd * FT2_PER_M2, 3) if lpd else 0.0,
            "equipment_w_m2": round(epd * FT2_PER_M2, 3) if epd else 0.0,
            "gas_equipment_w_m2": round(gpd * FT2_PER_M2, 3) if gpd else 0.0,
            "equipment_fraction_latent": _num(r.get("electric_equipment_fraction_latent")) or 0.0,
            "equipment_fraction_radiant": _num(r.get("electric_equipment_fraction_radiant")) or 0.0,
            "people_per_m2": round(occ / 1000.0 * FT2_PER_M2, 5) if occ else 0.0,
            "oa_m3s_per_m2": round(oa_a * CFM_TO_M3S * FT2_PER_M2, 7) if oa_a else 0.0,
            "oa_m3s_per_person": round(oa_p * CFM_TO_M3S, 7) if oa_p else 0.0,
            "oa_ach": _num(r.get("ventilation_air_changes")) or 0.0,
            # gal/h per ft2 -> m3/s per m2
            "swh_m3s_per_m2": (swh * GAL_TO_M3 / 3600.0 * FT2_PER_M2) if swh else 0.0,
            "schedules": {
                "lighting": r.get("lighting_schedule"),
                "equipment": r.get("electric_equipment_schedule"),
                "occupancy": r.get("occupancy_schedule"),
                "activity": r.get("occupancy_activity_schedule"),
                "heating_setpoint": r.get("heating_setpoint_schedule"),
                "cooling_setpoint": r.get("cooling_setpoint_schedule"),
                "swh": r.get("service_water_heating_schedule") or None,
            },
            "source": f"OpenStudio-Standards {TEMPLATE} {BUILDING} {r['space_type']}",
        }
    return out


def schedules(root: Path, needed: set[str]) -> dict:
    p = root / "lib/openstudio-standards/standards/ashrae_90_1/data/ashrae_90_1.schedules.json"
    rows = json.loads(p.read_text())["schedules"]
    out: dict[str, list] = {}
    for r in rows:
        if r["name"] in needed:
            out.setdefault(r["name"], []).append(
                {"day_types": r["day_types"].split("|"), "type": r["type"], "values": r["values"],
                 "category": r.get("category")}
            )
    missing = needed - set(out)
    if missing:
        raise SystemExit(f"schedules not found: {sorted(missing)}")
    return out


def envelope(root: Path) -> dict:
    p = root / "lib/openstudio-standards/standards/ashrae_90_1/ashrae_90_1_2019/data/ashrae_90_1_2019.construction_properties.json"
    rows = json.loads(p.read_text())["construction_properties"]
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("building_category") != "Nonresidential":
            continue
        cz = r["climate_zone_set"].replace("ClimateZone ", "")
        key = f"{r['intended_surface_type']}:{r['standards_construction_type']}"
        if r.get("minimum_percent_of_surface") not in (None, 0):
            continue  # keep the 0-40 % WWR rows only
        u = _num(r.get("assembly_maximum_u_value"))
        f = _num(r.get("assembly_maximum_f_factor"))
        c = _num(r.get("assembly_maximum_c_factor"))
        out.setdefault(cz, {})[key] = {
            "u_w_m2k": round(u * BTU_HFT2F_TO_W_M2K, 4) if u else None,
            "f_w_mk": round(f * BTU_HFTF_TO_W_MK, 4) if f else None,
            "c_w_m2k": round(c * BTU_HFT2F_TO_W_M2K, 4) if c else None,
            "shgc": _num(r.get("assembly_maximum_solar_heat_gain_coefficient")),
        }
    return out


def main(root: Path) -> None:
    rev = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H %cs"],
                         capture_output=True, text=True).stdout.strip()
    data = Path(__file__).resolve().parents[1] / "src/hcem/data"
    st = space_types(root)
    needed = {s for t in st.values() for s in t["schedules"].values() if s}
    meta = {"source": "NREL OpenStudio-Standards (BSD-3-Clause)", "revision": rev, "template": TEMPLATE}
    (data / "space_types.json").write_text(json.dumps({"_meta": meta, "space_types": st}, indent=1))
    (data / "schedules.json").write_text(json.dumps({"_meta": meta, "schedules": schedules(root, needed)}, indent=1))
    (data / "envelope.json").write_text(json.dumps({"_meta": meta, "climate_zones": envelope(root)}, indent=1))
    print(f"{len(st)} space types, {len(needed)} schedules, revision {rev}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
