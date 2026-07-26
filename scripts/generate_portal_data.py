"""
generate_portal_data.py — reads communities/ configs, writes map JSON files.

Outputs (consumed by index.html):
  data/sites.json             — site list auto-built from communities/
  data/conditions_latest.json — per-site conditions
  data/wind_grid_latest.json  — regional wind grid for particle animation
"""

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: requests not installed — run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Load community configs
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMUNITIES_DIR = os.path.join(REPO_ROOT, "communities")


def load_communities():
    communities = []
    for name in sorted(os.listdir(COMMUNITIES_DIR)):
        cfg_path = os.path.join(COMMUNITIES_DIR, name, "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                communities.append(json.load(f))
    return communities


# ---------------------------------------------------------------------------
# Wind grid
# ---------------------------------------------------------------------------

# Bounds match the particle animation display extent in index.html.
# Resolution is 1° lat × 1° lon — coarser than the GEM model (~15 km) but
# fine enough to resolve synoptic-scale wind patterns without hundreds of
# individual API calls.  Batch fetching keeps total request count low (~35).
GRID_BOUNDS = {"south": 35.0, "north": 76.0, "west": -175.0, "east": -94.0}
GRID_LAT_STEP = 1.0   # degrees
GRID_LON_STEP = 1.0   # degrees
GRID_NY = round((GRID_BOUNDS["north"] - GRID_BOUNDS["south"]) / GRID_LAT_STEP) + 1
GRID_NX = round((GRID_BOUNDS["east"]  - GRID_BOUNDS["west"])  / GRID_LON_STEP) + 1
GRID_BATCH = 100       # Open-Meteo supports up to ~300 locations per request

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "alfred-portal-data/1.0"})


def _get(url, params=None, timeout=20, retries=2):
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(3 * (attempt + 1))


# ---------------------------------------------------------------------------
# Weather — Open-Meteo ECMWF IFS 0.25°
# ---------------------------------------------------------------------------

def _fetch_weather(lat, lon):
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = _get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            # wind_speed_10m (km/h) and wind_direction_10m (degrees) are the
            # standard verified surface variables; u/v components can return
            # pressure-level values on some models despite the "10m" name.
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,weather_code",
            "models": "gem_seamless",
            "timezone": "UTC",
            "start_date": now_iso,
            "end_date": now_iso,
        },
    )
    data  = r.json()
    temps  = data["hourly"]["temperature_2m"]
    speeds = data["hourly"]["wind_speed_10m"]    # km/h
    dirs   = data["hourly"]["wind_direction_10m"] # degrees FROM (meteorological)
    codes  = data["hourly"]["weather_code"]

    now_h = datetime.now(timezone.utc).hour
    idx   = min(range(len(temps)), key=lambda i: abs(i - now_h))

    spd_kmh  = speeds[idx] or 0.0
    wind_dir = dirs[idx]
    spd_ms   = spd_kmh / 3.6
    # Derive u/v (eastward/northward) from speed + direction for wind grid
    dir_rad = math.radians(wind_dir) if wind_dir is not None else 0.0
    u_ms = -spd_ms * math.sin(dir_rad)
    v_ms = -spd_ms * math.cos(dir_rad)

    return {
        "air_temp_c":   round(temps[idx], 1) if temps[idx] is not None else None,
        "wind_kmh":     round(spd_kmh, 1),
        "wind_dir_deg": round(wind_dir, 1) if wind_dir is not None else None,
        "wind_u_ms":    round(u_ms, 3),
        "wind_v_ms":    round(v_ms, 3),
        "weather_code": codes[idx],
        "surge_m":      None,
    }


# ---------------------------------------------------------------------------
# Wind grid
# ---------------------------------------------------------------------------

def _fetch_uv_batch(batch_points, now_h, now_iso):
    """
    Fetch u/v wind components for a list of (lat, lon) points in one
    Open-Meteo request.  Returns a list of (u_ms, v_ms) in the same order.
    """
    lats_str = ",".join(f"{lat:.2f}" for lat, _ in batch_points)
    lons_str = ",".join(f"{lon:.2f}" for _, lon in batch_points)
    r = _get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":  lats_str,
            "longitude": lons_str,
            "hourly":    "wind_speed_10m,wind_direction_10m",
            "models":    "gem_seamless",
            "timezone":  "UTC",
            "start_date": now_iso,
            "end_date":   now_iso,
        },
        timeout=30,
    )
    results = r.json()
    # Single location returns a dict; multiple return a list.
    if isinstance(results, dict):
        results = [results]
    uvs = []
    for d in results:
        speeds = d["hourly"]["wind_speed_10m"]
        dirs   = d["hourly"]["wind_direction_10m"]
        idx    = min(range(len(speeds)), key=lambda i: abs(i - now_h))
        spd_ms  = (speeds[idx] or 0.0) / 3.6
        wind_dir = dirs[idx] or 0.0
        dir_rad  = math.radians(wind_dir)
        uvs.append((-spd_ms * math.sin(dir_rad), -spd_ms * math.cos(dir_rad)))
    return uvs


