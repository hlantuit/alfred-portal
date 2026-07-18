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

GRID_BOUNDS = {"south": 62.0, "north": 78.0, "west": -175.0, "east": -110.0}
GRID_NX, GRID_NY = 20, 20

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
            "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,weather_code",
            "models": "ecmwf_ifs025",
            "timezone": "UTC",
            "start_date": now_iso,
            "end_date": now_iso,
        },
    )
    data = r.json()
    temps  = data["hourly"]["temperature_2m"]
    speeds = data["hourly"]["wind_speed_10m"]
    dirs   = data["hourly"]["wind_direction_10m"]
    codes  = data["hourly"]["weather_code"]

    now_h = datetime.now(timezone.utc).hour
    idx = min(range(len(temps)), key=lambda i: abs(i - now_h))

    return {
        "air_temp_c":   round(temps[idx],  1) if temps[idx]  is not None else None,
        "wind_kmh":     round(speeds[idx], 1) if speeds[idx] is not None else None,
        "wind_dir_deg": dirs[idx],
        "weather_code": codes[idx],
        "surge_m":      None,
    }


# ---------------------------------------------------------------------------
# Wind grid
# ---------------------------------------------------------------------------

def _fetch_uv(lat, lon):
    try:
        d = _fetch_weather(lat, lon)
        spd = (d["wind_kmh"] or 0) / 3.6
        dr = math.radians(d["wind_dir_deg"] or 0)
        return -spd * math.sin(dr), -spd * math.cos(dr)
    except Exception as e:
        print(f"WIND GRID FETCH FAILED ({lat:.2f},{lon:.2f}): {e}")
        return 0.0, 0.0


def build_wind_grid(now_utc):
    s, n = GRID_BOUNDS["south"], GRID_BOUNDS["north"]
    w, e = GRID_BOUNDS["west"],  GRID_BOUNDS["east"]
    lats = [s + (n - s) * j / (GRID_NY - 1) for j in range(GRID_NY)]
    lons = [w + (e - w) * i / (GRID_NX - 1) for i in range(GRID_NX)]

    u_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]
    v_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]

    total = GRID_NY * GRID_NX
    print(f"WIND GRID: fetching {total} points ({GRID_NY}x{GRID_NX})")

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(_fetch_uv, lats[j], lons[i]): (j, i)
                for j in range(GRID_NY) for i in range(GRID_NX)}
        done = 0
        for fut in as_completed(futs):
            j, i = futs[fut]
            u, v = fut.result()
            u_grid[j][i] = round(u, 2)
            v_grid[j][i] = round(v, 2)
            done += 1
            if done % 50 == 0:
                print(f"WIND GRID: {done}/{total} done")

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
            "dashboard": f"https://www.notion.so/{c['notion_page_id']}",
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
