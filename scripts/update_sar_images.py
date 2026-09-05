#!/usr/bin/env python3
"""Refresh Sentinel-1 SAR / sea-ice imagery for all communities.

Runs hourly in update_portal_data_fast.yml. A community is re-rendered
only when the Sentinel Hub catalog shows a NEW acquisition compared to
the 's1_acquired' stamp in communities/<id>/charts/satellite_meta.json,
so Sentinel Hub processing units are spent roughly once per day per
site. The hourly gh-pages workflow then copies charts/*.png into
docs/<id>/img/, which is how the images reach the dashboards.

Blocks handled (from each config.json "blocks" list):
  sentinel1       -> charts/sentinel1.png    (grayscale SAR, 300 km frame)
  sea_ice         -> charts/sea_ice.png      (ice/water estimate, 300 km)
  sea_ice_zoom    -> charts/sea_ice_zoom.png (ice/water estimate, 50 km)
  lake_river_ice  -> charts/lake_ice.png     (plain SAR, 100 km frame —
                     the ice-classification overlay is deliberately OFF:
                     it was unreliable over delta lakes and channels)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# dashboard_lib constructs a Notion client at import time; nothing here
# ever calls Notion, so a placeholder token is fine.
os.environ.setdefault("NOTION_TOKEN", "offline-unused")

import dashboard_lib as lib  # noqa: E402

S1_BLOCKS = {"sentinel1", "sea_ice", "sea_ice_zoom", "lake_river_ice"}


def refresh_community(cdir, force=False):
    cfg = json.loads((cdir / "config.json").read_text(encoding="utf-8"))
    blocks = set(cfg.get("blocks", [])) & S1_BLOCKS
    if not blocks:
        return
    utm_zone = cfg.get("utm_zone")
    utm_epsg = cfg.get("utm_epsg")
    if not utm_zone and utm_epsg:
        try:
            utm_zone = int(utm_epsg) - 32600
        except (TypeError, ValueError):
            utm_zone = None
    lat, lon = cfg.get("lat"), cfg.get("lon")
    if not utm_zone or not utm_epsg or lat is None or lon is None:
        print(f"{cdir.name}: S1 blocks configured but utm_zone/utm_epsg/lat/lon missing — skipping")
        return
    site_label = cfg.get("site_display_name") or cfg.get("name") or cdir.name
    tz_name = cfg.get("tz_name", "UTC")
    now_utc = datetime.now(timezone.utc)

    token = lib.get_sentinel_hub_token()
    if not token:
        print(f"{cdir.name}: no Sentinel Hub credentials — skipping")
        return

    lookback = cfg.get("sentinel1_lookback_days", 10)
    s1_date, s1_full_dt, acq_mode, band, _pol = lib.find_latest_sentinel1_date(
        token, lat, lon, site_label, lookback_days=lookback, now_utc=now_utc)
    if not s1_date:
        print(f"{cdir.name}: no Sentinel-1 acquisition in the last {lookback} days")
        return

    charts = cdir / "charts"
    charts.mkdir(exist_ok=True)
    meta_path = charts / "satellite_meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    if not force and meta.get("s1_acquired") == s1_full_dt:
        print(f"{cdir.name}: acquisition {s1_full_dt} already rendered — skipping")
        return
    print(f"{cdir.name}: rendering acquisition {s1_full_dt} ({acq_mode}/{band})")

    cx, cy = lib.latlon_to_utm(lat, lon, zone=utm_zone)
    map_pts = list(cfg.get("map_points", [])) + lib.hydrometric_map_points(
        cfg.get("hydrometric_stations", []))
    ref_lines = cfg.get("map_reference_lines", [])
    _coast_rel = cfg.get("coastline_geojson_path")
    coastline = str(cdir / _coast_rel) if _coast_rel and (cdir / _coast_rel).exists() else None
    sea_seed_from = cfg.get("sea_seed_from", "top")
    sea_seed_points = cfg.get("sea_seed_points") or None

    def _save(name, img_bytes, caption):
        if not img_bytes:
            print(f"  {name}: no image produced")
            return False
        (charts / name).write_bytes(img_bytes)
        print(f"  {name}: {len(img_bytes) // 1024} kB")
        meta.setdefault(name.rsplit(".", 1)[0], {})["caption"] = caption or ""
        return True

    ok = False
    if "sentinel1" in blocks:
        b, cap = lib.fetch_and_process_sentinel1(
            lat=lat, lon=lon, site_label=site_label,
            utm_zone=utm_zone, utm_epsg=utm_epsg, center_x=cx, center_y=cy,
            points=map_pts, tz_name=tz_name, reference_lines=ref_lines,
            coastline_geojson_path=coastline, now_utc=now_utc,
            lookback_days=lookback)
        ok |= _save("sentinel1.png", b, cap)
    if "sea_ice" in blocks:
        b, cap = lib.fetch_and_process_sentinel1_ice(
            lat=lat, lon=lon, site_label=site_label,
            utm_zone=utm_zone, utm_epsg=utm_epsg, center_x=cx, center_y=cy,
            points=map_pts, tz_name=tz_name, half_width_m=150_000,
            reference_lines=ref_lines, coastline_geojson_path=coastline,
            now_utc=now_utc, lookback_days=lookback,
            sea_seed_from=sea_seed_from, sea_seed_points=sea_seed_points)
        ok |= _save("sea_ice.png", b, cap)
    if "sea_ice_zoom" in blocks:
        _ssdc_lat, _ssdc_lon = cfg.get("ssdc_lat"), cfg.get("ssdc_lon")
        _arrows = (
            [(_ssdc_lat, _ssdc_lon, cfg.get("ssdc_label", "SSDC"), 65, -65, 22)]
            if _ssdc_lat is not None and _ssdc_lon is not None else None
        )
        b, cap = lib.fetch_and_process_sentinel1_ice(
            lat=lat, lon=lon, site_label=site_label,
            utm_zone=utm_zone, utm_epsg=utm_epsg, center_x=cx, center_y=cy,
            points=map_pts, tz_name=tz_name, half_width_m=25_000,
            reference_lines=ref_lines, coastline_geojson_path=coastline,
            now_utc=now_utc, arrow_annotations=_arrows,
            lookback_days=lookback, sea_seed_from=sea_seed_from,
            sea_seed_points=sea_seed_points)
        ok |= _save("sea_ice_zoom.png", b, cap)
    if "lake_river_ice" in blocks:
        # Plain SAR frame at 50 km half-width. The lake/river-ice
        # classification overlay is deliberately not rendered — calm open
        # water reads as ice and the result was misleading.
        b, cap = lib.fetch_and_process_sentinel1(
            lat=lat, lon=lon, site_label=site_label,
            utm_zone=utm_zone, utm_epsg=utm_epsg, center_x=cx, center_y=cy,
            points=map_pts, tz_name=tz_name, half_width_m=50_000,
            reference_lines=ref_lines, coastline_geojson_path=coastline,
            now_utc=now_utc, lookback_days=lookback)
        ok |= _save("lake_ice.png", b, cap)

    if ok:
        meta["s1_acquired"] = s1_full_dt
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--community", action="append",
                    help="only refresh this community id (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="re-render even if the acquisition is unchanged")
    args = ap.parse_args()
    for cdir in sorted((ROOT / "communities").iterdir()):
        if not (cdir / "config.json").exists():
            continue
        if args.community and cdir.name not in args.community:
            continue
        if cdir.name.startswith("dummy"):
            continue
        try:
            refresh_community(cdir, force=args.force)
        except Exception as e:
            print(f"{cdir.name}: Sentinel-1 refresh failed: {e}")


if __name__ == "__main__":
    main()
