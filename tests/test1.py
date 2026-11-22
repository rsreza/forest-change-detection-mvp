import xarray as xr
from pathlib import Path

cube = xr.open_dataarray("data/ndvi_cube.nc")
print(cube.time.values.min(), cube.time.values.max(), cube.sizes["time"])
print("cached tifs:", len(list(Path("data/cache").glob("*.tif"))))