# WilDivCheck

Willamette Valley reservoir elevation check: how far would each project's pool
have been drawn down if an additional withdrawal demand had been taken out of
it, on top of what actually happened?

For every year from 2015 through 2026 and every project, `src/wil_div_check.py`
takes the observed forebay elevation starting 01Jan, converts it to storage on
that reservoir's storage-elevation curve, subtracts the withdrawal demand
accumulated from 01Jan, converts back to an elevation, and plots the observed
and adjusted elevations together as `out/<year>/<CODE>_<year>_elevation.png`.
2026 runs to the last day of record rather than 31Dec.

## Inputs

| File | What it supplies |
|---|---|
| `data/WIL_ELEV_DICT.csv` | USGS site number per project |
| `data/STOR_RATINGS.xlsx` | one sheet per project: `ELEV (FEET)` vs `STOR (ACRE-FEET)` |
| `data/ALT_WithdrawalDemand.csv` | daily withdrawal demand in cfs per project |
| USGS Water Data API | daily mean forebay elevation, parameter 62614 |

`data/ALT_WithdrawalDemand.csv` holds a single representative year (2024), so it
is treated as a seasonal pattern and applied to every year by month and day. A
file spanning several calendar years would instead be used as a direct time
series.

`data/RuleCurves.csv` is not used by the adjustment. Its `Date` column did not
survive the export (Excel wrote it as `#####`); only its elevation columns are
readable, and they are used solely to bound the stand-in elevations described
below.

## Projects

Ten projects are plotted: Blue River, Cottage Grove, Cougar, Detroit, Dorena,
Fall Creek, Fern Ridge, Green Peter, Hills Creek and Lookout Point.

- **Big Cliff** and **Dexter** are skipped. They are re-regulating pools that
  pass the daily average outflow of Detroit and Lookout Point, so they carry no
  storage adjustment of their own.
- **Foster** is not plotted. Its demand is taken on by Green Peter, using the
  `GPR_FOS` column already present in `ALT_WithdrawalDemand.csv` (verified equal
  to `GPR + FOS`). The plain `GPR` column is not added on top of it.

## Running

```
pip install pandas numpy matplotlib openpyxl dataretrieval
cd src
python wil_div_check.py
```

### USGS API key

`api.waterdata.usgs.gov` now requires a personal access token; register at
<https://api.waterdata.usgs.gov/signup/>. The script looks for it in this
order, and never stores it itself:

1. the `API_USGS_PAT` environment variable (what `dataretrieval` reads),
2. `USGS_API_KEY`, accepted as an alias,
3. `data/usgs_api_key.txt`, a one-line file that `.gitignore` excludes.

The file is usually easiest under Jupyter, where the environment is awkward to
set:

```
# from the repository root, once
echo YOUR-KEY-HERE > data/usgs_api_key.txt
```

Treat the key as a password: do not commit it, and re-issue it at the signup
page if it is ever exposed.

### If a download returns no data

Run `python wil_div_check.py --probe`. It lists the time series USGS actually
publishes for each site, so the parameter code and statistic can be compared
against what the script requests. Elevation is not always available as a daily
statistic, so the downloader tries the daily endpoint first and falls back to
averaging the continuous (instantaneous) record -- the path used for each site
is printed.

It also runs unchanged in a Jupyter notebook or a VS Code interactive window
with no arguments -- the kernel's own command line is ignored, and `data/` is
located whether the working directory is `src/` or the repository root.

USGS downloads are cached under `data/cache/`, one CSV per site. A cached day
is never requested twice: a run downloads only the days on either end of what
the cache already holds, so rerunning the next day fetches that one day and
rerunning unchanged fetches nothing. Pass `--refresh` to re-download the whole
span, `--years 2020 2021` to limit the run,
and `--out` / `--data-dir` to redirect the output or input directories.

Alongside each PNG the script writes a CSV of the same name carrying the
observed and adjusted elevation, the observed and adjusted storage, the daily
demand, the withdrawal to date, the deficit still outstanding, and the
elevation change, so the plotted
numbers can be checked.

## Rule curve

Each plot also carries the project's rule curve in black, read from
`data/RuleCurves.csv`, which holds one representative year. It is applied as a
day-of-year pattern, so it maps onto any plotted year. The source year is not a
leap year and has no 29 February; in a leap year that day is interpolated from
28 February and 1 March rather than left as a gap. A project with no column in
the file is plotted without a rule curve.

## Method

- Observed daily mean elevation is converted to storage by linear interpolation
  on the project's rating table, and back the same way. Values outside the
  tabulated range are clamped to its end points rather than extrapolated.
- Demand in cfs becomes volume at 1 cfs-day = 86,400 ft³ = 1.9834711 acre-feet.
- The deficit accumulates while the demand is on: 01Jan carries none, so both
  traces start together, and each later day is reduced by everything withdrawn
  before it.
- Once the demand stops, the deficit is repaid out of the reservoir's own
  drawdown. Every acre-foot released after the season is an acre-foot that would
  instead have covered the withdrawal, so the operator reaches the same target
  pool having released that much less, and the adjusted trace holds level while
  the observed trace falls to meet it. Where a project draws down far enough the
  two rejoin; where it does not — Cougar and Lookout Point in several years — the
  deficit is still open on 31 December, which is the real result.
- Where the demand exceeds the storage available above the bottom of the rating
  curve, the adjusted trace is pinned to the curve minimum and the plot subtitle
  says on how many days that happened. On those days the adjusted elevation
  understates the shortfall and should not be read as a real pool level.

## Testing without USGS access

`src/make_dummy_inputs.py` builds a sandbox that runs the pipeline with no
network. It copies the real `STOR_RATINGS.xlsx`, `ALT_WithdrawalDemand.csv` and
`WIL_ELEV_DICT.csv` through unchanged, so the real parsers run against the real
inputs, and fabricates only the USGS elevation record — shaped like a rule curve
and confined to each project's real operating band from `RuleCurves.csv`.

```
cd src
python make_dummy_inputs.py
python wil_div_check.py --data-dir ../sandbox/data --out ../sandbox/out
```

**Those elevations are fabricated, not observations.** The sandbox exists to
prove the plumbing; delete it and run against `data/` for real results.
