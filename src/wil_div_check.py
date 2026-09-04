# -*- coding: utf-8 -*-
"""
WilDivCheck - Willamette reservoir elevation adjustment for withdrawal demand.

For each year 2015-2026 and each Willamette Valley project, this script:
  1. Downloads the daily mean forebay elevation from USGS (parameter 62614)
     for the sites listed in data/WIL_ELEV_DICT.csv.
  2. Converts the observed elevation to storage with that reservoir's
     storage-elevation curve from data/STOR_RATINGS.xlsx.
  3. Subtracts the running total of the withdrawal demand in
     data/ALT_WithdrawalDemand.csv, accumulated from 01Jan of that year.
  4. Converts the adjusted storage back to an elevation and plots the
     observed and adjusted elevation together, one PNG per reservoir per
     year, under out/<year>/.

Excluded projects
-----------------
Big Cliff and Dexter are re-regulating pools that pass the daily average
outflow of Detroit and Lookout Point respectively, so they carry no storage
adjustment of their own and are skipped. Foster is skipped as a plotted
project, and its demand is added to Green Peter's instead.

USGS downloads are cached under data/cache/ as one CSV per site, and only the
days missing from the cache are ever requested, so a rerun downloads nothing
and a rerun the next day downloads one day. Pass --refresh to re-download.

Usage
-----
    python wil_div_check.py                  # download (or use cache) and plot
    python wil_div_check.py --refresh        # force re-download from USGS
    python wil_div_check.py --years 2020 2021

It also runs as-is in a Jupyter/VS Code interactive window or notebook with
no arguments; the kernel's own command line is ignored.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # Pasted into a notebook cell, so there is no __file__ to anchor to.
    HERE = os.path.abspath(os.getcwd())


def _find_data_dir():
    """Locate data/ whether we are run from src/, the repo root, or a notebook."""
    for base in (HERE, os.path.join(HERE, ".."), os.getcwd(),
                 os.path.join(os.getcwd(), "..")):
        candidate = os.path.normpath(os.path.join(base, "data"))
        if os.path.exists(os.path.join(candidate, "WIL_ELEV_DICT.csv")):
            return candidate
    return os.path.normpath(os.path.join(HERE, "..", "data"))


DATA_DIR = _find_data_dir()
REPO_DIR = os.path.dirname(DATA_DIR)
OUT_DIR = os.path.join(REPO_DIR, "out")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

ELEV_DICT_PATH = os.path.join(DATA_DIR, "WIL_ELEV_DICT.csv")
STOR_RATINGS_PATH = os.path.join(DATA_DIR, "STOR_RATINGS.xlsx")
DEMAND_PATH = os.path.join(DATA_DIR, "ALT_WithdrawalDemand.csv")
RULE_CURVE_PATH = os.path.join(DATA_DIR, "RuleCurves.csv")

# USGS now requires a personal access token for api.waterdata.usgs.gov.
# API_USGS_PAT is the variable dataretrieval itself reads; USGS_API_KEY is
# accepted as a friendlier alias. The key is a credential: it is never stored
# in this repository, and the key file below is in .gitignore.
API_KEY_ENV = "API_USGS_PAT"
API_KEY_ALIAS = "USGS_API_KEY"
API_KEY_FILENAME = "usgs_api_key.txt"
API_KEY_SIGNUP = "https://api.waterdata.usgs.gov/signup/"

ELEV_PARAMETER_CD = "62614"   # Lake or reservoir water surface elevation, NGVD29 ft
DAILY_MEAN_STAT_CD = "00003"

FIRST_YEAR = 2015
LAST_YEAR = 2026

# 1 cfs sustained for one day = 86400 cubic feet = 86400/43560 acre-feet.
CFS_DAY_TO_ACRE_FT = 86400.0 / 43560.0

# Validated 2-series categorical palette (dataviz reference instance).
COLOR_RULE = "#000000"       # rule curve: a reference line, not a series
COLOR_OBSERVED = "#2a78d6"   # categorical slot 1, blue
COLOR_ADJUSTED = "#eb6834"   # categorical slot 2, orange
COLOR_SURFACE = "#fcfcfb"
COLOR_TEXT = "#0b0b0b"
COLOR_TEXT_MUTED = "#52514e"
COLOR_GRID = "#e3e2df"

# USGS site number -> (project code, display name). The project code is the
# NWD 3-letter identifier used in ALT_WithdrawalDemand.csv and, normally, as
# the STOR_RATINGS.xlsx sheet name.
SITE_META = {
    "14181400": ("BCL", "Big Cliff"),
    "14162100": ("BLU", "Blue River"),
    "14153000": ("COT", "Cottage Grove"),
    "14159400": ("CGR", "Cougar"),
    "14180500": ("DET", "Detroit"),
    "14149500": ("DEX", "Dexter"),
    "14155000": ("DOR", "Dorena"),
    "14150900": ("FAL", "Fall Creek"),
    "14168000": ("FRN", "Fern Ridge"),
    "14186600": ("FOS", "Foster"),
    "14186100": ("GPR", "Green Peter"),
    "14145100": ("HCR", "Hills Creek"),
    "14149000": ("LOP", "Lookout Point"),
}

# Re-regulating pools that pass the upstream project's daily average flow, and
# Foster, whose demand is rolled into Green Peter instead of being plotted.
SKIP_PROJECTS = {"BCL", "DEX", "FOS"}

# Foster is not modelled as its own pool; its demand is taken on by Green Peter.
# ALT_WithdrawalDemand.csv supplies a pre-combined GPR_FOS column, which is used
# when present; otherwise GPR and FOS are summed to the same effect.
PREFERRED_DEMAND_COLUMN = {"GPR": "GPR_FOS"}
EXTRA_DEMAND = {"GPR": ["FOS"]}

# Alternate spellings that may appear as a demand column or a rating sheet name.
NAME_ALIASES = {
    "BCL": ["BIGCLIFF", "BIGCLIFFLAKE"],
    "BLU": ["BLUERIVER", "BLUERIVERLAKE"],
    "COT": ["COTTAGEGROVE", "COTTAGEGROVELAKE", "CGL"],
    "CGR": ["COUGAR", "COUGARLAKE"],
    "DET": ["DETROIT", "DETROITLAKE"],
    "DEX": ["DEXTER", "DEXTERLAKE"],
    "DOR": ["DORENA", "DORENALAKE"],
    "FAL": ["FALLCREEK", "FALLCREEKLAKE", "FCR"],
    "FRN": ["FERNRIDGE", "FERNRIDGELAKE", "FRD", "FER"],
    "FOS": ["FOSTER", "FOSTERLAKE"],
    "GPR": ["GREENPETER", "GREENPETERLAKE", "GPE"],
    "HCR": ["HILLSCREEK", "HILLSCREEKLAKE", "HCK"],
    "LOP": ["LOOKOUTPOINT", "LOOKOUTPOINTLAKE", "LOK"],
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _norm(text) -> str:
    """Uppercase and strip everything that is not a letter or digit."""
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def _match_project(label, projects) -> str | None:
    """Resolve a sheet name or column header to one of `projects`.

    Matches the 3-letter code first, then the display name, then any known
    alias, comparing on the punctuation- and case-stripped form so that
    "Green Peter", "GREEN_PETER" and "GPR" all land on GPR.
    """
    key = _norm(label)
    if not key:
        return None
    for code, name in projects.items():
        if key == code:
            return code
    candidates = []
    for code, name in projects.items():
        for alt in [code, _norm(name)] + [_norm(a) for a in NAME_ALIASES.get(code, [])]:
            if alt:
                candidates.append((alt, code))
    # Longest candidate first so COTTAGEGROVE beats a stray short substring.
    for alt, code in sorted(candidates, key=lambda p: -len(p[0])):
        if key == alt or key.startswith(alt) or alt in key:
            return code
    return None


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def read_elev_dict(path: str = ELEV_DICT_PATH) -> pd.DataFrame:
    """Read WIL_ELEV_DICT.csv and attach the project code and display name.

    Rows whose site number is not in SITE_META, and the re-regulating pools
    and Foster in SKIP_PROJECTS, are dropped.
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["site"] = df["Download_Key"].astype(str).str.strip()
    meta = df["site"].map(SITE_META)
    unknown = df.loc[meta.isna(), "site"].tolist()
    if unknown:
        print(f"[WARNING] Site(s) not in SITE_META, skipped: {', '.join(unknown)}")
    df = df.loc[meta.notna()].copy()
    df["code"] = [m[0] for m in meta.dropna()]
    df["name"] = [m[1] for m in meta.dropna()]
    skipped = sorted(set(df.loc[df["code"].isin(SKIP_PROJECTS), "code"]))
    if skipped:
        print(f"[INFO] Skipping pass-through / rolled-up projects: {', '.join(skipped)}")
    df = df.loc[~df["code"].isin(SKIP_PROJECTS)].copy()
    return df.sort_values("name").reset_index(drop=True)


