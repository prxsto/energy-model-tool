"""Command line: hcem zones | build | run."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from . import builder, massing as massing_mod, simulate, zoning

app = typer.Typer(add_completion=False, help="Healthcare massing to EnergyPlus.")


def _load(path: Path, min_ftf: float) -> massing_mod.Massing:
    if path.suffix.lower() == ".3dm":
        from . import rhino_io
        dept_file = path.with_suffix(".departments.json")
        depts, extra = None, {}
        if dept_file.exists():  # optional sidecar: {"departments": {...}, "north_deg": .., ...}
            raw = json.loads(dept_file.read_text())
            depts = {k: massing_mod.Department(**v) for k, v in raw.pop("departments", {}).items()}
            extra = raw
        return rhino_io.read_3dm(path, min_ftf=min_ftf, departments=depts, **extra)
    return massing_mod.load(path)


@app.command()
def zones(massing: Path, min_ftf: float = typer.Option(2.4, help="Merge plates closer than this (m)")):
    """Print the perimeter/core zones a massing produces."""
    typer.echo(zoning.summary(zoning.zone_massing(_load(massing, min_ftf))))


@app.command()
def build(massing: Path, out: Path = typer.Option(Path("out"), help="Output folder"),
          epw: Path | None = typer.Option(None, help="Weather file (.epw)"),
          programs: Path | None = typer.Option(None, help="Extra program blends JSON"),
          min_ftf: float = 2.4):
    """Write the OpenStudio model (.osm) and EnergyPlus input (.idf) without running."""
    m = _load(massing, min_ftf)
    model, rep = builder.build(m, str(epw) if epw else None, programs)
    out.mkdir(parents=True, exist_ok=True)
    model.save(str(out / "model.osm"), True)
    simulate.write_idf(model, out / "in.idf")
    _report(m, rep)
    typer.echo(f"wrote {out / 'model.osm'} and {out / 'in.idf'}")


@app.command()
def run(massing: Path, epw: Path = typer.Option(..., help="Weather file (.epw)"),
        out: Path = typer.Option(Path("out"), help="Output folder"),
        programs: Path | None = None, energyplus: str | None = None,
        boiler_eff: float = 0.80, chiller_cop: float = 4.0, min_ftf: float = 2.4):
    """Build, simulate and print EUI by end use."""
    m = _load(massing, min_ftf)
    model, rep = builder.build(m, str(epw), programs)
    out.mkdir(parents=True, exist_ok=True)
    model.save(str(out / "model.osm"), True)
    simulate.write_idf(model, out / "in.idf")
    _report(m, rep)
    typer.echo("running EnergyPlus ...")
    sql = simulate.run(out / "in.idf", str(epw), out / "run", energyplus)
    res = simulate.read_results(sql)
    text = simulate.format_results(res, boiler_eff, chiller_cop)
    (out / "results.txt").write_text(text + "\n")
    typer.echo(text)


def _report(m, rep: builder.BuildReport):
    typer.echo(f"{m.name}: {len(rep.zones)} zones, {m.floor_area:,.0f} m2")
    wwr = {o: rep.window_area.get(o, 0) / a for o, a in rep.wall_area.items() if a}
    typer.echo("WWR as built: " + "  ".join(f"{o} {wwr[o]:.0%}" for o in "NESW" if o in wwr))
    for w in rep.warnings:
        typer.echo(f"warning: {w}")


if __name__ == "__main__":
    app()
