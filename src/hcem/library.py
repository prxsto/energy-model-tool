"""Space type library, schedules, envelope limits and department program blends."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path


def _load(name: str) -> dict:
    return json.loads(resources.files("hcem.data").joinpath(name).read_text())


@lru_cache
def space_types() -> dict:
    return _load("space_types.json")["space_types"]


@lru_cache
def schedules() -> dict:
    return _load("schedules.json")["schedules"]


@lru_cache
def envelope(climate_zone: str) -> dict:
    cz = "".join(ch for ch in climate_zone if ch.isdigit())
    return _load("envelope.json")["climate_zones"][cz]


def programs(extra: str | Path | None = None) -> dict:
    progs = dict(_load("programs.json")["programs"])
    if extra:
        progs.update(json.loads(Path(extra).read_text())["programs"])
    return progs


LOAD_KEYS = ("lighting_w_m2", "equipment_w_m2", "gas_equipment_w_m2", "people_per_m2",
             "oa_m3s_per_m2", "oa_m3s_per_person", "swh_m3s_per_m2")
SCHEDULE_FOR = {"lighting_w_m2": "lighting", "equipment_w_m2": "equipment", "people_per_m2": "occupancy"}


@dataclass
class Blend:
    """Area-weighted loads for one zone, with the component mix kept for OA and reporting."""
    name: str
    mix: dict[str, float]
    loads: dict[str, float]
    schedules: dict[str, str]

    def outdoor_air_per_area(self, height: float) -> float:
        """Design outdoor air (m3/s per m2 floor) combining per-area, per-person and ACH rules."""
        lib = space_types()
        total = 0.0
        for st, f in self.mix.items():
            s = lib[st]
            total += f * (s["oa_m3s_per_m2"] + s["oa_m3s_per_person"] * s["people_per_m2"]
                          + s["oa_ach"] * height / 3600.0)
        return total


def blend(name: str, mix: dict[str, float]) -> Blend:
    lib = space_types()
    unknown = set(mix) - set(lib)
    if unknown:
        raise KeyError(f"{name}: unknown space types {sorted(unknown)}; choose from {sorted(lib)}")
    tot = sum(mix.values())
    if abs(tot - 1.0) > 1e-3:
        raise ValueError(f"{name}: fractions sum to {tot:.3f}, expected 1.0")
    loads = {k: sum(f * lib[st][k] for st, f in mix.items()) for k in LOAD_KEYS}
    scheds = {}
    for key, role in SCHEDULE_FOR.items():  # schedule of the largest contributor
        st = max(mix, key=lambda s: mix[s] * (lib[s][key] or 1e-9))
        scheds[role] = lib[st]["schedules"][role]
    dom = max(mix, key=mix.get)
    for role in ("activity", "heating_setpoint", "cooling_setpoint"):
        scheds[role] = lib[dom]["schedules"][role]
    swh = [s for s in mix if lib[s]["schedules"]["swh"]]
    scheds["swh"] = lib[max(swh, key=lambda s: mix[s] * lib[s]["swh_m3s_per_m2"])]["schedules"]["swh"] if swh else None
    return Blend(name, mix, loads, scheds)


def zone_blend(program: str, kind: str, progs: dict) -> Blend:
    p = progs[program]
    if kind == "whole":  # unsplit plate: 40 % perimeter mix, 60 % core mix
        mix: dict[str, float] = {}
        for part, w in (("perimeter", 0.4), ("core", 0.6)):
            for st, f in p[part].items():
                mix[st] = mix.get(st, 0.0) + w * f
        return blend(f"{program}_Whole", mix)
    return blend(f"{program}_{kind.capitalize()}", p[kind])
