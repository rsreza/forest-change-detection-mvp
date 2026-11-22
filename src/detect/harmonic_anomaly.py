"""
Seasonal-aware forest disturbance detection — final version.

Fixes applied:
  - Reads the forest-masked cube (data/ndvi_cube_forest.nc)
  - No crude NDVI mean filter (forest mask already handles non-forest exclusion)
  - Removed linear trend term (was causing catastrophic extrapolation)
  - Bounded least_squares parameters with better initial guess
  - Wider harmonic bounds [-2, 2] to accommodate larger seasonal swings
  - Explicit finite_mask to remove residual NaT
  - Relaxed R² gate (0.05) to keep valid forest pixels
  - Growing-season filter: only count detections in June-September
    (spring detections are phenological false positives, not bark beetle)
  - Predicted-NDVI sanity check
  - Error logging to diagnose least_squares failures
"""

import numpy as np
import xarray as xr
from scipy.optimize import least_squares
from datetime import datetime

BASELINE_YEAR = 2018
MONITOR_YEAR = 2019
N_HARMONICS = 2
RESIDUAL_THRESHOLD = -0.20
PERSISTENCE = 3
MIN_BASELINE_OBS = 8
MIN_R2 = 0.05
MIN_OBS_DENSITY = 3
MAX_GAP_DAYS = 45
GROWING_SEASON_MONTHS = (6, 7, 8, 9)  # June-September only


def _harmonic_design(t_days, n_harmonics=N_HARMONICS):
    """Design matrix: intercept + annual/semi-annual harmonics. No trend term."""
    t = t_days / 365.25
    cols = [np.ones_like(t)]
    for k in range(1, n_harmonics + 1):
        cols.append(np.sin(2 * np.pi * k * t))
        cols.append(np.cos(2 * np.pi * k * t))
    return np.column_stack(cols)


