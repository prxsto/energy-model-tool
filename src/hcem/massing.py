"""Massing input: levels of department floor plates, in metres.

A massing is the only geometry the engine needs: each level has an elevation and a
floor-to-floor height, and holds one or more plates (a plan polygon tagged with a
department). Plates on the same level may touch; shared edges become interior walls.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

FT = 0.3048

Orientation = str  # "N" | "E" | "S" | "W"


class Department(BaseModel):
    program: str = Field(description="Program key in the program library, e.g. 'Inpatient'")
    wwr: dict[str, float] = Field(default_factory=lambda: {"N": 0.3, "E": 0.3, "S": 0.3, "W": 0.3})
    perimeter_depth: float = Field(4.57, description="Perimeter zone depth in metres (15 ft)")
    hvac: str | None = Field(None, description="HVAC system key; None uses the building default")

    @field_validator("wwr")
    @classmethod
    def _wwr(cls, v):
        for k, x in v.items():
            if k not in "NESW" or not 0 <= x < 0.95:
                raise ValueError(f"bad WWR {k}={x}")
        return v


class Plate(BaseModel):
    department: str
    polygon: list[tuple[float, float]]

    def shape(self) -> Polygon:
        p = Polygon(self.polygon)
        if not p.is_valid or p.area <= 0:
            raise ValueError(f"invalid plate polygon for {self.department}")
        return orient(p, sign=1.0)  # counter-clockwise


class Level(BaseModel):
    name: str
    elevation: float
    height: float = Field(gt=1.5)
    plates: list[Plate]
    multiplier: int = 1


class Massing(BaseModel):
    name: str = "Hospital"
    north_deg: float = Field(0.0, description="Clockwise angle from model +Y to true north")
    climate_zone: str = "6A"
    hvac: str = "ideal_loads"
    departments: dict[str, Department]
    levels: list[Level]

    @model_validator(mode="after")
    def _check(self):
        for lv in self.levels:
            for pl in lv.plates:
                if pl.department not in self.departments:
                    raise ValueError(f"level {lv.name}: department '{pl.department}' not defined")
        return self

    @property
    def floor_area(self) -> float:
        return sum(pl.shape().area * lv.multiplier for lv in self.levels for pl in lv.plates)


def load(path: str | Path) -> Massing:
    """Load a massing JSON. Lengths may be given in feet with "units": "ft"."""
    raw = json.loads(Path(path).read_text())
    if raw.pop("units", "m") in ("ft", "feet"):
        for lv in raw["levels"]:
            lv["elevation"] *= FT
            lv["height"] *= FT
            for pl in lv["plates"]:
                pl["polygon"] = [(x * FT, y * FT) for x, y in pl["polygon"]]
        for d in raw["departments"].values():
            if "perimeter_depth" in d:
                d["perimeter_depth"] *= FT
    return Massing.model_validate(raw)
