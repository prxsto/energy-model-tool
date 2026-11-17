# hcem

Healthcare massing to EnergyPlus. Floor plates by department in, perimeter/core zones,
an OpenStudio model, an EnergyPlus run and EUI by end use out.

## Setup (macOS)

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Install EnergyPlus 25.2 (matches OpenStudio 3.11) from the EnergyPlus GitHub releases.
`hcem` finds it on the PATH or under /Applications; otherwise pass `--energyplus`.

## Use

```sh
hcem zones examples/example_hospital.json
hcem run examples/example_hospital.json --epw path/to/weather.epw --out out/
hcem run examples/example_hospital.3dm  --epw path/to/weather.epw --out out/
```

A .3dm uses layer names as departments. Put a `<name>.departments.json` beside it to set
programs, WWR and north: `{"north_deg": 0, "departments": {"Lobby": {"program": "Lobby", "wwr": {"S": 0.5}}}}`.

HVAC is ideal loads for now, so results are a loads check, not a full EUI. See HANDOFF.md.

## Data and licenses

`src/hcem/data/` (except programs.json) is extracted from NREL OpenStudio-Standards
(BSD-3-Clause, see tools/OPENSTUDIO_STANDARDS_LICENSE.md) by `tools/extract_osstd.py`.