def _locate_rating_columns(raw: pd.DataFrame):
    """Find the (elevation, storage) columns in one rating sheet.

    The sheet is read with header=None so a title block above the real header
    does not confuse things. Returns two float Series with NaN rows dropped.
    """
    # Find the header row: the first row where one cell names elevation and a
    # *different* cell names storage. Requiring two distinct cells keeps a title
    # like "Blue River storage-elevation curve" from matching both on itself.
    header_row = elev_idx = stor_idx = None
    for i in range(min(20, len(raw))):
        cells = [_norm(c) for c in raw.iloc[i].tolist()]
        elev_at = [j for j, c in enumerate(cells) if "ELEV" in c]
        stor_at = [j for j, c in enumerate(cells)
                   if "STOR" in c or c.startswith("AF") or "ACREFT" in c]
        pair = next(((a, b) for a in elev_at for b in stor_at if a != b), None)
        if pair is not None:
            header_row, (elev_idx, stor_idx) = i, pair
            break

    if header_row is not None:
        body = raw.iloc[header_row + 1:].reset_index(drop=True)
    else:
        # No recognizable header - fall back to the two numeric columns, taking
        # the one with the smaller magnitude as elevation (feet are hundreds to
        # low thousands; storage in acre-feet is far larger).
        body = raw

    if elev_idx is None or stor_idx is None:
        numeric_cols = []
        for j in range(body.shape[1]):
            col = _numeric(body.iloc[:, j])
            if col.notna().sum() >= 3:
                numeric_cols.append((j, col.abs().max()))
        if len(numeric_cols) < 2:
            raise ValueError("could not find two numeric columns for the rating curve")
        numeric_cols.sort(key=lambda p: p[1])
        elev_idx, stor_idx = numeric_cols[0][0], numeric_cols[-1][0]

    if elev_idx == stor_idx:
        raise ValueError("elevation and storage resolved to the same column")

    elev = _numeric(body.iloc[:, elev_idx])
    stor = _numeric(body.iloc[:, stor_idx])
    keep = elev.notna() & stor.notna()
    if keep.sum() < 2:
        raise ValueError("rating curve has fewer than two usable rows")
    elev, stor = elev[keep].to_numpy(float), stor[keep].to_numpy(float)

    # A reservoir's storage in acre-feet spans a far wider range than its
    # elevation in feet. If it does not, the columns are almost certainly
    # mismatched, and silently producing nonsense drawdowns is worse than
    # stopping here.
    if np.ptp(stor) <= np.ptp(elev):
        raise ValueError(
            f"storage range ({np.ptp(stor):,.0f}) is not wider than the elevation "
            f"range ({np.ptp(elev):,.0f}); columns look mismatched")
    return elev, stor


