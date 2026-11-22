import rioxarray
import xarray as xr
import numpy as np

CUBE_PATH = "data/ndvi_cube.nc"
MASK_PATH = "data/forest_mask.tif"
OUT_PATH = "data/ndvi_cube_forest.nc"

print("Loading NDVI cube...")
cube = xr.open_dataarray(CUBE_PATH)

# Get cube bounds for cropping
x_min, x_max = float(cube.x.min()), float(cube.x.max())
y_min, y_max = float(cube.y.min()), float(cube.y.max())
print(f"Cube bounds: x=[{x_min}, {x_max}], y=[{y_min}, {y_max}]")

print("Loading and cropping forest mask...")
# Key fix: use clip_box to only load the AOI region, not the whole Germany file
mask = rioxarray.open_rasterio(MASK_PATH, masked=True).squeeze()
mask_cropped = mask.rio.clip_box(
    minx=x_min - 100,   # small buffer to avoid edge issues
    miny=y_min - 100,
    maxx=x_max + 100,
    maxy=y_max + 100,
)
print(f"Cropped mask shape: {mask_cropped.shape}")

# Check unique values on the cropped mask
vals, counts = np.unique(mask_cropped.values, return_counts=True)
print("Unique values on cropped mask:", dict(zip(vals.tolist(), counts.tolist())))

# Align to cube grid (same CRS, only resampling)
print("Aligning mask to cube grid...")
mask_aligned = mask_cropped.rio.reproject_match(cube)

# Forest condition: value == 1 (adjust if unique values show otherwise)
forest_mask = (mask_aligned == 1)

total = cube.sizes["y"] * cube.sizes["x"]
n_forest = int(forest_mask.sum().values)
print(f"Forest pixels: {n_forest} / {total} ({100*n_forest/total:.1f}%)")

print("Applying mask...")
cube_masked = cube.where(forest_mask)
cube_masked.attrs.update(cube.attrs)

cube_masked.to_netcdf(OUT_PATH)
print(f"Saved {OUT_PATH}")