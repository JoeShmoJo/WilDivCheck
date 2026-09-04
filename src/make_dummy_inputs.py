# -*- coding: utf-8 -*-
"""
Build a sandbox that lets wil_div_check.py run without USGS network access.

The real STOR_RATINGS.xlsx, ALT_WithdrawalDemand.csv and WIL_ELEV_DICT.csv are
copied through unchanged, so the real parsers run against the real inputs. The
only fabricated piece is the USGS daily forebay elevation, written straight into
the sandbox's cache directory in the same format wil_div_check.py caches real
downloads in.

THE ELEVATIONS ARE FABRICATED. They are shaped like a Willamette rule curve -
low in winter, refilling through spring, full over summer, drawn down in autumn -
and confined to each project's real operating band taken from RuleCurves.csv, so
the plots look right and the arithmetic is exercised. They are not observations.
Delete the sandbox and run wil_div_check.py against data/ to use real records.

Usage
-----
    python make_dummy_inputs.py                     # writes ../sandbox/data
    python make_dummy_inputs.py --dest somewhere
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_DATA_DIR = os.path.normpath(os.path.join(HERE, "..", "data"))
DEFAULT_DEST = os.path.normpath(os.path.join(HERE, "..", "sandbox", "data"))

PASSTHROUGH = ["WIL_ELEV_DICT.csv", "STOR_RATINGS.xlsx", "ALT_WithdrawalDemand.csv"]
RULE_CURVES = "RuleCurves.csv"

FIRST_YEAR, LAST_YEAR = 2015, 2026

# USGS site numbers for the projects that get a synthetic record, keyed by the
# RuleCurves.csv column that supplies the operating band.
SITES = {
    "BLU": ("14162100", "BLUE RIVER"),
    "COT": ("14153000", "COTTAGE GROVE"),
    "CGR": ("14159400", "COUGAR"),
    "DET": ("14180500", "DETROIT"),
    "DOR": ("14155000", "DORENA"),
    "FAL": ("14150900", "FALL CREEK"),
    "FRN": ("14168000", "FERN RIDGE"),
    "GPR": ("14186100", None),           # not in RuleCurves.csv - see FALLBACK_BAND
    "HCR": ("14145100", "HILLS CREEK"),
    "LOP": ("14149000", "LOOKOUT POINT"),
}

# Green Peter's conservation pool, since RuleCurves.csv carries no column for it.
FALLBACK_BAND = {"GPR": (922.0, 1010.0)}


def operating_bands(path: str) -> dict[str, tuple[float, float]]:
    """Minimum and full pool per project, from the real rule curves."""
    bands = {}
    if os.path.exists(path):
        # The Date column of this export is unreadable (Excel wrote it as #####),
        # but the elevation columns are intact, which is all that is needed here.
        curves = pd.read_csv(path)
        curves = curves.drop(columns=[c for c in curves.columns if c.strip().lower() == "date"])
        curves = curves.apply(pd.to_numeric, errors="coerce")
        for code, (_site, column) in SITES.items():
            if column and column in curves.columns:
                series = curves[column].dropna()
                if len(series):
                    bands[code] = (float(series.min()), float(series.max()))
    for code, band in FALLBACK_BAND.items():
        bands.setdefault(code, band)
    missing = sorted(set(SITES) - set(bands))
    if missing:
        raise SystemExit(f"[ERROR] No operating band for: {', '.join(missing)}")
    return bands


def elevation_series(index: pd.DatetimeIndex, min_elev: float, full_elev: float,
                     rng: np.random.Generator) -> pd.Series:
    """A rule-curve-shaped pool confined to [min_elev, full_elev]."""
    doy = index.dayofyear.to_numpy(dtype=float)
    band = full_elev - min_elev

    # Refill 01Feb to 01Jun, hold through August, draw down to 01Dec.
    fill = np.clip((doy - 32) / (152 - 32), 0.0, 1.0)
    draw = np.clip((doy - 244) / (335 - 244), 0.0, 1.0)
    level = min_elev + band * (0.06 + 0.90 * np.clip(fill - draw, 0.0, 1.0))

    # A wet or dry year shifts the whole season; weather moves it day to day.
    years = index.year.to_numpy()
    for year in np.unique(years):
        level[years == year] += band * rng.normal(0.0, 0.035)
    wiggle = pd.Series(rng.normal(0.0, band * 0.012, len(index))).rolling(
        7, min_periods=1, center=True).mean().to_numpy()

    level = np.clip(level + wiggle, min_elev, full_elev)
    return pd.Series(np.round(level, 2), index=index, name="elevation_ft")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default=DEFAULT_DEST,
                        help=f"directory to write into (default {DEFAULT_DEST})")
    parser.add_argument("--seed", type=int, default=20260904)
    args = parser.parse_args(argv)

    dest = os.path.abspath(args.dest)
    if dest == os.path.abspath(REAL_DATA_DIR):
        print("[ERROR] Refusing to write fabricated elevations into the real data "
              "directory. Pick a different --dest.")
        return 1

    cache_dir = os.path.join(dest, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    for name in PASSTHROUGH:
        source = os.path.join(REAL_DATA_DIR, name)
        if not os.path.exists(source):
            print(f"[ERROR] Missing real input {source}")
            return 1
        shutil.copy(source, os.path.join(dest, name))
        print(f"[INFO] Copied real {name}")

    bands = operating_bands(os.path.join(REAL_DATA_DIR, RULE_CURVES))

    # End the record today so 2026 is a partial year, as it is in reality.
    index = pd.date_range(f"{FIRST_YEAR}-01-01",
                          min(pd.Timestamp.today().normalize(),
                              pd.Timestamp(f"{LAST_YEAR}-12-31")), freq="D")

    rng = np.random.default_rng(args.seed)
    for code, (site, _column) in SITES.items():
        min_elev, full_elev = bands[code]
        series = elevation_series(index, min_elev, full_elev, rng)
        path = os.path.join(cache_dir, f"USGS_{site}_62614_daily.csv")
        series.rename_axis("date").to_frame().to_csv(path)
        print(f"[INFO] Fabricated {code} ({site}): {min_elev:,.2f}-{full_elev:,.2f} ft")

    print(f"\n[INFO] {len(index):,} days, {index.min().date()} to {index.max().date()}")
    print(f"[INFO] Now run:\n"
          f"    python wil_div_check.py --data-dir ../sandbox/data --out ../sandbox/out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
