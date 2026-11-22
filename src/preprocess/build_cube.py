import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preprocess.cloud_mask import valid_mask
from preprocess.ndvi import compute_ndvi

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUT_PATH = PROJECT_ROOT / "data" / "ndvi_cube.nc"
MIN_VALID_FRACTION = 0.05   # drop scenes that are almost entirely masked


def main():
    arrays, times = [], []
    transform = crs = None

    for tif in sorted(CACHE_DIR.glob("*.tif")):
        with rasterio.open(tif) as src:
            red, nir, scl = src.read(1), src.read(2), src.read(3)
            tags = src.tags()
            if transform is None:
                transform, crs = src.transform, src.crs
            elif src.transform != transform:
                print(f"SKIP {tif.name}: grid differs from first scene")
                continue

        ndvi = compute_ndvi(red, nir, tags["processing_baseline"])
        ok = valid_mask(scl)
        ndvi[~ok] = np.nan

        frac = np.isfinite(ndvi).mean()
        t = datetime.strptime(tags["sensing_time"], "%Y%m%dT%H%M%S")
        if frac < MIN_VALID_FRACTION:
            print(f"DROP {t.date()}: only {frac:.1%} valid pixels")
            continue

        print(f"{t.date()}  baseline {tags['processing_baseline']}  valid {frac:.1%}")
        arrays.append(ndvi)
        times.append(np.datetime64(t))

    if not arrays:
        raise RuntimeError("No usable scenes found in data/cache/")

    h, w = arrays[0].shape
    x = transform.c + transform.a * (np.arange(w) + 0.5)
    y = transform.f + transform.e * (np.arange(h) + 0.5)

    cube = xr.DataArray(
        np.stack(arrays),
        dims=("time", "y", "x"),
        coords={"time": times, "y": y, "x": x},
        name="ndvi",
        attrs={"crs": str(crs)},
    ).sortby("time")

    cube.to_netcdf(OUT_PATH)
    print(f"Saved {cube.sizes['time']} scenes to {OUT_PATH}")


if __name__ == "__main__":
    main()