class RatingCurve:
    """A monotonic storage-elevation curve with interpolation both ways.

    Values outside the tabulated range are clamped to the end points rather
    than extrapolated, so a pool drawn below the bottom of the curve reports
    the curve's minimum elevation instead of an invented one.
    """

    def __init__(self, elevation: np.ndarray, storage: np.ndarray, label: str = ""):
        order = np.argsort(elevation)
        elevation, storage = elevation[order], storage[order]
        # Drop duplicate elevations, which would make the inverse ambiguous.
        keep = np.concatenate([[True], np.diff(elevation) > 0])
        self.elevation, self.storage = elevation[keep], storage[keep]
        self.label = label
        if np.any(np.diff(self.storage) <= 0):
            print(f"[WARNING] {label}: storage is not strictly increasing with "
                  f"elevation; inverse lookup may be imprecise.")

    @property
    def min_storage(self) -> float:
        return float(self.storage[0])

    def to_storage(self, elev):
        return np.interp(np.asarray(elev, dtype=float), self.elevation, self.storage)

    def to_elevation(self, stor):
        return np.interp(np.asarray(stor, dtype=float), self.storage, self.elevation)


def read_stor_ratings(projects, path: str = STOR_RATINGS_PATH):
    """Read STOR_RATINGS.xlsx, one sheet per reservoir, into RatingCurves."""
    book = pd.read_excel(path, sheet_name=None, header=None)
    curves = {}
    for sheet_name, raw in book.items():
        code = _match_project(sheet_name, projects)
        if code is None:
            print(f"[INFO] Rating sheet '{sheet_name}' does not match a project; ignored.")
            continue
        if code in curves:
            print(f"[WARNING] More than one sheet matches {code}; keeping the first.")
            continue
        try:
            elev, stor = _locate_rating_columns(raw)
        except ValueError as exc:
            print(f"[WARNING] Rating sheet '{sheet_name}' ({code}) unusable: {exc}")
            continue
        curves[code] = RatingCurve(elev, stor, label=code)
        print(f"[INFO] Rating curve {code}: {len(curves[code].elevation)} points, "
              f"elev {curves[code].elevation[0]:.1f}-{curves[code].elevation[-1]:.1f} ft, "
              f"storage {curves[code].storage[0]:,.0f}-{curves[code].storage[-1]:,.0f} af")
    missing = sorted(set(projects) - set(curves))
    if missing:
        print(f"[WARNING] No rating sheet found for: {', '.join(missing)}")
    return curves


def read_demand(projects, path: str = DEMAND_PATH) -> pd.DataFrame:
    """Read ALT_WithdrawalDemand.csv into a daily flow table in cfs.

    The first column that parses as dates becomes the index; every remaining
    column is kept under its own header, normalized. Columns are matched to
    projects later, in demand_for_project, so that a combined column such as
    GPR_FOS stays distinct from the plain GPR column instead of being folded
    into it and counted twice.
    """
    df = pd.read_csv(path)
    date_col = None
    for col in df.columns:
        parsed = pd.to_datetime(df[col], errors="coerce")
        if parsed.notna().sum() >= max(1, int(0.8 * len(df))):
            date_col = col
            break
    if date_col is None:
        raise ValueError(f"no date column found in {path}")

    demand = df.drop(columns=[date_col]).apply(_numeric)
    demand.columns = [_norm(c) for c in demand.columns]
    demand.index = pd.to_datetime(df[date_col], errors="coerce")
    demand = demand.loc[demand.index.notna()].sort_index()
    demand = demand.resample("D").mean()

    print(f"[INFO] Demand columns: {', '.join(demand.columns)}")
    print(f"[INFO] Demand period: {demand.index.min().date()} to "
          f"{demand.index.max().date()} ({len(demand):,} days)")
    return demand


