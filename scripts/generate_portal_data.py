"""
generate_portal_data.py — Alfred Portal live-data exporter (self-contained).

Writes two JSON files consumed by index.html:
  data/conditions_latest.json  — per-site scalar conditions
  data/wind_grid_latest.json   — 15x15 regional u/v grid for particle animation

Depends only on: requests (pip install requests)
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
# Sites
# ---------------------------------------------------------------------------

SITES = [
    {"id": "herschel",      "lat": 69.590,  "lon": -139.099, "has_surge": True},
    {"id": "shingle-point", "lat": 68.994,  "lon": -137.390, "has_surge": True},
    {"id": "tuktoyaktuk",   "lat": 69.454,  "lon": -133.037, "has_surge": True},
    {"id": "aklavik",       "lat": 68.224,  "lon": -135.013, "has_surge": False},
    {"id": "inuvik",        "lat": 68.361,  "lon": -133.723, "has_surge": False},
    {"id": "trail-valley",  "lat": 68.740,  "lon": -133.500, "has_surge": False},
    {"id": "police-cabin",  "lat": 68.749,  "lon": -136.485, "has_surge": False},
    {"id": "barrow",        "lat": 71.291,  "lon": -156.789, "has_surge": False},
    {"id": "prudhoe-bay",   "lat": 70.255,  "lon": -148.337, "has_surge": False},
]

# ---------------------------------------------------------------------------
# Wind grid
# ---------------------------------------------------------------------------

GRID_BOUNDS = {"south": 67.5, "north": 72.5, "west": -158.0, "east": -130.0}
GRID_NX, GRID_NY = 15, 15

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
# Weather — Open-Meteo ERA5 current analysis
# ---------------------------------------------------------------------------

def _fetch_weather(lat, lon):
    r = _get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current_weather": True,
            "hourly": "relativehumidity_2m,pressure_msl",
            "timezone": "UTC",
        },
    )
    cw = r.json()["current_weather"]
    return {
        "air_temp_c":   round(cw["temperature"],  1),
        "wind_kmh":     round(cw["windspeed"],     1),
        "wind_dir_deg": round(cw["winddirection"], 0),
        "surge_m":      None,
    }


# ---------------------------------------------------------------------------
# Surge — TOPAZ6 via THREDDS (coastal sites only)
# ---------------------------------------------------------------------------

def _fetch_surge(lat, lon):
    """
    Returns the nearest current total-water-level value from the TOPAZ6 model,
    or None on any failure. Uses only requests + basic math (no xarray/netCDF4).
    Accesses the OpenDAP ASCII endpoint so no binary libraries are needed.
    """
    try:
        # TOPAZ6 polar-stereographic grid parameters (spherical, pole-centred)
        # Confirmed against OpenDrift debug output for the same .ncml file.
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        lat_ts_r = math.radians(70.0)

        # Forward polar-stereographic projection (WGS84 sphere approximation)
        R = 6_371_000.0
        k0 = (1 + math.sin(lat_ts_r)) / 2  # scale at lat_ts
        rho = R * k0 * math.cos(lat_r) / (1 + math.sin(lat_r)) if lat > -89 else 0
        x_m = rho * math.sin(lon_r)
        y_m = -rho * math.cos(lon_r)

        # Grid is stored in units of 100 km
        UNIT = 100_000.0
        x_tgt = x_m / UNIT
        y_tgt = y_m / UNIT

        # Fetch a small slice of the coordinate axes to find the nearest index.
        # x runs roughly -30..+30, y roughly -35..+35.
        # Resolution ~3 km → 0.00003 units.  Request full 1-D axes.
        base = (
            "https://thredds.met.no/thredds/dodsC/cmems/topaz6/"
            "dataset-topaz6-arc-15min-3km-be.ncml"
        )
        # ASCII data for x axis
        rx = _get(f"{base}.ascii?x", timeout=30)
        x_vals = [float(v) for v in rx.text.split("\n")
                  if v.strip() and not v.startswith("Dataset") and not v.startswith("x")]
        # ASCII data for y axis
        ry = _get(f"{base}.ascii?y", timeout=30)
        y_vals = [float(v) for v in ry.text.split("\n")
                  if v.strip() and not v.startswith("Dataset") and not v.startswith("y")]

        if not x_vals or not y_vals:
            return None

        ix = min(range(len(x_vals)), key=lambda i: abs(x_vals[i] - x_tgt))
        iy = min(range(len(y_vals)), key=lambda i: abs(y_vals[i] - y_tgt))

        # Fetch a 5x5 neighbourhood around nearest point to handle masked cells
        r = 2
        ix0, ix1 = max(0, ix - r), min(len(x_vals) - 1, ix + r)
        iy0, iy1 = max(0, iy - r), min(len(y_vals) - 1, iy + r)

        # Latest time step: index 0 of the time dimension
        rz = _get(
            f"{base}.ascii?zos[0][{iy0}:{iy1}][{ix0}:{ix1}]",
            timeout=30,
        )
        # Parse the ASCII response — values are comma/space-separated floats
        values = []
        for line in rz.text.splitlines():
            for tok in line.replace(",", " ").split():
                try:
                    v = float(tok)
                    if not math.isnan(v) and abs(v) < 100:
                        values.append(v)
                except ValueError:
                    pass

        if values:
            return round(sum(values) / len(values), 2)
    except Exception as e:
        print(f"SURGE FETCH FAILED ({lat},{lon}): {e}")
    return None


# ---------------------------------------------------------------------------
# Wind grid
# ---------------------------------------------------------------------------

def _fetch_uv(lat, lon):
    try:
        d = _fetch_weather(lat, lon)
        spd = d["wind_kmh"] / 3.6
        dr = math.radians(d["wind_dir_deg"])
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
            if done % 30 == 0:
                print(f"WIND GRID: {done}/{total} points done")

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

    now_utc = datetime.now(timezone.utc)
    generated = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    conditions = {}

    def fetch_site(site):
        sid = site["id"]
        try:
            cond = _fetch_weather(site["lat"], site["lon"])
            print(f"WEATHER [{sid}]: {cond['air_temp_c']}°C {cond['wind_kmh']}km/h {cond['wind_dir_deg']}°")
        except Exception as e:
            print(f"WEATHER [{sid}] FAILED: {e}")
            cond = {"air_temp_c": None, "wind_kmh": None, "wind_dir_deg": None, "surge_m": None}
        return sid, cond

    def fetch_surge_for(site):
        sid = site["id"]
        surge = _fetch_surge(site["lat"], site["lon"])
        print(f"SURGE [{sid}]: {surge} m")
        return sid, surge

    coastal = [s for s in SITES if s["has_surge"]]

    with ThreadPoolExecutor(max_workers=len(SITES) + len(coastal)) as ex:
        w_futs = {ex.submit(fetch_site, s): s for s in SITES}
        s_futs = {ex.submit(fetch_surge_for, s): s for s in coastal}
        for fut in as_completed(list(w_futs) + list(s_futs)):
            if fut in w_futs:
                sid, cond = fut.result()
                conditions[sid] = cond
            else:
                sid, surge = fut.result()
                conditions.setdefault(sid, {})["surge_m"] = surge

    for site in SITES:
        conditions.setdefault(site["id"], {}).setdefault("surge_m", None)

    cond_path = os.path.join(args.out_dir, "conditions_latest.json")
    with open(cond_path, "w") as f:
        json.dump({"generated_utc": generated, "sites": conditions}, f, separators=(",", ":"))
    print(f"WROTE: {cond_path}")

    grid = build_wind_grid(now_utc)
    grid_path = os.path.join(args.out_dir, "wind_grid_latest.json")
    with open(grid_path, "w") as f:
        json.dump(grid, f, separators=(",", ":"))
    print(f"WROTE: {grid_path}")

    print(f"DONE — generated at {generated}")


if __name__ == "__main__":
    main()
