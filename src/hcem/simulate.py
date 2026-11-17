"""Translate an OpenStudio model to IDF, run EnergyPlus and read annual results."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import openstudio as osd

GJ_TO_KBTU = 947.817
M2_TO_FT2 = 10.7639104
GJ_TO_KWH = 277.778


def find_energyplus(explicit: str | None = None) -> str:
    for cand in (explicit, shutil.which("energyplus"), shutil.which("EnergyPlus")):
        if cand:
            return cand
    for root in (Path("/usr/local"), Path("/opt"), Path("/Applications"), Path("C:/")):
        hits = sorted(root.glob("EnergyPlus*/energyplus*"))
        if hits:
            return str(hits[-1])
    raise FileNotFoundError("EnergyPlus not found; install it or pass --energyplus")


def write_idf(model, path: Path) -> list[str]:
    ft = osd.energyplus.ForwardTranslator()
    ws = ft.translateModel(model)
    errors = [e.logMessage() for e in ft.errors()]
    if errors:
        raise RuntimeError("translation failed:\n" + "\n".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    ws.save(osd.toPath(str(path)), True)
    return [w.logMessage() for w in ft.warnings()]


def run(idf: Path, epw: str, out_dir: Path, energyplus: str | None = None) -> Path:
    exe = find_energyplus(energyplus)
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([exe, "-w", str(epw), "-d", str(out_dir), str(idf)],
                          capture_output=True, text=True)
    err = out_dir / "eplusout.err"
    text = err.read_text(errors="replace") if err.exists() else proc.stderr
    if proc.returncode != 0 or "EnergyPlus Completed Successfully" not in text:
        severe = [ln for ln in text.splitlines() if "** Severe" in ln or "**  Fatal" in ln]
        raise RuntimeError("EnergyPlus failed:\n" + "\n".join(severe[:20] or text.splitlines()[-20:]))
    return out_dir / "eplusout.sql"


@dataclass
class Results:
    area_m2: float
    end_uses_gj: dict[str, dict[str, float]]  # end use -> fuel -> GJ
    warnings: int

    def by_fuel(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for fuels in self.end_uses_gj.values():
            for f, v in fuels.items():
                out[f] = out.get(f, 0.0) + v
        return out

    def eui_kbtu_ft2(self, gj: float) -> float:
        return gj * GJ_TO_KBTU / (self.area_m2 * M2_TO_FT2)

    def plant_estimate(self, boiler_eff: float = 0.80, chiller_cop: float = 4.0) -> dict[str, float]:
        """Convert ideal-load district energy to gas and electricity with flat efficiencies."""
        out: dict[str, float] = {}
        for use, fuels in self.end_uses_gj.items():
            for f, v in fuels.items():
                if f == "District Heating Water":
                    out["Natural Gas"] = out.get("Natural Gas", 0.0) + v / boiler_eff
                elif f == "District Cooling":
                    out["Electricity"] = out.get("Electricity", 0.0) + v / chiller_cop
                else:
                    out[f] = out.get(f, 0.0) + v
        return out


def read_results(sql: Path) -> Results:
    con = sqlite3.connect(sql)
    q = """SELECT RowName, ColumnName, Value, Units FROM TabularDataWithStrings
           WHERE ReportName='AnnualBuildingUtilityPerformanceSummary' AND TableName=? AND ReportForString='Entire Facility'"""
    end_uses: dict[str, dict[str, float]] = {}
    for row, col, val, units in con.execute(q, ("End Uses",)):
        if row in ("", "Total End Uses") or col in ("Water",) or units != "GJ":
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        if v:
            end_uses.setdefault(row, {})[col] = v
    area = None
    for row, col, val, units in con.execute(q, ("Building Area",)):
        if row == "Total Building Area" and col == "Area":
            area = float(val)
    con.close()
    err = Path(sql).with_name("eplusout.err")
    warn = err.read_text(errors="replace").count("** Warning **") if err.exists() else 0
    return Results(area_m2=area or 0.0, end_uses_gj=end_uses, warnings=warn)


def format_results(res: Results, boiler_eff=0.80, chiller_cop=4.0) -> str:
    lines = [f"Floor area: {res.area_m2:,.0f} m2 ({res.area_m2 * M2_TO_FT2:,.0f} ft2)   "
             f"EnergyPlus warnings: {res.warnings}", "",
             f"{'End use':28s} {'fuel':26s} {'GJ':>10s} {'kBtu/ft2':>9s} {'kWh/m2':>8s}"]
    for use, fuels in res.end_uses_gj.items():
        for f, v in fuels.items():
            lines.append(f"{use:28s} {f:26s} {v:10.0f} {res.eui_kbtu_ft2(v):9.1f} {v * GJ_TO_KWH / res.area_m2:8.1f}")
    tot = sum(res.by_fuel().values())
    lines += ["", f"Site EUI (ideal loads as district energy): {res.eui_kbtu_ft2(tot):6.1f} kBtu/ft2"]
    est = res.plant_estimate(boiler_eff, chiller_cop)
    tot2 = sum(est.values())
    parts = ", ".join(f"{f} {res.eui_kbtu_ft2(v):.1f}" for f, v in est.items())
    lines.append(f"Site EUI, plant estimate (boiler {boiler_eff:.2f}, chiller COP {chiller_cop:.1f}): "
                 f"{res.eui_kbtu_ft2(tot2):6.1f} kBtu/ft2  ({parts})")
    lines.append("Ideal loads exclude fans, pumps, reheat and humidification; treat as a loads check, not an EUI.")
    return "\n".join(lines)