def demand_for_project(demand: pd.DataFrame, code: str) -> tuple[pd.Series, str] | None:
    """The demand column(s) for one project, plus a label describing the source.

    Green Peter prefers the pre-combined GPR_FOS column; if the file does not
    carry one, GPR and FOS are summed instead.
    """
    preferred = _norm(PREFERRED_DEMAND_COLUMN.get(code, ""))
    if preferred and preferred in demand.columns:
        return demand[preferred], preferred

    wanted = [code] + EXTRA_DEMAND.get(code, [])
    present = [c for c in wanted if c in demand.columns]
    if not present:
        # Fall back to fuzzy matching for a file that spells projects out.
        resolved = {}
        for column in demand.columns:
            matched = _match_project(column, {c: c for c in wanted})
            if matched and matched not in resolved:
                resolved[matched] = column
        present = list(resolved.values())
        if not present:
            return None
    return demand[present].sum(axis=1), " + ".join(present)


def read_rule_curves(projects: dict, path: str) -> dict:
    """Rule curve elevation per project, as a (month, day) -> elevation map.

    RuleCurves.csv is one column per project over a single representative year,
    so it is stored as a day-of-year pattern and mapped onto whichever year is
    being plotted. A file carrying several years is reduced to the most recent
    complete one. Any project without a column simply gets no rule curve line.
    """
    if not os.path.exists(path):
        print(f"[WARNING] No rule curves at {path}; plots omit the rule curve.")
        return {}

    raw = pd.read_csv(path)
    date_col = raw.columns[0]
    dates = pd.to_datetime(raw[date_col], format="%d%b%Y", errors="coerce")
    if dates.isna().any():
        print(f"[WARNING] {int(dates.isna().sum())} unreadable date(s) in "
              f"{os.path.basename(path)}; those rows are ignored.")
    raw = raw[dates.notna()].set_index(pd.DatetimeIndex(dates.dropna()))

    curves = {}
    for column in raw.columns:
        if column == date_col:
            continue
        code = _match_project(column, projects)
        if code is None:
            continue
        series = _numeric(raw[column]).dropna()
        if series.empty:
            continue
        # Several years collapse to the most recent complete one; a single
        # year is used as it stands.
        latest = series[series.index.year == series.index.year.max()]
        pattern = latest if len(latest) >= 300 else series
        pattern = pattern.groupby([pattern.index.month, pattern.index.day]).mean()
        curves[code] = pattern
        leap = "" if (2, 29) in pattern.index else ", no 29 Feb (filled per year)"
        print(f"[INFO] Rule curve {code}: {pattern.min():,.2f}-{pattern.max():,.2f} ft "
              f"over {len(pattern)} days{leap}")

    missing = sorted(set(projects) - set(curves))
    if missing:
        print(f"[INFO] No rule curve column for: {', '.join(missing)}")
    return curves


def rule_curve_for_year(pattern: pd.Series, year: int) -> pd.Series:
    """Map a (month, day) rule curve pattern onto one calendar year.

    The source year need not be a leap year, so 29 February can be absent from
    the pattern while the plotted year has one. That day is interpolated from
    28 February and 1 March rather than left as a gap in the line.
    """
    axis = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    aligned = pattern.reindex(pd.MultiIndex.from_arrays([axis.month, axis.day]))
    aligned.index = axis
    if aligned.isna().any():
        aligned = aligned.interpolate(method="time", limit_area="inside")
        aligned = aligned.ffill().bfill()
    return aligned


def demand_for_year(series: pd.Series, year: int) -> pd.Series:
    """Align one project's demand to a calendar year.

    ALT_WithdrawalDemand.csv holds a single representative year, so it is
    treated as a seasonal pattern and applied to every year by month and day.
    If a file spanning several calendar years is supplied instead, that year's
    values are used directly.
    """
    axis = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    if series.index.year.nunique() > 1:
        return series.reindex(axis)

    pattern = series.copy()
    pattern.index = pd.MultiIndex.from_arrays(
        [pattern.index.month, pattern.index.day], names=["month", "day"])
    pattern = pattern[~pattern.index.duplicated()]
    aligned = pattern.reindex(pd.MultiIndex.from_arrays([axis.month, axis.day]))
    aligned.index = axis
    # A leap-day gap (pattern year not a leap year, target year is) carries the
    # previous day forward rather than dropping to zero.
    return aligned.ffill()


# --------------------------------------------------------------------------
# USGS download
# --------------------------------------------------------------------------

def _cache_path(site: str) -> str:
    return os.path.join(CACHE_DIR, f"USGS_{site}_{ELEV_PARAMETER_CD}_daily.csv")


def _read_cache(site: str) -> pd.Series | None:
    path = _cache_path(site)
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["date"])
    return pd.Series(_numeric(df["elevation_ft"]).to_numpy(), index=pd.DatetimeIndex(df["date"]),
                     name="elevation_ft")


def _write_cache(site: str, series: pd.Series) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    series.rename("elevation_ft").rename_axis("date").to_frame().to_csv(_cache_path(site))