def build_wind_grid(now_utc):
    s, n = GRID_BOUNDS["south"], GRID_BOUNDS["north"]
    w, e = GRID_BOUNDS["west"],  GRID_BOUNDS["east"]
    lats = [round(s + j * GRID_LAT_STEP, 4) for j in range(GRID_NY)]
    lons = [round(w + i * GRID_LON_STEP, 4) for i in range(GRID_NX)]

    total = GRID_NY * GRID_NX
    print(f"WIND GRID: fetching {total} points ({GRID_NY}×{GRID_NX}) in batches of {GRID_BATCH}")

    # Flat list of (flat_index, lat, lon)
    all_points = [(j * GRID_NX + i, lats[j], lons[i])
                  for j in range(GRID_NY) for i in range(GRID_NX)]
    batches = [all_points[k:k + GRID_BATCH] for k in range(0, total, GRID_BATCH)]

    now_h    = now_utc.hour
    now_iso  = now_utc.strftime("%Y-%m-%d")
    u_flat   = [0.0] * total
    v_flat   = [0.0] * total

    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = {
            ex.submit(_fetch_uv_batch, [(lat, lon) for _, lat, lon in batch], now_h, now_iso): batch
            for batch in batches
        }
        done_pts = 0
        for fut in as_completed(futs):
            batch = futs[fut]
            try:
                uvs = fut.result()
            except Exception as e:
                print(f"WIND GRID BATCH FAILED: {e}")
                uvs = [(0.0, 0.0)] * len(batch)
            for (flat_idx, _, _), (u, v) in zip(batch, uvs):
                u_flat[flat_idx] = round(u, 3)
                v_flat[flat_idx] = round(v, 3)
            done_pts += len(batch)
            print(f"WIND GRID: {done_pts}/{total} done")

    u_grid = [[u_flat[j * GRID_NX + i] for i in range(GRID_NX)] for j in range(GRID_NY)]
    v_grid = [[v_flat[j * GRID_NX + i] for i in range(GRID_NX)] for j in range(GRID_NY)]

    print("WIND GRID: complete")
    return {
        "generated_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bounds": GRID_BOUNDS,
        "nx": GRID_NX, "ny": GRID_NY,
        "u": u_grid, "v": v_grid,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", default="data")
    args = p.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    communities = load_communities()
    print(f"Loaded {len(communities)} communities")

    now_utc   = datetime.now(timezone.utc)
    generated = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---- Write sites.json (auto-built from configs) ----
    sites = [
        {
            "id":        c["id"],
            "name":      c["name"],
            "lat":       c["lat"],
            "lon":       c["lon"],
            "type":      c["type"],
            "dashboard": c.get("public_notion_url", f"https://www.notion.so/{c['notion_page_id']}"),
        }
        for c in communities
    ]
    sites_path = os.path.join(args.out_dir, "sites.json")
    with open(sites_path, "w") as f:
        json.dump(sites, f, indent=2)
    print(f"WROTE: {sites_path}")

    # ---- Fetch conditions in parallel ----
    conditions = {}

    def fetch_site(c):
        sid = c["id"]
        try:
            cond = _fetch_weather(c["lat"], c["lon"])
            print(f"WEATHER [{sid}]: {cond['air_temp_c']}°C "
                  f"{cond['wind_kmh']}km/h {cond['wind_dir_deg']}° code={cond['weather_code']}")
            return sid, cond
        except Exception as e:
            print(f"WEATHER [{sid}] FAILED: {e}")
            return sid, {"air_temp_c": None, "wind_kmh": None,
                         "wind_dir_deg": None, "weather_code": None, "surge_m": None}

    with ThreadPoolExecutor(max_workers=len(communities)) as ex:
        for sid, cond in ex.map(fetch_site, communities):
            conditions[sid] = cond

    cond_path = os.path.join(args.out_dir, "conditions_latest.json")
    with open(cond_path, "w") as f:
        json.dump({"generated_utc": generated, "sites": conditions},
                  f, separators=(",", ":"))
    print(f"WROTE: {cond_path}")

    # ---- Wind grid ----
    grid = build_wind_grid(now_utc)
    grid_path = os.path.join(args.out_dir, "wind_grid_latest.json")
    with open(grid_path, "w") as f:
        json.dump(grid, f, separators=(",", ":"))
    print(f"WROTE: {grid_path}")

    print(f"DONE — {generated}")


if __name__ == "__main__":
    main()
