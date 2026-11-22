import argparse
import calendar
import sys
import time
from pathlib import Path

import pystac_client

# Reuse the working pieces from stac_fetch.py (same folder)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stac_fetch import (  # noqa: E402
    AOI_BBOX, CACHE_DIR, CRED_FILE, MAX_CLOUD, TILE,
    configure_gdal_s3, extract_scene, load_credentials, parse_id,
)

DEFAULT_START = "2018-01"
DEFAULT_END = "2019-12"


def parse_ym(s):
    y, m = s.split("-")
    return int(y), int(m)


def months(start, end):
    y, m = start
    while (y, m) <= end:
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def search_month(catalog, y, m, retries=3):
    last = calendar.monthrange(y, m)[1]
    dt = f"{y}-{m:02d}-01/{y}-{m:02d}-{last:02d}"
    for attempt in range(1, retries + 1):
        try:
            search = catalog.search(
                collections=["sentinel-2-l2a"],
                bbox=AOI_BBOX,
                datetime=dt,
                query={"eo:cloud_cover": {"lt": MAX_CLOUD}},
            )
            return list(search.items())
        except Exception as e:
            print(f"  search {dt} attempt {attempt} failed: {e}")
            time.sleep(2 * attempt)
    return None


def cached_keys():
    """(sensing_time, tile) of everything already in the cache."""
    keys = set()
    for p in CACHE_DIR.glob("*.tif"):
        parsed = parse_id(p.stem)
        if parsed:
            keys.add((parsed[0], parsed[1]))
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-only", action="store_true",
                    help="report missing scenes without downloading")
    ap.add_argument("--start", default=DEFAULT_START, help="first month, YYYY-MM")
    ap.add_argument("--end", default=DEFAULT_END, help="last month, YYYY-MM")
    args = ap.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in CACHE_DIR.glob("*.tmp"):
        stale.unlink()                      # leftovers from interrupted writes

    have = cached_keys()
    print(f"Cache: {len(have)} scenes already downloaded\n")

    catalog = pystac_client.Client.open("https://stac.dataspace.copernicus.eu/v1")

    print(f"{'month':8} {'catalog':>8} {'cached':>7} {'missing':>8}")
    missing_items = []
    failed_months = []
    for y, m in months(parse_ym(args.start), parse_ym(args.end)):
        items = search_month(catalog, y, m)
        if items is None:
            print(f"{y}-{m:02d}   SEARCH FAILED")
            failed_months.append(f"{y}-{m:02d}")
            continue

        best = {}   # (sensing, tile) -> item, keeping the latest reprocessing
        for item in items:
            parsed = parse_id(item.id)
            if parsed is None or parsed[1] != TILE:
                continue
            key = (parsed[0], parsed[1])
            if key not in best or parsed[3] > parse_id(best[key].id)[3]:
                best[key] = item

        miss = [it for k, it in best.items() if k not in have]
        missing_items += miss
        print(f"{y}-{m:02d}   {len(best):>8} {len(best) - len(miss):>7} {len(miss):>8}")

    missing_items.sort(key=lambda it: parse_id(it.id)[0])
    print(f"\nTotal missing: {len(missing_items)}")
    if failed_months:
        print(f"Months whose search failed (re-run to retry): {failed_months}")

    if args.check_only or not missing_items:
        return

    access_key, secret_key = load_credentials(CRED_FILE)
    configure_gdal_s3(access_key, secret_key)
    print("Credentials loaded. Downloading...\n")

    ok, failed = 0, []
    for i, item in enumerate(missing_items, start=1):
        out_path = CACHE_DIR / f"{item.id}.tif"
        for attempt in (1, 2):
            try:
                extract_scene(item, out_path)
                ok += 1
                print(f"[{i}/{len(missing_items)}] cached {out_path.name}")
                break
            except Exception as e:
                tmp = out_path.with_suffix(".tmp")
                if tmp.exists():
                    tmp.unlink()
                if attempt == 2:
                    failed.append(item.id)
                    print(f"[{i}/{len(missing_items)}] FAILED {item.id}: {e}")
                else:
                    time.sleep(3)

    total_mb = sum(p.stat().st_size for p in CACHE_DIR.glob("*.tif")) / 1e6
    print(f"\nDone. downloaded={ok} failed={len(failed)} cache={total_mb:.0f} MB")
    if failed:
        print("Failed items (run the script again to retry):")
        for f in failed:
            print("  ", f)


if __name__ == "__main__":
    main()