def configure_api_key() -> str | None:
    """Make the USGS API key available to dataretrieval.

    Looked up in order: the API_USGS_PAT environment variable, the USGS_API_KEY
    alias, then a one-line file named usgs_api_key.txt in the data directory.
    Keeping the key in the environment or in that ignored file is what stops a
    credential from being committed; nothing here writes it back to disk.
    """
    key = os.environ.get(API_KEY_ENV)
    source = API_KEY_ENV
    if not key:
        key, source = os.environ.get(API_KEY_ALIAS), API_KEY_ALIAS
    if not key:
        path = os.path.join(DATA_DIR, API_KEY_FILENAME)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                key = handle.read().strip()
            source = path

    if not key:
        print(f"[WARNING] No USGS API key found. Downloads will be rate limited "
              f"or rejected.\n"
              f"          Set {API_KEY_ENV}, or save the key as "
              f"{os.path.join(DATA_DIR, API_KEY_FILENAME)}.\n"
              f"          Register at {API_KEY_SIGNUP}")
        return None

    os.environ[API_KEY_ENV] = key
    print(f"[INFO] USGS API key loaded from {source} (ends {key[-4:]})")
    return key


def _clean_series(frame: pd.DataFrame) -> pd.Series:
    """Turn a waterdata response frame into a clean float Series on time."""
    if frame is None or frame.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], name="date"))

    value_col = "value" if "value" in frame.columns else frame.columns[-1]
    time_col = "time" if "time" in frame.columns else frame.columns[0]
    stamps = pd.to_datetime(frame[time_col], errors="coerce", utc=True)
    series = pd.Series(_numeric(frame[value_col]).to_numpy(),
                       index=pd.DatetimeIndex(stamps.dt.tz_localize(None)))
    series = series[series.index.notna()].sort_index()
    # USGS missing-value standins used by the DP download script.
    series[series < -9000] = np.nan
    series[series.isin([-901, -902])] = np.nan
    return series.dropna()


def _daily_values(site: str, start: str, end: str, statistic: bool) -> pd.DataFrame:
    """One call to the daily-values endpoint.

    The daily endpoint's `time` field is the date an observation represents, so
    it is queried with plain dates rather than the timestamps the continuous
    endpoint wants.
    """
    from dataretrieval import waterdata

    kwargs = dict(monitoring_location_id=f"USGS-{site}",
                  parameter_code=ELEV_PARAMETER_CD,
                  time=f"{start}/{end}",
                  skip_geometry=True)
    if statistic:
        kwargs["statistic_id"] = DAILY_MEAN_STAT_CD
    frame, _ = waterdata.get_daily(**kwargs)
    if frame is not None and not frame.empty and not statistic:
        # Without the filter the response can carry min/max alongside the mean.
        if "statistic_id" in frame.columns:
            mean_only = frame[frame["statistic_id"].astype(str).str.contains("00003")]
            if not mean_only.empty:
                frame = mean_only
    return frame


def _continuous_daily_mean(site: str, start: str, end: str, label: str) -> pd.Series:
    """Continuous (instantaneous) record, averaged to a daily mean.

    This is the path the DP download script uses for elevation. The continuous
    endpoint accepts at most three years per call, so the record is walked one
    calendar year at a time and each chunk is collapsed to daily means before
    the next is fetched, which keeps 15-minute data for a decade manageable.
    """
    from dataretrieval import waterdata

    first, last = pd.Timestamp(start), pd.Timestamp(end)
    chunks = []
    for year in range(first.year, last.year + 1):
        lo = max(first, pd.Timestamp(f"{year}-01-01"))
        hi = min(last, pd.Timestamp(f"{year}-12-31"))
        frame, _ = waterdata.get_continuous(
            monitoring_location_id=f"USGS-{site}",
            parameter_code=ELEV_PARAMETER_CD,
            time=(f"{lo.strftime('%Y-%m-%d')}T00:00:00Z/"
                  f"{hi.strftime('%Y-%m-%d')}T23:59:59Z"),
        )
        chunk = _clean_series(frame)
        if not chunk.empty:
            chunks.append(chunk.resample("D").mean().dropna())
        print(f"[INFO]   {label} {year}: {len(chunk):,} continuous values")

    if not chunks:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], name="date"))
    return pd.concat(chunks).sort_index()


def download_elevation(site: str, start: str, end: str, label: str = "") -> pd.Series:
    """Daily mean forebay elevation for one USGS site, as a float Series.

    Uses the modernized USGS Water Data API (dataretrieval.waterdata), which
    replaces the legacy WaterServices endpoints that USGS is retiring. Site
    numbers are passed in the "USGS-#######" monitoring_location_id form.

    Reservoir elevation is not always published as a daily statistic, so this
    tries the cheap daily endpoint first and falls back to averaging the
    continuous record, which is what the DP download script reads. The path
    that produced the data is printed so a silent empty result is impossible.
    """
    attempts = (
        ("daily mean", lambda: _clean_series(_daily_values(site, start, end, True))),
        ("daily, any statistic",
         lambda: _clean_series(_daily_values(site, start, end, False))),
    )
    for how, call in attempts:
        try:
            series = call()
        except Exception as exc:
            print(f"[INFO]   {label} {how}: failed ({exc})")
            continue
        if not series.empty:
            print(f"[INFO]   {label} via {how} endpoint")
            return series
        print(f"[INFO]   {label} {how}: empty, trying next")

    print(f"[INFO]   {label} falling back to the continuous record")
    series = _continuous_daily_mean(site, start, end, label)
    if not series.empty:
        print(f"[INFO]   {label} via continuous endpoint, averaged to daily")
    return series