def fit_harmonic_baseline(ndvi_ts, time_ts):
    """
    Fit bounded harmonic regression to a single pixel's baseline NDVI.

    Returns parameter vector or None if:
      - fewer than MIN_BASELINE_OBS valid observations
      - fitted baseline has poor R² (< MIN_R2)
      - any predicted baseline value falls outside [-0.5, 1.5]
    """
    valid = np.isfinite(ndvi_ts)
    if valid.sum() < MIN_BASELINE_OBS:
        return None

    t_days = (time_ts[valid] - time_ts[valid].min()) / np.timedelta64(1, "D")
    y = ndvi_ts[valid]

    finite_mask = np.isfinite(t_days) & np.isfinite(y)
    if finite_mask.sum() < MIN_BASELINE_OBS:
        return None
    t_days = t_days[finite_mask]
    y = y[finite_mask]

    X = _harmonic_design(t_days)
    n_params = X.shape[1]

    x0 = np.zeros(n_params)
    x0[0] = np.clip(y.mean(), 0.01, 0.99)

    lower = np.full(n_params, -2.0)
    upper = np.full(n_params, 2.0)
    lower[0], upper[0] = 0.0, 1.0

    try:
        result = least_squares(
            lambda p: X @ p - y,
            x0=x0,
            bounds=(lower, upper),
            max_nfev=300,
        )
    except Exception as e:
        if not hasattr(fit_harmonic_baseline, "_err_count"):
            fit_harmonic_baseline._err_count = 0
        if fit_harmonic_baseline._err_count < 5:
            print(f"  fit error: {type(e).__name__}: {e}")
            fit_harmonic_baseline._err_count += 1
        return None

    pred = X @ result.x
    ss_res = np.sum((y - pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = 1 - ss_res / max(ss_tot, 1e-10)
    if r2 < MIN_R2:
        return None

    if np.any(pred < -0.5) or np.any(pred > 1.5):
        return None

    return result.x


def detect(cube_path="data/ndvi_cube_forest.nc",
           output_path="data/disturbance_2019_forest.nc"):
    print("Loading forest-masked cube...")
    cube = xr.open_dataarray(cube_path)
    print(f"Cube shape: {cube.shape}")

    baseline = cube.sel(time=cube.time.dt.year == BASELINE_YEAR)
    monitor = cube.sel(time=cube.time.dt.year == MONITOR_YEAR)
    print(f"Baseline ({BASELINE_YEAR}): {baseline.sizes['time']} scenes")
    print(f"Monitor  ({MONITOR_YEAR}): {monitor.sizes['time']} scenes")

    base_ndvi = baseline.values
    base_times = baseline.time.values
    mon_ndvi = monitor.values
    mon_times = monitor.time.values

    ny, nx = cube.sizes["y"], cube.sizes["x"]
    print(f"Processing {ny}×{nx} = {ny*nx} pixels")

    detection_date = np.full((ny, nx), np.datetime64("NaT"), dtype="datetime64[ns]")
    confidence = np.full((ny, nx), "none", dtype=object)
    obs_density = np.zeros((ny, nx), dtype=np.int32)
    max_gap = np.zeros((ny, nx), dtype=np.int32)
    residual_min = np.full((ny, nx), np.nan, dtype=np.float32)

    n_fitted = 0
    n_disturbed = 0

    print("Fitting harmonic baselines per pixel...")
    for j in range(ny):
        if j % 100 == 0:
            print(f"  row {j}/{ny}")
        for i in range(nx):
            base_pixel = base_ndvi[:, j, i]

            params = fit_harmonic_baseline(base_pixel, base_times)
            if params is None:
                continue
            n_fitted += 1

            t_offsets = (mon_times - base_times.min()) / np.timedelta64(1, "D")
            predicted = _harmonic_design(t_offsets) @ params

            if np.any(predicted < -0.5) or np.any(predicted > 1.5):
                continue

            observed = mon_ndvi[:, j, i]
            valid = np.isfinite(observed)
            if valid.sum() < PERSISTENCE:
                continue

            residuals = np.where(valid, observed - predicted, np.nan)
            residual_min[j, i] = (
                np.nanmin(residuals) if np.any(np.isfinite(residuals)) else np.nan
            )

            below = (residuals < RESIDUAL_THRESHOLD) & valid
            run = 0
            detected = False
            for k in range(len(below)):
                if below[k]:
                    run += 1
                    if run >= PERSISTENCE:
                        det_month = int(str(mon_times[k])[5:7])
                        if det_month not in GROWING_SEASON_MONTHS:
                            # Spring/winter detection — likely phenological, skip
                            break
                        detection_date[j, i] = mon_times[k]
                        n_disturbed += 1
                        detected = True
                        break
                else:
                    run = 0

            if not detected:
                continue

            det_idx = int(np.where(mon_times == detection_date[j, i])[0][0])
            window_start = mon_times[det_idx] - np.timedelta64(60, "D")
            in_window = (mon_times >= window_start) & (mon_times <= mon_times[det_idx])
            n_valid_60d = int(np.sum(valid & in_window))
            obs_density[j, i] = n_valid_60d

            valid_indices = np.where(valid)[0]
            if len(valid_indices) > 1:
                gaps = np.diff(mon_times[valid_indices]).astype("timedelta64[D]").astype(int)
                max_gap[j, i] = int(gaps.max())

            if n_valid_60d < MIN_OBS_DENSITY:
                confidence[j, i] = "low"
            elif max_gap[j, i] > MAX_GAP_DAYS:
                confidence[j, i] = "sparse"
            else:
                confidence[j, i] = "high"

    pct = 100 * n_disturbed / max(n_fitted, 1)
    print(f"Fitted: {n_fitted} pixels, disturbed: {n_disturbed} ({pct:.1f}%)")

    out = xr.Dataset(
        {
            "detection_date": (("y", "x"), detection_date),
            "confidence": (("y", "x"), confidence),
            "obs_density_60d": (("y", "x"), obs_density),
            "max_gap_days": (("y", "x"), max_gap),
            "residual_min": (("y", "x"), residual_min),
        },
        coords={"y": cube.y, "x": cube.x},
        attrs={
            "crs": "EPSG:32632",
            "baseline_year": BASELINE_YEAR,
            "monitor_year": MONITOR_YEAR,
            "threshold": RESIDUAL_THRESHOLD,
            "persistence": PERSISTENCE,
            "min_r2": MIN_R2,
            "growing_season_months": str(GROWING_SEASON_MONTHS),
            "created": datetime.now().isoformat(),
        },
    )
    out.to_netcdf(output_path)
    print(f"Saved to {output_path}")
    return out


if __name__ == "__main__":
    detect()