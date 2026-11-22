import numpy as np
import xarray as xr
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape
from sqlalchemy import create_engine, text
from geoalchemy2 import Geometry, WKTElement
import pandas as pd

CUBE_PATH = "data/ndvi_cube_forest.nc"
DIST_PATH = "data/disturbance_2019_forest.nc"
DATABASE_URL = "postgresql://forest:forestpulse@localhost:5432/disturbances"
MIN_AREA_HA = 0.5


def raster_to_polygons(dist_ds):
    dates = dist_ds.detection_date.values
    mask = ~np.isnat(dates)
    if mask.sum() == 0:
        return []
    cube = xr.open_dataarray(CUBE_PATH)
    y = cube.y.values
    x = cube.x.values
    transform = rasterio.transform.from_origin(x.min() - 5, y.max() + 5, 10.0, 10.0)
    conf = dist_ds.confidence.values
    density = dist_ds.obs_density_60d.values
    gap = dist_ds.max_gap_days.values
    mask_uint8 = mask.astype(np.uint8)
    polygons = []
    for geom, value in shapes(mask_uint8, transform=transform):
        if value != 1:
            continue
        poly = shape(geom)
        area_ha = poly.area / 10000.0
        if area_ha < MIN_AREA_HA:
            continue
        cy, cx = poly.centroid.y, poly.centroid.x
        row = int((y.max() - cy) / 10.0)
        col = int((cx - x.min()) / 10.0)
        row = np.clip(row, 0, dates.shape[0] - 1)
        col = np.clip(col, 0, dates.shape[1] - 1)
        det_date = dates[row, col]
        polygons.append({
            "geometry": poly,
            "detection_date": str(det_date)[:10] if not np.isnat(det_date) else None,
            "confidence": str(conf[row, col]),
            "obs_density_60d": int(density[row, col]),
            "max_gap_days": int(gap[row, col]),
            "area_ha": round(area_ha, 2),
        })
    return polygons


def write_to_postgis(polygons, engine):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS disturbance_polygons (
                id SERIAL PRIMARY KEY,
                detection_date DATE,
                confidence VARCHAR(10),
                obs_density_60d INTEGER,
                max_gap_days INTEGER,
                area_ha FLOAT,
                geom GEOMETRY(POLYGON, 32632)
            );
        """))
        conn.execute(text("DELETE FROM disturbance_polygons;"))
    if not polygons:
        print("No polygons to insert.")
        return
    df = pd.DataFrame(polygons)
    df["geom"] = df["geometry"].apply(lambda g: WKTElement(g.wkt, srid=32632))
    df = df.drop(columns=["geometry"])
    df.to_sql("disturbance_polygons", engine, if_exists="append", index=False,
              dtype={"geom": Geometry("POLYGON", srid=32632)})
    print(f"Inserted {len(df)} polygons.")


def main():
    print("Loading disturbance dataset...")
    dist_ds = xr.open_dataset(DIST_PATH)
    print("Vectorizing...")
    polygons = raster_to_polygons(dist_ds)
    print(f"Generated {len(polygons)} polygons (>= {MIN_AREA_HA} ha)")
    if polygons:
        conf_counts = {}
        for p in polygons:
            conf_counts[p["confidence"]] = conf_counts.get(p["confidence"], 0) + 1
        print("Confidence distribution:", conf_counts)
        total_ha = sum(p["area_ha"] for p in polygons)
        print(f"Total disturbed area: {total_ha:.1f} ha")
    print("Connecting to PostGIS...")
    engine = create_engine(DATABASE_URL)
    write_to_postgis(polygons, engine)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*), SUM(area_ha) FROM disturbance_polygons;")).fetchone()
        print(f"DB check: {result[0]} polygons, {result[1]:.1f} ha total")


if __name__ == "__main__":
    main()