def _missing_spans(cached: pd.Series, start: pd.Timestamp, end: pd.Timestamp):
    """The parts of [start, end] the cache does not already cover.

    Only the head and tail matter. Interior gaps are real gaps in the USGS
    record -- days the gage did not report -- and re-requesting them every run
    would download the whole span again to learn nothing.
    """
    if cached is None or cached.empty:
        return [(start, end)]
    spans = []
    if start < cached.index.min():
        spans.append((start, cached.index.min() - pd.Timedelta(days=1)))
    if end > cached.index.max():
        spans.append((cached.index.max() + pd.Timedelta(days=1), end))
    return spans


def get_elevations(sites: pd.DataFrame, start: str, end: str, refresh: bool = False):
    """Elevation series per project, downloading only what the cache lacks.

    USGS requires an API key partly to discourage repeated identical downloads,
    so a cached day is never requested twice: a run tops the cache up with the
    days on either end of it and reuses everything already stored. --refresh
    forces the whole span to be downloaded again.
    """
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    elevations = {}

    for row in sites.itertuples():
        label = f"{row.code} ({row.name})"
        cached = None if refresh else _read_cache(row.site)
        spans = [(first, last)] if refresh else _missing_spans(cached, first, last)

        if cached is not None and not cached.empty and not spans:
            covered = cached.loc[first:last]
            print(f"[INFO] {label}: {len(covered):,} values from cache "
                  f"({cached.index.min().date()} to {cached.index.max().date()}), "
                  f"no download needed")
            elevations[row.code] = cached
            continue

        if cached is not None and not cached.empty:
            print(f"[INFO] {label}: cache holds {cached.index.min().date()} to "
                  f"{cached.index.max().date()}; fetching "
                  + ", ".join(f"{a.date()} to {b.date()}" for a, b in spans))

        pieces = [] if cached is None or refresh else [cached]
        failed = False
        for span_start, span_end in spans:
            try:
                pieces.append(download_elevation(
                    row.site, span_start.strftime("%Y-%m-%d"),
                    span_end.strftime("%Y-%m-%d"), label))
            except Exception as exc:
                print(f"[ERROR] {label} site {row.site} download failed: {exc}")
                failed = True
                break

        merged = pd.concat(pieces) if pieces else pd.Series(dtype=float)
        if not merged.empty:
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        if merged.empty:
            print(f"[WARNING] {label} returned no data.")
            continue

        if not failed and (cached is None or len(merged) != len(cached)):
            _write_cache(row.site, merged)
            print(f"[INFO] {label}: cached {len(merged):,} daily values "
                  f"({merged.index.min().date()} to {merged.index.max().date()})")
        elevations[row.code] = merged
    return elevations


# --------------------------------------------------------------------------
# Adjustment
# --------------------------------------------------------------------------

def _carried_deficit(daily_af: pd.Series, storage_obs: pd.Series) -> pd.Series:
    """The withdrawal deficit carried into each day, in acre-feet.

    Withdrawal accumulates while the demand is on. Once it stops, the deficit is
    repaid out of the reservoir's own drawdown: every acre-foot released after
    the season is an acre-foot that would instead have covered the withdrawal,
    so the operator reaches the same target pool having released that much less.

    Carrying the full season's withdrawal to 31 December instead would subtract
    it from a pool that has already drawn down to its winter minimum, and the
    adjusted trace would fall off the bottom of the rating curve months after
    the last withdrawal.

    Days with no observed elevation neither accrue nor repay -- the release over
    a gap in the record is unknown, so the deficit is simply held.
    """
    take = daily_af.to_numpy(dtype=float)
    storage = storage_obs.to_numpy(dtype=float)
    carried = np.zeros(len(take))

    deficit = 0.0
    for i in range(len(take)):
        # Today's withdrawal is felt tomorrow, so 01Jan carries no deficit.
        carried[i] = deficit
        deficit += take[i]
        if take[i] > 0.0 or deficit <= 0.0 or i + 1 >= len(take):
            continue
        release = storage[i] - storage[i + 1]
        if np.isfinite(release) and release > 0.0:
            deficit = max(0.0, deficit - release)

    return pd.Series(carried, index=daily_af.index)


