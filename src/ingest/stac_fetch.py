import os
import re
from pathlib import Path

import numpy as np
import pystac_client
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CRED_FILE = PROJECT_ROOT / "S3 Credentials.txt"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

AOI_BBOX = [10.5, 51.7, 10.8, 51.9]      # lon/lat (WGS84)
DATE_RANGE = "2018-01-01/2019-12-31"     # baseline year + monitoring year
MAX_CLOUD = 60                           # whole-tile cover; SCL masks pixels later
TILE = "32UPC"                           # one tile avoids mosaicking
ASSET_KEYS = ("B04_10m", "B08_10m", "SCL_20m")


# ---------- credentials ----------
def read_text_any_encoding(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load_credentials(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Credentials file not found: {path}")
    lines = [
        l.strip() for l in read_text_any_encoding(path).splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    access = secret = None
    for line in lines:
        m = re.match(r"^(?:export\s+)?([A-Za-z0-9_ .-]+?)\s*[:=]\s*(.+)$", line)
        if not m:
            continue
        name = m.group(1).lower().replace(" ", "_")
        value = m.group(2).strip().strip("'\"")
        if "access" in name:
            access = value
        elif "secret" in name:
            secret = value
    if (not access or not secret) and len(lines) == 2 and not any(
        re.search(r"[:=]", l) for l in lines
    ):
        access, secret = lines[0], lines[1]
    if not access or not secret:
        raise RuntimeError(f"Could not find both keys in {path}.")
    return access, secret


def configure_gdal_s3(access_key: str, secret_key: str):
    # Newer rasterio rejects AWS_* options in rasterio.Env(), so use env vars.
    os.environ["AWS_ACCESS_KEY_ID"] = access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
    os.environ["AWS_S3_ENDPOINT"] = "eodata.dataspace.copernicus.eu"
    os.environ["AWS_VIRTUAL_HOSTING"] = "FALSE"
    os.environ["AWS_HTTPS"] = "YES"
    os.environ["AWS_DEFAULT_REGION"] = "default"


# ---------- STAC helpers ----------
def parse_id(item_id: str):
    """Return (sensing_time, tile, processing_baseline, processing_time) from item id."""
    m = re.search(
        r"MSIL2A_(\d{8}T\d{6})_(N\d{4})_R\d{3}_T(\d{2}[A-Z]{3})_(\d{8}T\d{6})", item_id
    )
    if not m:
        return None
    return m.group(1), m.group(3), m.group(2), m.group(4)


def select_items(catalog):
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        bbox=AOI_BBOX,
        datetime=DATE_RANGE,
        query={"eo:cloud_cover": {"lt": MAX_CLOUD}},
    )
    best = {}  # (sensing_time, tile) -> item, keeping the latest reprocessing
    for item in search.items():
        parsed = parse_id(item.id)
        if parsed is None:
            continue
        sensing, tile, _, proc_time = parsed
        if tile != TILE:
            continue
        key = (sensing, tile)
        if key not in best or proc_time > parse_id(best[key].id)[3]:
            best[key] = item
    return [best[k] for k in sorted(best)]


# ---------- windowed extraction ----------
def aoi_window_10m(src, bbox) -> Window:
    """AOI window on the 10 m grid, snapped to even offsets/sizes so it lines
    up exactly with the 20 m grid (needed for SCL)."""
    left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *bbox)
    w = from_bounds(left, bottom, right, top, transform=src.transform)
    col0 = max(int(np.floor(w.col_off / 2) * 2), 0)
    row0 = max(int(np.floor(w.row_off / 2) * 2), 0)
    col1 = min(int(np.ceil((w.col_off + w.width) / 2) * 2), src.width)
    row1 = min(int(np.ceil((w.row_off + w.height) / 2) * 2), src.height)
    return Window(col0, row0, col1 - col0, row1 - row0)


def extract_scene(item, out_path: Path):
    missing = [k for k in ASSET_KEYS if k not in item.assets]
    if missing:
        raise KeyError(f"Missing assets {missing}. Available: {list(item.assets)}")

    sensing, tile, baseline, _ = parse_id(item.id)

    with rasterio.Env():
        with rasterio.open(item.assets["B04_10m"].href) as src:
            win = aoi_window_10m(src, AOI_BBOX)
            red = src.read(1, window=win)
            transform = src.window_transform(win)
            crs = src.crs
        with rasterio.open(item.assets["B08_10m"].href) as src:
            nir = src.read(1, window=win)
        with rasterio.open(item.assets["SCL_20m"].href) as src:
            win20 = Window(win.col_off // 2, win.row_off // 2,
                           win.width // 2, win.height // 2)
            scl = src.read(1, window=win20)

    # nearest-neighbour upsample SCL from 20 m to 10 m
    scl = np.repeat(np.repeat(scl, 2, axis=0), 2, axis=1)
    stack = np.stack([red, nir, scl.astype(np.uint16)]).astype(np.uint16)

    tmp_path = out_path.with_suffix(".tmp")
    with rasterio.open(
        tmp_path, "w", driver="GTiff",
        height=stack.shape[1], width=stack.shape[2], count=3, dtype="uint16",
        crs=crs, transform=transform, compress="deflate", predictor=2,
    ) as dst:
        dst.write(stack)
        for i, name in enumerate(("B04", "B08", "SCL"), start=1):
            dst.set_band_description(i, name)
        dst.update_tags(
            item_id=item.id,
            sensing_time=sensing,
            tile=tile,
            processing_baseline=baseline,
        )
    os.replace(tmp_path, out_path)


def main():
    access_key, secret_key = load_credentials(CRED_FILE)
    configure_gdal_s3(access_key, secret_key)
    print(f"Credentials loaded (access key length {len(access_key)}, "
          f"secret key length {len(secret_key)})")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    catalog = pystac_client.Client.open("https://stac.dataspace.copernicus.eu/v1")
    items = select_items(catalog)
    print(f"Selected {len(items)} scenes for tile {TILE}, {DATE_RANGE}")

    ok = skipped = failed = 0
    for i, item in enumerate(items, start=1):
        out_path = CACHE_DIR / f"{item.id}.tif"
        if out_path.exists():
            skipped += 1
            continue
        try:
            extract_scene(item, out_path)
            ok += 1
            print(f"[{i}/{len(items)}] cached {out_path.name}")
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(items)}] FAILED {item.id}: {e}")

    total_mb = sum(p.stat().st_size for p in CACHE_DIR.glob("*.tif")) / 1e6
    print(f"Done. new={ok} skipped={skipped} failed={failed} cache={total_mb:.0f} MB")


if __name__ == "__main__":
    main()