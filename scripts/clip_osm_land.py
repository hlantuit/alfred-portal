"""
One-time script: download OSM land polygons and clip to Arctic extent.
Output: data/osm_land_arctic/osm_land_arctic.shp  (committed to repo)

Run via GitHub Actions workflow clip_osm_land.yml — do not run manually
unless fiona and shapely are installed locally.
"""

import os
import sys
import zipfile

CLIP_BOUNDS = (-175.0, 60.0, -110.0, 80.0)   # minlon, minlat, maxlon, maxlat
OSM_URL     = "https://osmdata.openstreetmap.de/download/land-polygons-complete-4326.zip"
ZIP_PATH    = "/tmp/osm_land.zip"
TMP_DIR     = "/tmp/osm_land_src"
OUT_DIR     = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "osm_land_arctic")

def download(url, dest):
    import requests
    print(f"Downloading {url} …")
    r = requests.get(url, stream=True, timeout=600)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    done  = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"  {done/1e6:.0f} / {total/1e6:.0f} MB", end="\r")
    print(f"\nDownloaded {done/1e6:.0f} MB")

def main():
    import fiona
    from shapely.geometry import box, shape, mapping

    # 1. Download
    if not os.path.exists(ZIP_PATH):
        download(OSM_URL, ZIP_PATH)
    else:
        print(f"Using cached {ZIP_PATH}")

    # 2. Unzip
    print("Unzipping …")
    os.makedirs(TMP_DIR, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH) as z:
        z.extractall(TMP_DIR)

    # 3. Find the .shp
    shp_path = None
    for root, _, files in os.walk(TMP_DIR):
        for fn in files:
            if fn.endswith(".shp"):
                shp_path = os.path.join(root, fn)
                break
        if shp_path:
            break
    if not shp_path:
        print("ERROR: no .shp found in zip")
        sys.exit(1)
    print(f"Source shapefile: {shp_path}")

    # 4. Clip and write
    os.makedirs(OUT_DIR, exist_ok=True)
    out_shp = os.path.join(OUT_DIR, "osm_land_arctic.shp")
    clip_box = box(*CLIP_BOUNDS)

    print(f"Clipping to {CLIP_BOUNDS} …")
    kept = 0
    with fiona.open(shp_path) as src:
        meta = src.meta.copy()
        meta["driver"] = "ESRI Shapefile"
        with fiona.open(out_shp, "w", **meta) as dst:
            for feat in src.filter(bbox=CLIP_BOUNDS):
                geom = shape(feat["geometry"])
                clipped = geom.intersection(clip_box)
                if clipped.is_empty:
                    continue
                new_feat = dict(feat)
                new_feat["geometry"] = mapping(clipped)
                dst.write(new_feat)
                kept += 1
                if kept % 500 == 0:
                    print(f"  {kept} polygons written …")

    print(f"Done — {kept} polygons written to {out_shp}")
    # Report output size
    for fn in os.listdir(OUT_DIR):
        fp = os.path.join(OUT_DIR, fn)
        print(f"  {fn}: {os.path.getsize(fp)/1e6:.1f} MB")

if __name__ == "__main__":
    main()