def adjust_year(elevation: pd.Series, demand_cfs: pd.Series, curve: RatingCurve,
                year: int) -> pd.DataFrame:
    """Deplete one year of observed elevation by the cumulative withdrawal.

    The observed elevation is converted to storage on the reservoir's rating
    curve, the withdrawal demand is accumulated in acre-feet from 01Jan, and
    the difference is converted back to an elevation. 01Jan itself carries no
    deficit - the first day's withdrawal is felt on 02Jan - so the two traces
    start together and diverge over the year, then converge again as the
    reservoir draws down after the demand season (see `_carried_deficit`).
    """
    start = pd.Timestamp(year=year, month=1, day=1)
    end = pd.Timestamp(year=year, month=12, day=31)

    observed = elevation.loc[(elevation.index >= start) & (elevation.index <= end)]
    if observed.empty:
        return pd.DataFrame()
    # A continuous daily axis from 01Jan through the last observed day, so
    # gaps in the record stay visible instead of being closed up.
    axis = pd.date_range(start, observed.index.max(), freq="D")
    observed = observed.reindex(axis)

    demand = demand_cfs.reindex(axis).fillna(0.0).clip(lower=0.0)
    daily_af = demand * CFS_DAY_TO_ACRE_FT
    withdrawn_af = daily_af.cumsum().shift(1).fillna(0.0)

    storage_obs = pd.Series(curve.to_storage(observed.to_numpy()), index=axis)
    storage_obs[observed.isna()] = np.nan
    deficit_af = _carried_deficit(daily_af, storage_obs)
    storage_adj = (storage_obs - deficit_af).clip(lower=curve.min_storage)
    elevation_adj = pd.Series(curve.to_elevation(storage_adj.to_numpy()), index=axis)
    elevation_adj[storage_adj.isna()] = np.nan

    # Days where the demand exceeded the storage available above the bottom of
    # the rating curve. The adjusted trace is pinned to the curve minimum there,
    # so it understates the shortfall and should not be read as a real elevation.
    unclipped = storage_obs - deficit_af
    at_floor = unclipped < curve.min_storage

    return pd.DataFrame({
        "elev_observed_ft": observed,
        "elev_adjusted_ft": elevation_adj,
        "storage_observed_af": storage_obs,
        "storage_adjusted_af": storage_adj,
        "demand_cfs": demand,
        "cumulative_withdrawal_af": withdrawn_af,
        "deficit_af": deficit_af,
        "elev_change_ft": elevation_adj - observed,
        "at_rating_floor": at_floor.fillna(False),
    })


# --------------------------------------------------------------------------
# Plotting
# --------------------------------------------------------------------------

def plot_year(frame: pd.DataFrame, code: str, name: str, year: int, out_path: str) -> None:
    """One PNG: observed and adjusted elevation for one reservoir-year."""
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=150)
    fig.patch.set_facecolor(COLOR_SURFACE)
    ax.set_facecolor(COLOR_SURFACE)

    if "elev_rule_ft" in frame.columns and frame["elev_rule_ft"].notna().any():
        ax.plot(frame.index, frame["elev_rule_ft"], color=COLOR_RULE,
                linewidth=1.4, label="Rule curve", zorder=2)
    ax.plot(frame.index, frame["elev_observed_ft"], color=COLOR_OBSERVED,
            linewidth=2.0, label="Observed", zorder=3)
    ax.plot(frame.index, frame["elev_adjusted_ft"], color=COLOR_ADJUSTED,
            linewidth=2.0, label="Adjusted for withdrawal", zorder=4)

    drop = frame["elev_change_ft"].dropna()
    if not drop.empty:
        worst = drop.min()
        total_af = frame["cumulative_withdrawal_af"].dropna()
        total = total_af.iloc[-1] if not total_af.empty else 0.0
        subtitle = (f"Maximum drawdown {abs(worst):,.2f} ft   ·   "
                    f"cumulative withdrawal {total:,.0f} acre-ft")
        floor_days = int(frame["at_rating_floor"].sum())
        if floor_days:
            subtitle += (f"   ·   demand exceeds available storage on "
                         f"{floor_days} day{'s' if floor_days != 1 else ''}")
    else:
        subtitle = "No overlapping elevation and demand record"

    ax.set_title(f"{name} ({code}) — {year} forebay elevation",
                 fontsize=14, fontweight="bold", color=COLOR_TEXT, loc="left", pad=18)
    ax.text(0.0, 1.015, subtitle, transform=ax.transAxes, fontsize=9.5,
            color=COLOR_TEXT_MUTED, ha="left", va="bottom")
    ax.set_ylabel("Elevation (ft, NGVD29)", fontsize=10, color=COLOR_TEXT_MUTED)

    ax.grid(True, axis="y", color=COLOR_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(COLOR_GRID)
    ax.tick_params(colors=COLOR_TEXT_MUTED, labelsize=9)

    ax.set_xlim(frame.index[0], frame.index[-1])
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))

    # Below the axes: the traces fill the plot area at some point in every
    # year, so any in-axes corner eventually collides with the data.
    legend = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.10),
                       frameon=False, fontsize=9.5, ncol=3)
    for text in legend.get_texts():
        text.set_color(COLOR_TEXT_MUTED)

    fig.tight_layout()
    fig.savefig(out_path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def probe_sites(sites: pd.DataFrame) -> int:
    """Report what USGS actually publishes for each site.

    Run this when a download comes back empty: it lists the time series each
    monitoring location offers, so the parameter code and statistic in use can
    be checked against what the script asks for.
    """
    from dataretrieval import waterdata

    for row in sites.itertuples():
        print(f"\n=== {row.code} ({row.name})  USGS-{row.site} ===")
        try:
            meta, _ = waterdata.get_time_series_metadata(
                monitoring_location_id=f"USGS-{row.site}")
        except Exception as exc:
            print(f"  metadata request failed: {exc}")
            continue
        if meta is None or meta.empty:
            print("  no time series metadata returned")
            continue
        keep = [c for c in ("parameter_code", "parameter_name", "statistic_id",
                            "computation_period_identifier", "begin", "end",
                            "unit_of_measure")
                if c in meta.columns]
        table = meta[keep] if keep else meta
        if "parameter_code" in table.columns:
            elev = table[table["parameter_code"].astype(str) == ELEV_PARAMETER_CD]
            if not elev.empty:
                print(f"  parameter {ELEV_PARAMETER_CD} series:")
                print(elev.to_string(index=False, max_colwidth=32))
                continue
            print(f"  no parameter {ELEV_PARAMETER_CD}; available parameters:")
            print("   ", ", ".join(sorted(table["parameter_code"].astype(str).unique())))
        print(table.head(25).to_string(index=False, max_colwidth=32))
    return 0


def _cli_args(argv):
    """Command-line arguments, minus anything a notebook kernel injected.

    Jupyter launches as `ipykernel_launcher.py --f=...kernel.json`, which
    argparse rejects. Under a kernel, keep only the flags this script defines.
    """
    if argv is not None:
        return list(argv)
    args = list(sys.argv[1:])
    if "ipykernel" not in sys.modules:
        return args
    known = ("--refresh", "--years", "--out", "--data-dir", "--probe")
    cleaned, keep = [], False
    for arg in args:
        if arg.startswith("-"):
            keep = arg.split("=")[0] in known
        if keep:
            cleaned.append(arg)
    return cleaned


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true",
                        help="re-download from USGS instead of using data/cache/")
    parser.add_argument("--years", type=int, nargs="+",
                        help=f"years to process (default {FIRST_YEAR}-{LAST_YEAR})")
    parser.add_argument("--out", default=OUT_DIR, help="output directory (default ../out)")
    parser.add_argument("--probe", action="store_true",
                        help="list the time series USGS publishes for each site "
                             "and exit; use this when a download returns no data")
    parser.add_argument("--data-dir", default=None,
                        help="read inputs from here instead of ../data (for testing "
                             "against generated inputs, see make_dummy_inputs.py)")
    args = parser.parse_args(_cli_args(argv))

    if args.data_dir:
        global DATA_DIR, CACHE_DIR, ELEV_DICT_PATH, STOR_RATINGS_PATH
        global DEMAND_PATH, RULE_CURVE_PATH
        DATA_DIR = os.path.abspath(args.data_dir)
        CACHE_DIR = os.path.join(DATA_DIR, "cache")
        ELEV_DICT_PATH = os.path.join(DATA_DIR, "WIL_ELEV_DICT.csv")
        STOR_RATINGS_PATH = os.path.join(DATA_DIR, "STOR_RATINGS.xlsx")
        DEMAND_PATH = os.path.join(DATA_DIR, "ALT_WithdrawalDemand.csv")
        RULE_CURVE_PATH = os.path.join(DATA_DIR, "RuleCurves.csv")
        print(f"[INFO] Reading inputs from {DATA_DIR}")

    years = args.years or list(range(FIRST_YEAR, LAST_YEAR + 1))

    for path in (ELEV_DICT_PATH, STOR_RATINGS_PATH, DEMAND_PATH):
        if not os.path.exists(path):
            print(f"[ERROR] Missing required input: {path}")
            return 1

    configure_api_key()

    sites = read_elev_dict(ELEV_DICT_PATH)
    projects = dict(zip(sites["code"], sites["name"]))
    print(f"[INFO] {len(projects)} projects: {', '.join(sorted(projects))}\n")

    if args.probe:
        return probe_sites(sites)

    curves = read_stor_ratings(projects, STOR_RATINGS_PATH)
    print()
    demand = read_demand(projects, DEMAND_PATH)
    print()
    rule_curves = read_rule_curves(projects, RULE_CURVE_PATH)
    print()

    start = f"{min(years)}-01-01"
    end = min(pd.Timestamp.today().normalize(), pd.Timestamp(f"{max(years)}-12-31"))
    elevations = get_elevations(sites, start, end.strftime("%Y-%m-%d"), refresh=args.refresh)
    print()

    written = 0
    for year in years:
        year_dir = os.path.join(args.out, str(year))
        os.makedirs(year_dir, exist_ok=True)
        for code, name in sorted(projects.items(), key=lambda p: p[1]):
            if code not in elevations:
                continue
            if code not in curves:
                print(f"[WARNING] {year} {code}: no rating curve; skipped.")
                continue

            resolved = demand_for_project(demand, code)
            if resolved is None:
                print(f"[WARNING] {year} {code}: no demand column; skipped.")
                continue
            demand_cfs, source = resolved
            if source != code:
                print(f"[INFO] {year} {code}: demand column is {source}")

            frame = adjust_year(elevations[code], demand_for_year(demand_cfs, year),
                                curves[code], year)
            if frame.empty:
                print(f"[WARNING] {year} {code}: no elevation record; skipped.")
                continue

            if code in rule_curves:
                frame["elev_rule_ft"] = rule_curve_for_year(
                    rule_curves[code], year).reindex(frame.index)

            png = os.path.join(year_dir, f"{code}_{year}_elevation.png")
            plot_year(frame, code, name, year, png)
            frame.round(3).rename_axis("date").to_csv(
                os.path.join(year_dir, f"{code}_{year}_elevation.csv"))
            written += 1
            worst = frame["elev_change_ft"].min()
            print(f"[INFO] {year} {code}: {os.path.relpath(png, args.out)} "
                  f"(max drawdown {abs(worst):,.2f} ft)")

    print(f"\n[INFO] Wrote {written} reservoir-year plots under {args.out}")
    return 0


if __name__ == "__main__":
    status = main()
    # SystemExit inside a notebook surfaces as an ugly traceback; just return.
    if "ipykernel" not in sys.modules:
        sys.exit(status)
