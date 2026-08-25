#!/usr/bin/env python3
"""
Generate GitHub Pages output for a community dashboard.

Writes docs/<community-id>/data.json  (fetched by index.html on every page load)
Copies chart PNGs from communities/<community-id>/charts/ → docs/<community-id>/img/

The existing Notion pipeline is not touched.
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

import csv as _csv_mod
import datetime as _dt_mod
from collections import defaultdict as _defaultdict


def _is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _date_to_clim_doy(date):
    """Day-of-year 1-365, mapping Feb 29 → 59 and shifting Mar 1+ back by 1 in leap years."""
    doy = date.timetuple().tm_yday
    if _is_leap_year(date.year) and doy >= 60:
        doy -= 1
    return min(doy, 365)


def fetch_hydrometric_climatology(station_id, clim_years=30, provterr=None):
    """Standalone version — only needs requests (already imported above)."""
    try:
        now = _dt_mod.datetime.utcnow()
        current_year = now.year
        clim_start_year = max(1970, current_year - clim_years)
        clim_end_year = current_year - 1

        def _get_json(url, **kwargs):
            r = requests.get(url, **kwargs)
            r.raise_for_status()
            return r.json()

        def _fetch_range(date_from, date_to):
            base = "https://api.weather.gc.ca/collections/hydrometric-daily-mean/items"
            records = []
            offset = 0
            limit = 10000
            while True:
                data = _get_json(base, params={
                    "STATION_NUMBER": station_id,
                    "datetime": f"{date_from}/{date_to}",
                    "limit": limit, "offset": offset, "f": "json",
                }, timeout=40)
                features = data.get("features", [])
                records.extend(features)
                if len(features) < limit:
                    break
                offset += limit
            return records

        print(f"  HYDROMETRIC CLIM [{station_id}]: fetching {clim_start_year}–{clim_end_year}")
        clim_features = _fetch_range(f"{clim_start_year}-01-01", f"{clim_end_year}-12-31")
        print(f"  HYDROMETRIC CLIM [{station_id}]: OGC returned {len(clim_features)} historical features")

        # Datamart CSV fallback for historical data when OGC returns nothing
        _csv_all_daily = {}
        if not clim_features and provterr:
            pv = provterr.upper()
            for _csv_url_hist in [
                f"https://dd.weather.gc.ca/today/hydrometric/csv/{pv}/daily/{pv}_{station_id}_daily_hydrometric.csv",
                f"https://dd.weather.gc.ca/hydrometric/csv/{pv}/daily/{pv}_{station_id}_daily_hydrometric.csv",
            ]:
                try:
                    resp = requests.get(_csv_url_hist, timeout=30)
                    resp.raise_for_status()
                    for row in _csv_mod.reader(resp.text.splitlines()[1:]):
                        if len(row) < 3:
                            continue
                        dstr = row[1].strip()
                        lstr = row[2].strip() or (len(row) >= 4 and row[3].strip())
                        if not dstr or not lstr:
                            continue
                        try:
                            d = _dt_mod.date.fromisoformat(dstr[:10])
                            _csv_all_daily[d] = float(lstr)
                        except Exception:
                            continue
                    print(f"  HYDROMETRIC CLIM [{station_id}]: Datamart CSV gave {len(_csv_all_daily)} rows")
                    if _csv_all_daily:
                        clim_features = [
                            {"properties": {"DATE": str(d), "LEVEL": v}}
                            for d, v in sorted(_csv_all_daily.items())
                            if clim_start_year <= d.year <= clim_end_year
                        ]
                        print(f"  HYDROMETRIC CLIM [{station_id}]: {len(clim_features)} historical from Datamart")
                        break
                except Exception as e:
                    print(f"  HYDROMETRIC CLIM [{station_id}]: Datamart hist failed: {e}")

        # Current-year data: pre-fill from Datamart CSV if already loaded
        cur_daily = {d: v for d, v in _csv_all_daily.items() if d.year == current_year}

        # Try WaterOffice graph JSON API for more complete current-year coverage
        try:
            wo_session = requests.Session()
            wo_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; alfred-portal/1.0)"})
            graph_url = "https://wateroffice.ec.gc.ca/services/real_time_graph/json/inline"
            graph_params = {"station": station_id,
                            "start_date": f"{current_year}-01-01",
                            "end_date": str(_dt_mod.date.today()),
                            "param1": 46, "param2": 3}

            def _try_graph(sess):
                r = sess.get(graph_url, params=graph_params, timeout=60)
                r.raise_for_status()
                if "text/html" in r.headers.get("Content-Type", ""):
                    raise ValueError("HTML disclaimer page")
                return r.json()

            try:
                wo_data = _try_graph(wo_session)
            except Exception:
                rpt = wo_session.get(
                    "https://wateroffice.ec.gc.ca/report/real_time_e.html",
                    params={"stn": station_id, "startDate": f"{current_year}-01-01",
                            "endDate": str(_dt_mod.date.today()), "prm1": 46, "prm2": 3, "mode": "Graph"},
                    timeout=60)
                wo_session.post("https://wateroffice.ec.gc.ca/disclaimer_e.html",
                                data={"agree": "1"}, headers={"Referer": rpt.url},
                                timeout=30, allow_redirects=True)
                wo_data = _try_graph(wo_session)

            daily_sums = _defaultdict(list)
            for pk in ("46", "3"):
                series = wo_data.get(pk, {})
                for pt in series.get("approved", []) + series.get("provisional", []):
                    if len(pt) < 2 or pt[1] is None:
                        continue
                    try:
                        d = _dt_mod.date.fromtimestamp(pt[0] / 1000)
                        if d.year == current_year:
                            daily_sums[d].append(float(pt[1]))
                    except Exception:
                        continue
                if daily_sums:
                    break
            wo_cur = {d: sum(vs) / len(vs) for d, vs in daily_sums.items()}
            cur_daily.update(wo_cur)
            print(f"  HYDROMETRIC CLIM [{station_id}]: WO graph API gave {len(wo_cur)} current-year days")
        except Exception as e:
            print(f"  HYDROMETRIC CLIM [{station_id}]: WO graph API failed: {e}")

        cur_features = [
            {"properties": {"DATE": str(d), "LEVEL": v}}
            for d, v in sorted(cur_daily.items())
        ]
        print(f"  HYDROMETRIC CLIM [{station_id}]: {len(clim_features)} hist + {len(cur_features)} current-year records")

        def _parse(features):
            out = []
            for f in features:
                p = f.get("properties", {})
                date_str = p.get("DATE") or p.get("DATETIME", "")
                try:
                    d = _dt_mod.date.fromisoformat(date_str[:10])
                except Exception:
                    continue
                level = p.get("LEVEL")
                discharge = p.get("DISCHARGE")
                out.append((d, level, discharge))
            return out

        clim_records = _parse(clim_features)
        cur_records = _parse(cur_features)

        use_level = any(r[1] is not None for r in clim_records + cur_records)
        unit = "level" if use_level else "discharge"

        def _val(r):
            return r[1] if use_level else r[2]

        doy_values = _defaultdict(list)
        for r in clim_records:
            v = _val(r)
            if v is not None:
                doy_values[_date_to_clim_doy(r[0])].append(float(v))

        current_year_list = sorted(
            (_date_to_clim_doy(r[0]), float(_val(r)))
            for r in cur_records if _val(r) is not None
        )

        if not doy_values and not current_year_list:
            print(f"  HYDROMETRIC CLIM [{station_id}]: no data returned")
            return None, None, unit

        print(f"  HYDROMETRIC CLIM [{station_id}]: {len({r[0].year for r in clim_records})} clim years, "
              f"{len(current_year_list)} current-year points, unit={unit}")
        return dict(doy_values), current_year_list, unit

    except Exception as e:
        print(f"  HYDROMETRIC CLIM [{station_id}] FAILED: {e}")
        return None, None, "level"

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMUNITIES_DIR = REPO_ROOT / "communities"
DOCS_DIR = REPO_ROOT / "docs"

WMO_LABELS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ heavy hail",
}

_WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"]


def wind_dir_label(deg):
    return _WIND_DIRS[round(float(deg) / 22.5) % 16]


def get_with_retry(url, params=None, timeout=30, retries=3, headers=None):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=headers)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt+1}/{retries-1} for {url}: {e}")
    raise RuntimeError("unreachable")


# ── Weather ───────────────────────────────────────────────────────────────

def fetch_weather(lat, lon, tz):
    r = get_with_retry(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": ",".join([
                "temperature_2m", "apparent_temperature", "relativehumidity_2m",
                "windspeed_10m", "winddirection_10m", "weathercode",
                "surface_pressure", "cloudcover",
            ]),
            "hourly": "cloudcover,windspeed_10m,winddirection_10m,pressure_msl,temperature_2m,precipitation,rain,snowfall,snow_depth",
            "daily": ",".join([
                "temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max", "precipitation_sum",
                "windspeed_10m_max", "windgusts_10m_max",
                "winddirection_10m_dominant",
                "weathercode", "uv_index_max",
                "sunrise", "sunset",
            ]),
            "timezone": tz,
            "forecast_days": 10,
            "models": "gem_seamless",
        },
    )
    d = r.json()
    cur = d.get("current", {})
    daily = d.get("daily", {})
    hourly = d.get("hourly", {})

    # UV index: gem_seamless doesn't provide it — fetch from best_match model
    uv_daily = []
    try:
        r_uv = get_with_retry(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "daily": "uv_index_max",
                "timezone": tz,
                "forecast_days": 10,
            },
            timeout=20,
        )
        uv_daily = r_uv.json().get("daily", {}).get("uv_index_max", [])
        print(f"  UV index (best_match): {uv_daily[:3]}")
    except Exception as e:
        print(f"  UV index fetch failed: {e}")

    # Daily forecast
    def dget(key, i, default=None):
        arr = daily.get(key, [])
        return arr[i] if i < len(arr) else default

    forecast = []
    for i, date in enumerate(daily.get("time", [])):
        dt = datetime.strptime(date, "%Y-%m-%d")
        if i == 0:
            day_label = "Today"
        elif i == 1:
            day_label = "Tomorrow"
        else:
            day_label = dt.strftime("%a %-d %b")
        wcode = dget("weathercode", i, 0)
        wind_deg = dget("winddirection_10m_dominant", i)
        forecast.append({
            "date": date,
            "day": day_label,
            "weathercode": wcode,
            "condition": WMO_LABELS.get(wcode, ""),
            "hi": _r1(dget("temperature_2m_max", i)),
            "lo": _r1(dget("temperature_2m_min", i)),
            "precip_prob": dget("precipitation_probability_max", i, 0) or 0,
            "precip_mm": _r1(dget("precipitation_sum", i)),
            "wind_max": _ri(dget("windspeed_10m_max", i)),
            "gusts_max": _ri(dget("windgusts_10m_max", i)),
            "wind_dir_deg": _ri(wind_deg),
            "wind_dir_label": wind_dir_label(wind_deg) if wind_deg is not None else None,
            "uv_max": _r1(uv_daily[i] if i < len(uv_daily) else dget("uv_index_max", i)),
            "sunrise": dget("sunrise", i),
            "sunset": dget("sunset", i),
        })

    # 10-day daily arrays
    daily_hi    = [fc["hi"]         for fc in forecast]
    daily_lo    = [fc["lo"]         for fc in forecast]
    daily_wind  = [fc["wind_max"]   for fc in forecast]
    daily_precip_prob = [fc["precip_prob"] for fc in forecast]
    daily_precip_mm   = [fc["precip_mm"]   for fc in forecast]
    daily_dates = [fc["date"]       for fc in forecast]

    # Hourly arrays — 48h compact and 10-day downsampled (every 2h = 120 pts)
    times_h = hourly.get("time", [])
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    start_i = next((i for i, t in enumerate(times_h) if t[:13] >= now_str), 0)

    def h_slice(key, n=48):
        vals = hourly.get(key, [])
        return [round(v, 1) if v is not None else None for v in vals[start_i:start_i+n]]

    def h_10d(key, decimals=1):
        """10-day hourly, every 2h starting from now (≈120 pts)."""
        vals = hourly.get(key, [])
        return [round(v, decimals) if v is not None else None
                for v in vals[start_i::2][:120]]

    hourly_times_10d = times_h[start_i::2][:120]  # ISO strings, every 2h

    # Daily means from full 10-day hourly (240h)
    def daily_from_hourly(key, n_days=10):
        vals = hourly.get(key, [])
        out = []
        for d in range(n_days):
            day = [v for v in vals[d*24:(d+1)*24] if v is not None]
            out.append(round(sum(day)/len(day), 1) if day else None)
        return out

    return {
        "temperature": _r1(cur.get("temperature_2m")),
        "feels_like": _r1(cur.get("apparent_temperature")),
        "humidity": _ri(cur.get("relativehumidity_2m")),
        "wind_speed": _ri(cur.get("windspeed_10m")),
        "wind_direction": _ri(cur.get("winddirection_10m")),
        "wind_dir_label": wind_dir_label(cur.get("winddirection_10m") or 0),
        "weathercode": cur.get("weathercode", 0),
        "condition": WMO_LABELS.get(cur.get("weathercode", 0), ""),
        "pressure": _ri(cur.get("surface_pressure")),
        "cloudcover": _ri(cur.get("cloudcover")),
        "forecast": forecast,
        "daily_hi": daily_hi,
        "daily_lo": daily_lo,
        "daily_wind": daily_wind,
        "daily_precip_prob": daily_precip_prob,
        "daily_precip_mm": daily_precip_mm,
        "daily_dates": daily_dates,
        "hourly_pressure": h_slice("pressure_msl"),
        "hourly_cloud": h_slice("cloudcover"),
        "daily_pressure": daily_from_hourly("pressure_msl"),
        "daily_cloud": daily_from_hourly("cloudcover"),
        # 10-day hourly series for interactive charts
        "hourly_times_10d": hourly_times_10d,
        "hourly_temperature_10d": h_10d("temperature_2m"),
        "hourly_wind_10d": h_10d("windspeed_10m"),
        "hourly_winddir_10d": h_10d("winddirection_10m"),
        "hourly_pressure_10d": h_10d("pressure_msl"),
        "hourly_cloud_10d": h_10d("cloudcover"),
        "hourly_precip_10d": h_10d("precipitation", decimals=2),
        "hourly_rain_10d": h_10d("rain", decimals=2),
        "hourly_snow_10d": [round(v * 10, 2) if v is not None else None for v in h_10d("snowfall", decimals=3)],
        "snow_depth_cm": h_10d("snow_depth", decimals=1),
    }


def _r1(v):
    return round(float(v), 1) if v is not None else None

def _ri(v):
    return round(float(v)) if v is not None else None


# ── Tides (IWLS — 7-day prediction) ──────────────────────────────────────

def _find_iwls_station_id(code):
    try:
        r = get_with_retry("https://api-iwls.dfo-mpo.gc.ca/api/v1/stations", timeout=30)
        for s in r.json():
            if s.get("code") == code:
                return s.get("id")
    except Exception as e:
        print(f"  IWLS station lookup failed: {e}")
    return None


def fetch_tide(station_code, station_name, now_utc):
    station_id = _find_iwls_station_id(station_code)
    if not station_id:
        print(f"  IWLS: station {station_code} not found")
        return None

    from_dt = now_utc - timedelta(hours=6)
    to_dt = now_utc + timedelta(days=7)

    # Try wlo (observed), wlp (predicted), wlf (forecast) — vary time window per code
    to_dt_48h = now_utc + timedelta(hours=48)
    points = None
    attempts = [
        ("wlo", from_dt, to_dt),       # observed: past 6h + 7d forward
        ("wlp", now_utc, to_dt),       # predicted: 7d forward
        ("wlp", now_utc, to_dt_48h),   # predicted: 48h (shorter window)
        ("wlf", now_utc, to_dt),       # forecast: 7d forward
        ("wlf", now_utc, to_dt_48h),   # forecast: 48h
        ("wlp", from_dt, to_dt_48h),   # predicted: past+48h
    ]
    for ts_code, fr, to in attempts:
        try:
            r = requests.get(
                f"https://api-iwls.dfo-mpo.gc.ca/api/v1/stations/{station_id}/data",
                params={
                    "time-series-code": ts_code,
                    "from": fr.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": to.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                timeout=30,
            )
            if r.status_code == 200:
                pts = r.json()
                if pts:
                    print(f"  IWLS: using time-series-code={ts_code} ({len(pts)} pts)")
                    points = pts
                    break
                else:
                    print(f"  IWLS {ts_code} returned 0 points")
            else:
                print(f"  IWLS {ts_code} HTTP {r.status_code}")
        except Exception as e:
            print(f"  IWLS {ts_code} attempt failed: {e}")
    if not points:
        print(f"  IWLS data fetch failed: no data for station {station_id}")
        return None

    if not points:
        print("  IWLS: empty response")
        return None

    now_ts = now_utc.timestamp()
    best = min(points, key=lambda p: abs(
        datetime.fromisoformat(p["eventDate"].replace("Z", "+00:00")).timestamp() - now_ts
    ))
    current_m = round(best.get("value", 0), 3)

    # Trend from previous few points
    idx_now = points.index(best)
    trend = "steady"
    if idx_now >= 6:
        prev = points[idx_now - 6]
        delta = best.get("value", 0) - prev.get("value", 0)
        if delta > 0.03:
            trend = "rising"
        elif delta < -0.03:
            trend = "falling"

    # Next high and low from future points
    def parse_t(p):
        return datetime.fromisoformat(p["eventDate"].replace("Z", "+00:00"))

    future = [p for p in points if parse_t(p).timestamp() > now_ts]
    highs, lows = [], []
    for i in range(1, len(future) - 1):
        v = future[i].get("value", 0)
        if v > future[i-1].get("value", 0) and v > future[i+1].get("value", 0):
            highs.append(future[i])
        elif v < future[i-1].get("value", 0) and v < future[i+1].get("value", 0):
            lows.append(future[i])

    def fmt_extreme(lst):
        if not lst:
            return None
        return {"time_utc": lst[0]["eventDate"], "m": round(lst[0].get("value", 0), 2)}

    # Thin series to ~200 points for the chart
    step = max(1, len(points) // 200)
    series = [{"t": p["eventDate"], "m": round(p.get("value", 0), 3)}
              for p in points[::step]]

    return {
        "current_m": current_m,
        "trend": trend,
        "next_high": fmt_extreme(highs),
        "next_low": fmt_extreme(lows),
        "series": series,
    }


# ── Total water level: TOPAZ6 (Copernicus) + GDSPS (MSC) ────────────────

def _latlon_to_topaz6(lat_deg, lon_deg):
    import math as _math
    R = 6378273.0
    lon0 = _math.radians(-45)
    lat = _math.radians(lat_deg)
    lon = _math.radians(lon_deg)
    rho = 2 * R * _math.tan(_math.pi / 4 - lat / 2)
    x = rho * _math.sin(lon - lon0)
    y = -rho * _math.cos(lon - lon0)
    return x, y


def fetch_topaz_water_level(lat, lon, now_utc, site_label, yearly_mean=None):
    """TOPAZ6 sea surface height via Copernicus THREDDS (tide + surge, 10-day forecast).
    Returns (times_iso, values_m_anomaly, yearly_mean) or (None, None, None)."""
    try:
        import math as _math
        import xarray as xr

        url = "https://thredds.met.no/thredds/dodsC/cmems/topaz6/dataset-topaz6-arc-15min-3km-be.ncml"
        target_x_m, target_y_m = _latlon_to_topaz6(lat, lon)
        UNIT_SCALE = 100_000
        target_x = target_x_m / UNIT_SCALE
        target_y = target_y_m / UNIT_SCALE

        ds = xr.open_dataset(url)
        search_r = 50_000 / UNIT_SCALE
        x_coords = ds["x"].values
        y_coords = ds["y"].values
        x_asc = x_coords[0] < x_coords[-1] if len(x_coords) > 1 else True
        y_asc = y_coords[0] < y_coords[-1] if len(y_coords) > 1 else True
        x_sl = slice(target_x - search_r, target_x + search_r) if x_asc else slice(target_x + search_r, target_x - search_r)
        y_sl = slice(target_y - search_r, target_y + search_r) if y_asc else slice(target_y + search_r, target_y - search_r)

        start = now_utc.replace(tzinfo=None)
        end = (now_utc + timedelta(days=10)).replace(tzinfo=None)
        time_coords = ds["zos"]["time"].values
        t_asc = time_coords[0] < time_coords[-1] if len(time_coords) > 1 else True
        t_sl = slice(start, end) if t_asc else slice(end, start)

        nearby = ds["zos"].sel(x=x_sl, y=y_sl, time=t_sl)
        if nearby.size == 0:
            print(f"  TOPAZ6: no cells near {site_label}")
            return None, None, None

        has_valid = nearby.notnull().any(dim="time").values
        xs = nearby["x"].values
        ys = nearby["y"].values
        best_pt, best_d = None, None
        for yi, yv in enumerate(ys):
            for xi, xv in enumerate(xs):
                if has_valid[yi, xi]:
                    d = _math.hypot(xv - target_x, yv - target_y)
                    if best_d is None or d < best_d:
                        best_d = d; best_pt = (xv, yv)
        if not best_pt:
            print(f"  TOPAZ6: no valid cells near {site_label}")
            return None, None, None

        point = nearby.sel(x=best_pt[0], y=best_pt[1])
        times = [str(t) for t in point["time"].values]
        raw = [float(v) for v in point.values.flatten()]
        times_c, vals_c = zip(*[(t, v) for t, v in zip(times, raw) if not _math.isnan(v)]) if raw else ([], [])
        times_c, vals_c = list(times_c), list(vals_c)

        if not vals_c:
            return None, None, None

        if yearly_mean is None:
            hist_start = (now_utc - timedelta(days=30)).replace(tzinfo=None)
            hist_sl = slice(hist_start, start) if t_asc else slice(start, hist_start)
            hist = ds["zos"].sel(x=best_pt[0], y=best_pt[1], time=hist_sl)
            hist_v = [float(v) for v in hist.values.flatten() if not _math.isnan(float(v))]
            yearly_mean = sum(hist_v) / len(hist_v) if hist_v else sum(vals_c) / len(vals_c)

        anomaly = [round(v - yearly_mean, 4) for v in vals_c]
        print(f"  TOPAZ6: {len(times_c)} steps for {site_label} (mean={yearly_mean:.3f}m)")
        step = max(1, len(times_c) // 300)
        return times_c[::step], anomaly[::step], yearly_mean

    except Exception as e:
        print(f"  TOPAZ6 fetch failed for {site_label}: {e}")
        return None, None, None


def fetch_gdsps_water_level(lat, lon, now_utc, site_label, yearly_mean=None):
    """GDSPS SSH (tide + surge) 10-day forecast via MSC GeoMet WMS GetFeatureInfo.
    Returns (times_iso, values_m_anomaly, yearly_mean) or (None, None, None)."""
    try:
        import re as _re
        import concurrent.futures as _cf

        caps = get_with_retry(
            "https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0"
            "&request=GetCapabilities&LAYERS=GDSPS_15km_SeaSfcHeight",
            timeout=30, retries=2,
        )
        caps.raise_for_status()
        m = _re.search(r'<Dimension[^>]*name=["\']time["\'][^>]*>([^<]+)</Dimension>', caps.text)
        if not m:
            raise ValueError("No time dimension in GDSPS capabilities")
        parts = m.group(1).strip().split("/")
        t_start = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).replace(tzinfo=None)
        t_end   = datetime.fromisoformat(parts[1].replace("Z", "+00:00")).replace(tzinfo=None)
        now_n = now_utc.replace(tzinfo=None)

        timestamps = []
        t = t_start
        while t <= t_end:
            if t >= now_n:
                timestamps.append(t)
            t += timedelta(hours=1)

        bbox = f"{lon - 0.5},{lat - 0.5},{lon + 0.5},{lat + 0.5}"

        def _one(ts):
            try:
                r = requests.get(
                    "https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0"
                    "&request=GetFeatureInfo&layers=GDSPS_15km_SeaSfcHeight"
                    "&query_layers=GDSPS_15km_SeaSfcHeight"
                    f"&bbox={bbox}&width=10&height=10&crs=CRS:84&i=5&j=5"
                    "&info_format=application/json"
                    f"&time={ts.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                    timeout=15,
                )
                feats = r.json().get("features", [])
                if feats:
                    v = feats[0]["properties"].get("value")
                    if v is not None:
                        return ts.strftime("%Y-%m-%dT%H:%M:%SZ"), float(v)
            except Exception:
                pass
            return None

        with _cf.ThreadPoolExecutor(max_workers=20) as pool:
            raw = list(pool.map(_one, timestamps))

        pairs = [item for item in raw if item]
        if not pairs:
            raise ValueError(f"No GDSPS data for {site_label}")

        times_out = [p[0] for p in pairs]
        # GDSPS SSH is already referenced to Mean Water Level — return as-is, no mean correction.
        vals_out  = [round(p[1], 4) for p in pairs]
        print(f"  GDSPS: {len(times_out)} steps for {site_label}")
        step = max(1, len(times_out) // 300)
        return times_out[::step], vals_out[::step], None

    except Exception as e:
        print(f"  GDSPS fetch failed for {site_label}: {e}")
        return None, None, None


# ── River level (WSC — OGC real-time API then Datamart CSV fallback) ─────

def fetch_river(station_id, provterr):
    """Fetch latest water level. Tries OGC API first, falls back to
    MSC Datamart hourly CSV."""

    # ── Method 1: OGC real-time API (same source as dashboard_lib) ──
    from datetime import date as _date, timedelta as _td
    current_m = None
    realtime_rows = []

    # Method 1a: OGC real-time (last ~7 days hourly) — for current reading + recent series
    try:
        r = get_with_retry(
            "https://api.weather.gc.ca/collections/hydrometric-realtime/items",
            params={"station_number": station_id, "limit": 720},
            timeout=30,
        )
        features = r.json().get("features", [])
        for f in features:
            props = f.get("properties", {})
            level = props.get("LEVEL")
            ts = props.get("DATETIME")
            if level is not None and ts:
                realtime_rows.append({"t": ts, "m": round(float(level), 3)})
        if realtime_rows:
            realtime_rows.sort(key=lambda x: x["t"])
            current_m = realtime_rows[-1]["m"]
            print(f"  WSC OGC realtime: {len(realtime_rows)} rows for {station_id}, current={current_m}")
    except Exception as e:
        print(f"  WSC OGC realtime failed for {station_id}: {e}")

    # Method 1b: OGC daily-mean for 30-day series
    daily_rows = []
    try:
        date_from = (_date.today() - _td(days=30)).strftime("%Y-%m-%d")
        date_to = _date.today().strftime("%Y-%m-%d")
        r2 = get_with_retry(
            "https://api.weather.gc.ca/collections/hydrometric-daily-mean/items",
            params={"STATION_NUMBER": station_id, "datetime": f"{date_from}/{date_to}",
                    "limit": 35, "f": "json"},
            timeout=30,
        )
        for f in r2.json().get("features", []):
            props = f.get("properties", {})
            level = props.get("MEAN") or props.get("LEVEL")
            ts = props.get("DATE") or props.get("DATETIME")
            if level is not None and ts:
                daily_rows.append({"t": ts, "m": round(float(level), 3)})
        if daily_rows:
            daily_rows.sort(key=lambda x: x["t"])
            if current_m is None:
                current_m = daily_rows[-1]["m"]
            print(f"  WSC OGC daily-mean: {len(daily_rows)} rows for {station_id}")
    except Exception as e:
        print(f"  WSC OGC daily-mean failed for {station_id}: {e}")

    # Merge: daily rows for backdrop, then real-time for the recent days
    # Deduplicate by date prefix — real-time wins for same-day entries
    if daily_rows or realtime_rows:
        merged = {}
        for row in daily_rows:
            key = row["t"][:10]
            merged[key] = row
        for row in realtime_rows:
            key = row["t"][:10]
            # Only add real-time if same day or newer than daily data
            if key >= (list(merged.keys())[0] if merged else ""):
                merged[key] = row
        # Build final series: daily for older days, all real-time rows for last week
        rt_dates = {r["t"][:10] for r in realtime_rows}
        series = [v for k, v in sorted(merged.items()) if k not in rt_dates]
        series += realtime_rows
        series.sort(key=lambda x: x["t"])
        if current_m is not None:
            print(f"  WSC merged series: {len(series)} rows for {station_id}")
            return {"current_m": current_m, "series": series}

    print(f"  WSC OGC: no data for {station_id}, falling back to Datamart")

    # ── Method 2: MSC Datamart — try hourly CSV first, then daily fallback ──
    prov = provterr.upper()
    urls_to_try = [
        f"https://dd.weather.gc.ca/hydrometric/csv/{prov}/hourly/{prov}_{station_id}_hourly_hydrometric.csv",
        f"https://dd.weather.gc.ca/today/hydrometric/csv/{prov}/hourly/{prov}_{station_id}_hourly_hydrometric.csv",
        f"https://dd.weather.gc.ca/hydrometric/csv/{prov}/daily/{prov}_{station_id}_daily_hydrometric.csv",
        f"https://dd.weather.gc.ca/today/hydrometric/csv/{prov}/daily/{prov}_{station_id}_daily_hydrometric.csv",
    ]
    for url in urls_to_try:
      try:
        r = get_with_retry(url, timeout=30)
        lines = [l for l in r.text.strip().splitlines() if l.strip()]
        header = [c.strip().strip('"') for c in lines[0].split(',')]
        level_idx = next(
            (i for i, h in enumerate(header) if "Water Level" in h or "Niveau" in h),
            2,
        )
        rows = []
        for line in lines[1:]:
            parts = line.split(',')
            if len(parts) > level_idx and parts[level_idx].strip():
                try:
                    m = float(parts[level_idx].strip().strip('"'))
                    ts = parts[1].strip().strip('"') if len(parts) > 1 else ""
                    rows.append({"t": ts, "m": round(m, 3)})
                except ValueError:
                    continue
        if not rows:
            print(f"  WSC Datamart: no valid rows for {station_id} ({url})")
            continue
        current_m = rows[-1]["m"]
        is_hourly = "hourly" in url
        print(f"  WSC Datamart {'hourly' if is_hourly else 'daily'} CSV: {len(rows)} rows, current={current_m}")
        return {"current_m": current_m, "series": rows}
      except Exception as e:
        print(f"  WSC Datamart failed ({url}): {e}")
        continue
    return None


# ── Marine forecast (EC RSS) ──────────────────────────────────────────────

def fetch_wave(lat, lon, tz):
    """Hourly wave height from Open-Meteo Marine API, downsampled to 2-hourly."""
    try:
        r = get_with_retry(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "wave_height",
                "forecast_days": 8,
                "timezone": tz,
            },
            timeout=30,
        )
        data = r.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        heights = hourly.get("wave_height", [])
        pairs = [
            {"t": t, "h": round(float(h), 2)}
            for t, h in zip(times, heights) if h is not None
        ]
        out = pairs[::2]  # 2-hourly to reduce payload
        print(f"  Wave: {len(out)} points")
        return out
    except Exception as e:
        print(f"  Wave fetch failed: {e}")
        return []


def fetch_marine_forecast(zone_id):
    import re
    url = f"https://weather.gc.ca/rss/marine/{zone_id}_e.xml"
    try:
        r = get_with_retry(url, timeout=30,
                           headers={"User-Agent": "alfred-portal/1.0 (research)"})
        text = r.text

        def clean(s):
            cdata = re.search(r'<!\[CDATA\[(.*?)\]\]>', s, re.DOTALL)
            if cdata:
                s = cdata.group(1)
            # Decode entities first so entity-encoded tags become real tags
            for ent, rep in [('&amp;','&'),('&lt;','<'),('&gt;','>'),('&nbsp;',' '),('&#13;','\n'),('&#10;','\n')]:
                s = s.replace(ent, rep)
            # Also strip any remaining entity-encoded <br/> that survived CDATA unwrap
            s = re.sub(r'&lt;br\s*/?&gt;', ' ', s, flags=re.IGNORECASE)
            s = re.sub(r'<br\s*/?>', '\n', s, flags=re.IGNORECASE)
            s = re.sub(r'<[^>]+>', ' ', s)
            s = re.sub(r'\n{3,}', '\n\n', s)
            s = re.sub(r' {2,}', ' ', s)
            # Strip "Issued …" trailing line
            s = re.sub(r'\n?Issued\s+\d.*$', '', s, flags=re.DOTALL)
            return s.strip()[:800]

        # Parse per-item (RSS 2.0 <item> or Atom <entry>)
        items = re.findall(r'<item[^>]*>(.*?)</item>', text, re.DOTALL)
        if not items:
            items = re.findall(r'<entry[^>]*>(.*?)</entry>', text, re.DOTALL)

        out = []
        for item in items:
            title_m = re.search(r'<title[^>]*>(.*?)</title>', item, re.DOTALL)
            content_m = (
                re.search(r'<description[^>]*>(.*?)</description>', item, re.DOTALL) or
                re.search(r'<summary[^>]*>(.*?)</summary>', item, re.DOTALL)
            )
            if not (title_m and content_m):
                continue
            title = clean(title_m.group(1))
            # Strip repeated zone name suffix from title (e.g. "- Yukon Coast")
            title = re.sub(r'\s*[-–]\s*[A-Z][a-z].*$', '', title).strip()
            body = clean(content_m.group(1))
            if not title or "No watches" in title or "Aucune veille" in title:
                continue
            if len(body) < 15:
                continue
            out.append({"title": title, "text": body})
            if len(out) >= 5:
                break

        if out:
            print(f"  Marine forecast: {len(out)} entries for zone {zone_id}")
        else:
            print(f"  Marine forecast: no usable entries for zone {zone_id}")
        return out or None
    except Exception as e:
        print(f"  Marine forecast fetch failed for zone {zone_id}: {e}")
        return None


# ── EC alerts ─────────────────────────────────────────────────────────────

def fetch_ec_alerts(prov="yt"):
    import re, xml.etree.ElementTree as ET
    # Try province feed; fall back to NT (Shingle Point is near the YT/NT border)
    for p in (prov, "nt"):
        url = f"https://weather.gc.ca/rss/warning/{p}_e.xml"
        try:
            r = requests.get(url, timeout=20,
                             headers={"User-Agent": "alfred-portal/1.0 (research dashboard)"})
            if r.status_code == 404:
                print(f"  EC alerts {p}_e.xml: 404, skipping")
                continue
            r.raise_for_status()
            text = r.text
            text = re.sub(r' xmlns[^"]*"[^"]*"', '', text)
            text = re.sub(r'<(\w+):(\w+)', r'<\2', text)
            text = re.sub(r'</(\w+):(\w+)', r'</\2', text)
            root = ET.fromstring(text)
            alerts = []
            for e in root.findall(".//entry"):
                title_el = e.find("title")
                if title_el is not None:
                    t = (title_el.text or "").strip()
                    if t and "No watches" not in t:
                        alerts.append(t)
            if alerts:
                print(f"  EC alerts ({p}): {len(alerts)} entries")
                return alerts[:5]
        except Exception as e:
            print(f"  EC alerts {p} failed: {e}")
    return []


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--community", required=True)
    args = ap.parse_args()

    cid = args.community
    config_path = COMMUNITIES_DIR / cid / "config.json"
    if not config_path.exists():
        print(f"ERROR: config not found at {config_path}")
        sys.exit(1)

    cfg = json.loads(config_path.read_text())
    lat = cfg["lat"]
    lon = cfg["lon"]
    tz = cfg.get("tz_name", "UTC")
    now_utc = datetime.now(timezone.utc)

    out_dir = DOCS_DIR / cid
    img_dir = out_dir / "img"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(exist_ok=True)

    # ── Fetch ──
    print("Fetching weather…")
    weather = fetch_weather(lat, lon, tz)

    tide = None
    total_water_level = None
    yearly_mean = cfg.get("water_level_yearly_mean")
    if cfg.get("tide_station_code") or cfg.get("type") == "coastal":
        # Try TOPAZ6 (Copernicus) first, then GDSPS (MSC), then IWLS tide prediction
        print("Fetching total water level (TOPAZ6)…")
        topaz_times, topaz_vals, _ = fetch_topaz_water_level(lat, lon, now_utc, cfg.get("site_display_name", cid), yearly_mean)
        print("Fetching total water level (GDSPS)…")
        gdsps_times, gdsps_vals, _ = fetch_gdsps_water_level(lat, lon, now_utc, cfg.get("site_display_name", cid))

        def _parse_iso(s):
            s = s.replace("Z", "+00:00") if s.endswith("Z") else s
            try:
                return datetime.fromisoformat(s).timestamp()
            except Exception:
                return 0.0

        def _make_wl(times, vals, source_label):
            if not times:
                return None
            now_ts = now_utc.timestamp()
            idx_now = min(range(len(times)), key=lambda i: abs(_parse_iso(times[i]) - now_ts))
            return {
                "current_m": round(vals[idx_now], 3),
                "series": [{"t": t, "m": v} for t, v in zip(times, vals)],
                "source": source_label,
            }

        twl_topaz = _make_wl(topaz_times, topaz_vals, "TOPAZ6 (Copernicus) · tide + surge")
        twl_gdsps = _make_wl(gdsps_times, gdsps_vals, "GDSPS (MSC/ECCC) · tide + surge")

        # Primary source for current_m: TOPAZ if available, else GDSPS
        twl_primary = twl_topaz or twl_gdsps
        total_water_level = None
        if twl_primary:
            total_water_level = {
                **twl_primary,
                "topaz": twl_topaz,
                "gdsps": twl_gdsps,
            }
        if cfg.get("tide_station_code"):
            print(f"Fetching tides (IWLS {cfg['tide_station_code']})…")
            tide = fetch_tide(cfg["tide_station_code"], cfg.get("tide_station_name", ""), now_utc)

    rivers = []
    for stn in cfg.get("hydrometric_stations", []):
        sid = stn["station_id"]
        prov = stn.get("provterr", "NT")
        print(f"Fetching river level ({sid})…")
        rd = fetch_river(sid, prov)
        if rd:
            entry = {
                **rd,
                "station_id": sid,
                "river_name": stn.get("river_name", ""),
                "heading": stn.get("heading", ""),
            }
            # Fetch full-year climatology (WaterOffice server-side, no CORS)
            print(f"Fetching river climatology ({sid})…")
            try:
                if fetch_hydrometric_climatology is None:
                    raise RuntimeError("dashboard_lib not available")
                doy_vals, cur_list, clim_unit = fetch_hydrometric_climatology(sid, clim_years=15, provterr=prov)
                if doy_vals:
                    _cy = _dt_mod.datetime.utcnow().year
                    entry["clim"] = {
                        "doy_values": {k: v for k, v in doy_vals.items()},
                        "current_year": cur_list,
                        "unit": clim_unit,
                        "hist_start": max(1970, _cy - 15),
                        "hist_end": _cy - 1,
                    }
            except Exception as _ce:
                print(f"  River climatology failed for {sid}: {_ce}")
            rivers.append(entry)
        else:
            print(f"  River fetch failed for {sid}")

    print("Fetching wave height…")
    wave = fetch_wave(lat, lon, tz)

    # Fetch wide MODIS banner image from NASA GIBS (landscape, centred on site)
    def fetch_modis_banner(lat, lon, out_path):
        from datetime import timedelta, date as _date
        # Wide bounding box: extend 10° west past Alaska, 4° east, 3° N/S
        lon_w, lon_e = lon - 15, lon + 15
        lat_s, lat_n = lat - 2.5, lat + 2.5
        bbox = f"{lon_w},{lat_s},{lon_e},{lat_n}"
        # Shingle Point fraction from left: (lon - lon_w) / (lon_e - lon_w)
        for delta in [1, 2, 3, 4]:
            d = _date.today() - timedelta(days=delta)
            try:
                r = get_with_retry(
                    "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi",
                    params={
                        "SERVICE":"WMS","REQUEST":"GetMap","VERSION":"1.3.0",
                        "LAYERS":"MODIS_Terra_CorrectedReflectance_TrueColor,Coastlines_15m,Reference_Labels_15m",
                        "CRS":"CRS:84","BBOX":bbox,
                        "WIDTH":"2200","HEIGHT":"640","FORMAT":"image/jpeg",
                        "TIME":d.strftime("%Y-%m-%d"),
                    },
                    timeout=60,
                )
                ct = r.headers.get("content-type","")
                if r.status_code==200 and "image" in ct:
                    try:
                        from PIL import Image as _Img, ImageDraw as _Draw, ImageFont as _Font
                        import io as _io
                        img = _Img.open(_io.BytesIO(r.content)).convert("RGB")
                        iw, ih = img.size
                        # Crop to 4:1
                        target_h = iw // 4
                        if ih > target_h:
                            top = (ih - target_h) // 2
                            img = img.crop((0, top, iw, top + target_h))
                        iw, ih = img.size

                        # Draw place labels — current community's map_points PLUS
                        # all other communities whose lat/lon falls in the banner extent
                        draw = _Draw.Draw(img)
                        try:
                            font = _Font.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
                            font_sm = _Font.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
                        except Exception:
                            font = font_sm = _Font.load_default()

                        # Build the full list of points to label
                        banner_pts = list(cfg.get("map_points", []))  # [lat, lon, label, dy?]
                        labeled_coords = set()  # deduplicate nearby community dots
                        # Add all other community centre points that fall in this banner
                        for _other_cfg_path in sorted(COMMUNITIES_DIR.glob("*/config.json")):
                            try:
                                import json as _json
                                _oc = _json.loads(_other_cfg_path.read_text(encoding="utf-8"))
                                _olat = float(_oc.get("lat", 0))
                                _olon = float(_oc.get("lon", 0))
                                if not (lon_w < _olon < lon_e and lat_s < _olat < lat_n):
                                    continue
                                _olbl = _oc.get("site_display_name") or _oc.get("name", "")
                                # Deduplicate if very close to an existing map_point
                                _key = (round(_olat, 1), round(_olon, 1))
                                if _key in labeled_coords:
                                    continue
                                labeled_coords.add(_key)
                                banner_pts.append([_olat, _olon, _olbl])
                            except Exception:
                                pass

                        # Collision-aware label placement
                        placed_boxes = []  # list of (x0,y0,x1,y1) already used

                        def _boxes_overlap(a, b, pad=6):
                            return not (a[2]+pad < b[0] or b[2]+pad < a[0] or
                                        a[3]+pad < b[1] or b[3]+pad < a[1])

                        def _find_label_pos(draw, lbl, font, dot_x, dot_y, iw, ih, placed):
                            pad_t = 4
                            r_dot = 8
                            try:
                                tb0 = draw.textbbox((0, 0), lbl, font=font)
                                tw = tb0[2] - tb0[0]
                                th = tb0[3] - tb0[1]
                            except Exception:
                                tw, th = len(lbl) * 12, 20
                            # Try candidates at increasing distances so close pairs can spread
                            base_candidates = []
                            for gap in (8, 18, 30, 46):
                                base_candidates += [
                                    (dot_x + r_dot + gap,  dot_y - th // 2),        # right
                                    (dot_x - r_dot - tw - gap, dot_y - th // 2),    # left
                                    (dot_x - tw // 2,    dot_y - r_dot - th - gap), # above
                                    (dot_x - tw // 2,    dot_y + r_dot + gap),       # below
                                    (dot_x + r_dot + gap, dot_y - r_dot - th - gap),# upper-right
                                    (dot_x - tw - r_dot - gap, dot_y - r_dot - th - gap), # upper-left
                                    (dot_x + r_dot + gap, dot_y + r_dot + gap),     # lower-right
                                    (dot_x - tw - r_dot - gap, dot_y + r_dot + gap),# lower-left
                                ]
                            for lx, ly in base_candidates:
                                box = (lx - pad_t, ly - pad_t, lx + tw + pad_t, ly + th + pad_t)
                                if box[0] < 4 or box[1] < 4 or box[2] > iw - 4 or box[3] > ih - 4:
                                    continue
                                if all(not _boxes_overlap(box, pb) for pb in placed):
                                    return lx, ly, box
                            # No clean spot found — return None to signal skip
                            return None, None, None

                        for pt in banner_pts:
                            pt_lat, pt_lon, pt_label = pt[0], pt[1], pt[2]
                            if not (lon_w < pt_lon < lon_e and lat_s < pt_lat < lat_n):
                                continue
                            px_x = int((pt_lon - lon_w) / (lon_e - lon_w) * iw)
                            px_y = int((lat_n - pt_lat) / (lat_n - lat_s) * ih)
                            # Skip points whose dot falls outside the image (can happen when
                            # map_points bbox is wider than the actual cropped image)
                            margin = 10
                            if not (margin <= px_x <= iw - margin and margin <= px_y <= ih - margin):
                                continue
                            # Dot
                            r_dot = 7
                            draw.ellipse([px_x-r_dot, px_y-r_dot, px_x+r_dot, px_y+r_dot],
                                         fill=(220, 60, 60), outline="white", width=2)
                            placed_boxes.append((px_x - r_dot, px_y - r_dot,
                                                 px_x + r_dot, px_y + r_dot))
                            # Find non-overlapping label position
                            lx, ly, lbox = _find_label_pos(
                                draw, pt_label, font_sm, px_x, px_y, iw, ih, placed_boxes)
                            if lx is None:
                                # No non-overlapping position found — skip label to avoid overlap
                                continue
                            placed_boxes.append(lbox)
                            # Label with dark background pill for readability against clouds
                            try:
                                tb = draw.textbbox((lx, ly), pt_label, font=font_sm)
                                pad = 4
                                overlay = _Img.new("RGBA", img.size, (0,0,0,0))
                                _Draw.Draw(overlay).rounded_rectangle(
                                    [tb[0]-pad, tb[1]-pad, tb[2]+pad, tb[3]+pad],
                                    radius=4, fill=(0,0,0,165))
                                img = _Img.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
                                draw = _Draw.Draw(img)
                            except Exception:
                                pass
                            draw.text((lx, ly), pt_label, font=font_sm, fill=(255,255,255))

                        buf = _io.BytesIO()
                        img.save(buf, "JPEG", quality=88)
                        out_path.write_bytes(buf.getvalue())
                    except Exception as crop_err:
                        print(f"  MODIS crop/label failed ({crop_err}), saving raw")
                        out_path.write_bytes(r.content)
                    print(f"  MODIS banner: {d} → {out_path.name} ({out_path.stat().st_size//1024} kB)")
                    return d.strftime("%Y-%m-%d")
            except Exception as e:
                print(f"  MODIS banner day -{delta} failed: {e}")
        return None

    banner_path = img_dir / "modis_banner.jpg"
    modis_date = fetch_modis_banner(lat, lon, banner_path)
    if not modis_date:
        print("  MODIS banner unavailable")

    # ── Shared image annotation helpers ──────────────────────────────────────
    def _annotate_img(img, date_str, meters_per_px):
        """Draw date label (top-left) and scale bar (bottom-right) on img."""
        from PIL import Image as _Img2, ImageDraw as _Draw2, ImageFont as _Font2
        import io as _io2, math as _math, datetime as _dt2
        draw = _Draw2.Draw(img)
        iw, ih = img.size
        _fs = max(16, int(20 * iw / 1000))  # scale so label looks same size across different native resolutions
        try:
            font_lg = _Font2.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", _fs)
            font_sm = _Font2.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max(14, _fs - 2))
        except Exception:
            font_lg = font_sm = _Font2.load_default()
        # Date label (with time if a full ISO datetime is provided)
        if date_str:
            try:
                if 'T' in date_str:
                    _dt_obj = _dt2.datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    try:
                        from zoneinfo import ZoneInfo as _ZI
                        _tz_name = cfg.get("tz_name", "America/Inuvik")
                        _local = _dt_obj.astimezone(_ZI(_tz_name))
                        _tz_abbr = _local.strftime('%Z')
                        label = _local.strftime(f"%-d %b %Y · %H:%M {_tz_abbr}")
                    except Exception:
                        label = _dt_obj.strftime("%-d %b %Y · %H:%M UTC")
                else:
                    d_obj = _dt2.date.fromisoformat(date_str)
                    label = d_obj.strftime("%-d %b %Y")
            except Exception:
                label = date_str
            pad = 6
            try:
                tb = draw.textbbox((0, 0), label, font=font_lg)
                overlay = _Img2.new("RGBA", img.size, (0, 0, 0, 0))
                _Draw2.Draw(overlay).rounded_rectangle(
                    [10 - pad, 10 - pad, tb[2] + 10 + pad, tb[3] + 10 + pad],
                    radius=5, fill=(0, 0, 0, 170))
                img = _Img2.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
                draw = _Draw2.Draw(img)
            except Exception:
                pass
            draw.text((11, 11), label, font=font_lg, fill=(0, 0, 0))
            draw.text((10, 10), label, font=font_lg, fill=(255, 255, 255))
        # Scale bar
        target_m = (iw // 4) * meters_per_px
        for nice in [200000, 100000, 50000, 25000, 20000, 10000, 5000, 2000, 1000, 500]:
            if nice <= target_m:
                scale_m = nice
                break
        else:
            scale_m = max(1, int(target_m))
        bar_px = max(10, int(scale_m / meters_per_px))
        bar_label = f"{scale_m // 1000} km" if scale_m >= 1000 else f"{scale_m} m"
        margin = 18
        bar_y = ih - margin - 16
        bar_x2 = iw - margin
        bar_x1 = bar_x2 - bar_px
        try:
            tb2 = draw.textbbox((0, 0), bar_label, font=font_sm)
            tw, th = tb2[2] - tb2[0], tb2[3] - tb2[1]
            overlay2 = _Img2.new("RGBA", img.size, (0, 0, 0, 0))
            _Draw2.Draw(overlay2).rounded_rectangle(
                [bar_x1 - 6, bar_y - th - 18, bar_x2 + 6, ih - margin + 6],
                radius=5, fill=(0, 0, 0, 160))
            img = _Img2.alpha_composite(img.convert("RGBA"), overlay2).convert("RGB")
            draw = _Draw2.Draw(img)
            draw.line([(bar_x1, bar_y - 5), (bar_x1, bar_y + 5)], fill="white", width=2)
            draw.line([(bar_x2, bar_y - 5), (bar_x2, bar_y + 5)], fill="white", width=2)
            draw.line([(bar_x1, bar_y), (bar_x2, bar_y)], fill="white", width=3)
            lx = (bar_x1 + bar_x2 - tw) // 2
            draw.text((lx, bar_y - th - 14), bar_label, font=font_sm, fill=(255, 255, 255))
        except Exception as _e:
            print(f"  scale bar failed: {_e}")
        return img

    def _overlay_gibs_coastlines(img, lon_w, lon_e, lat_s, lat_n):
        """Fetch GIBS Coastlines_15m+Reference_Labels_15m (transparent PNG) and composite."""
        import io as _io3
        from PIL import Image as _Img3
        try:
            r = get_with_retry(
                "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi",
                params={
                    "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0",
                    "LAYERS": "Coastlines_15m,Reference_Labels_15m",
                    "STYLES": "", "FORMAT": "image/png", "TRANSPARENT": "true",
                    "CRS": "CRS:84",
                    "BBOX": f"{lon_w},{lat_s},{lon_e},{lat_n}",
                    "WIDTH": str(img.size[0]), "HEIGHT": str(img.size[1]),
                },
                timeout=20,
            )
            if r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n":
                ol = _Img3.open(_io3.BytesIO(r.content)).convert("RGBA")
                base = img.convert("RGBA")
                base.alpha_composite(ol)
                return base.convert("RGB")
        except Exception as _e:
            print(f"  GIBS coastline overlay failed: {_e}")
        return img

    # ── VIIRS NOAA-20 true color (EPSG:3413, north-up) ──
    def fetch_viirs_image(out_path, max_hours_back=72):
        import io as _io, re as _re_v
        from datetime import datetime as _dtv, timedelta as _tdv
        bbox_3413 = cfg.get("modis_bbox_3413")
        rot = cfg.get("modis_rotation_deg", 0)
        if not bbox_3413:
            print("  VIIRS: no modis_bbox_3413 in config, skipping")
            return None
        _lon_v = cfg.get("lon", -140)
        _lat_v = cfg.get("lat", 69)
        _now_v = _dtv.utcnow()
        _start_v = _now_v - _tdv(hours=max_hours_back)

        # Step 1: find the most recent granules covering the site via CMR
        _cmr_map = [
            ("VJ109GA_NRT", "VIIRS_NOAA20_CorrectedReflectance_TrueColor_NRT"),
            ("VNP09GA_NRT", "VIIRS_SNPP_CorrectedReflectance_TrueColor_NRT"),
            ("VJ109GA",     "VIIRS_NOAA20_CorrectedReflectance_TrueColor"),
            ("VNP09GA",     "VIIRS_SNPP_CorrectedReflectance_TrueColor"),
        ]
        _candidates = []  # [(time_start_iso, gibs_layer), ...]
        for _short, _gibs_layer in _cmr_map:
            try:
                _cr = requests.get(
                    "https://cmr.earthdata.nasa.gov/search/granules.json",
                    params={
                        "short_name": _short,
                        "temporal": (f"{_start_v.strftime('%Y-%m-%dT%H:%M:%SZ')},"
                                     f"{_now_v.strftime('%Y-%m-%dT%H:%M:%SZ')}"),
                        "bounding_box": f"{_lon_v-5},{_lat_v-3},{_lon_v+5},{_lat_v+3}",
                        "sort_key": "-start_date",
                        "page_size": "5",
                    },
                    timeout=20,
                )
                for _e in _cr.json().get("feed", {}).get("entry", []):
                    _ts = _e.get("time_start", "")
                    if not _ts:
                        _pgid = _e.get("producer_granule_id", "")
                        _gm = _re_v.search(r'\.A(\d{4})(\d{3})\.(\d{2})(\d{2})\.', _pgid)
                        if _gm:
                            _gd = _dtv(_gm.group(1).__class__(int(_gm.group(1))), 1, 1) + _tdv(days=int(_gm.group(2))-1)
                            _ts = f"{_gd.strftime('%Y-%m-%d')}T{_gm.group(3)}:{_gm.group(4)}:00Z"
                    if _ts:
                        _candidates.append((_ts, _gibs_layer))
            except Exception as _ce:
                print(f"  VIIRS CMR {_short} failed: {_ce}")

        # Sort most-recent first, deduplicate by timestamp
        _candidates.sort(key=lambda x: x[0], reverse=True)
        _seen_ts, _dedup = set(), []
        for _c in _candidates:
            if _c[0] not in _seen_ts:
                _seen_ts.add(_c[0]); _dedup.append(_c)
        _candidates = _dedup

        # Fall back to day-by-day date list if CMR returned nothing
        if not _candidates:
            print("  VIIRS: CMR returned no granules, falling back to daily dates")
            from datetime import date as _ddate
            for _delta in range(0, 5):
                _d = _ddate.today() - _tdv(days=_delta)
                for _lyr in ["VIIRS_NOAA20_CorrectedReflectance_TrueColor_NRT",
                             "VIIRS_SNPP_CorrectedReflectance_TrueColor_NRT",
                             "VIIRS_NOAA20_CorrectedReflectance_TrueColor",
                             "VIIRS_SNPP_CorrectedReflectance_TrueColor"]:
                    _candidates.append((_d.strftime("%Y-%m-%d"), _lyr))

        # Step 2: try each candidate with exact TIME in GIBS
        for _viirs_date_str, layer in _candidates[:12]:
                try:
                    r = get_with_retry(
                        "https://gibs.earthdata.nasa.gov/wms/epsg3413/best/wms.cgi",
                        params={
                            "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.1.1",
                            "LAYERS": f"{layer}",
                            "STYLES": "",
                            "FORMAT": "image/png",
                            "TRANSPARENT": "false",
                            "SRS": "EPSG:3413",
                            "BBOX": bbox_3413,
                            "WIDTH": "1500", "HEIGHT": "1500",
                            "TIME": _viirs_date_str[:10],  # GIBS WMS accepts YYYY-MM-DD
                        },
                        timeout=30,
                    )
                    if r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n":
                        from PIL import Image as _Img, ImageDraw as _Draw, ImageFont as _Font
                        img = _Img.open(_io.BytesIO(r.content)).convert("RGB")
                        rotated = img.rotate(rot, resample=_Img.BICUBIC, expand=False)
                        w, h = rotated.size
                        sz = 1000
                        left, top = (w - sz) // 2, (h - sz) // 2
                        cropped = rotated.crop((left, top, left + sz, top + sz))
                        # Skip scenes where the centre (site location) has no-data.
                        # No-data in GIBS VIIRS is pure black (0,0,0). Dark ocean/water
                        # has small but non-zero channel values and must NOT be rejected.
                        import numpy as _np
                        _carr = _np.array(cropped.crop((350, 350, 650, 650)))
                        _pure_black = (_carr.max(axis=2) == 0)  # pixels where all channels = 0
                        _nodata_frac = _pure_black.mean()
                        print(f"  VIIRS {layer} {_viirs_date_str}: centre {_nodata_frac:.0%} no-data — using")
                        # Compute metres per pixel from bbox
                        try:
                            _bparts = [float(x) for x in bbox_3413.split(",")]
                            _mpp = (_bparts[2] - _bparts[0]) / 1500.0
                        except Exception:
                            _bparts = None
                            _mpp = 400.0
                        # Polar-stereo projection (needed for both coastline and labels)
                        if _bparts:
                            import math as _m
                            def _ll_to_ps3413(phi_d, lam_d):
                                a, e = 6378137.0, 0.0818191908
                                phi0, lam0 = _m.radians(70.0), _m.radians(-45.0)
                                phi, lam = _m.radians(phi_d), _m.radians(lam_d)
                                def _t(p):
                                    es = e * _m.sin(p)
                                    return _m.tan(_m.pi/4 - p/2) / ((1-es)/(1+es))**(e/2)
                                m0 = _m.cos(phi0) / _m.sqrt(1-(e*_m.sin(phi0))**2)
                                rho = a * m0 * _t(phi) / _t(phi0)
                                return rho*_m.sin(lam-lam0), -rho*_m.cos(lam-lam0)
                            # Coastline overlay from local OSM GeoJSON
                            _coast_path = COMMUNITIES_DIR / cid / cfg.get("coastline_geojson_path", "coastline_data.geojson")
                            if _coast_path.exists():
                                try:
                                    import json as _json2
                                    with open(_coast_path) as _cf:
                                        _coast = _json2.load(_cf)
                                    _draw_coast = _Draw.Draw(cropped)
                                    for _feat in _coast.get("features", []):
                                        _geom = _feat.get("geometry", {})
                                        if _geom.get("type") != "LineString":
                                            continue
                                        _prev = None
                                        for _clon, _clat in _geom.get("coordinates", []):
                                            _cx3, _cy3 = _ll_to_ps3413(_clat, _clon)
                                            _cpx = (_cx3 - _bparts[0]) / (_bparts[2]-_bparts[0]) * 1500
                                            _cpy = (_bparts[3]-_cy3) / (_bparts[3]-_bparts[1]) * 1500
                                            _th2 = _m.radians(-rot)
                                            _crx = 750 + (_cpx-750)*_m.cos(_th2) - (_cpy-750)*_m.sin(_th2)
                                            _cry = 750 + (_cpx-750)*_m.sin(_th2) + (_cpy-750)*_m.cos(_th2)
                                            _cpxc, _cpyc = _crx-250, _cry-250
                                            if _prev is not None:
                                                _draw_coast.line([_prev, (_cpxc, _cpyc)], fill=(40, 40, 40), width=4)
                                                _draw_coast.line([_prev, (_cpxc, _cpyc)], fill=(255, 255, 255), width=2)
                                            _prev = (_cpxc, _cpyc)
                                except Exception as _ce:
                                    print(f"  VIIRS coastline overlay failed: {_ce}")
                            try:
                                _draw_lbl = _Draw.Draw(cropped)
                                _font_pt = _Font.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26) if hasattr(_Font,'truetype') else _Font.load_default()
                                for _pt in cfg.get("map_points",[]):
                                    _px3, _py3 = _ll_to_ps3413(_pt[0], _pt[1])
                                    # Map to 1500×1500 pixels
                                    _px15 = (_px3 - _bparts[0]) / (_bparts[2]-_bparts[0]) * 1500
                                    _py15 = (_bparts[3] - _py3) / (_bparts[3]-_bparts[1]) * 1500
                                    # Rotate around centre (750,750)
                                    _th = _m.radians(-rot)
                                    _rx = 750 + (_px15-750)*_m.cos(_th) - (_py15-750)*_m.sin(_th)
                                    _ry = 750 + (_px15-750)*_m.sin(_th) + (_py15-750)*_m.cos(_th)
                                    # To 1000×1000 crop (offset 250)
                                    _cx, _cy = int(_rx-250), int(_ry-250)
                                    if not (8 <= _cx <= 992 and 8 <= _cy <= 992):
                                        continue
                                    r_dot = 7
                                    _draw_lbl.ellipse([_cx-r_dot,_cy-r_dot,_cx+r_dot,_cy+r_dot], fill=(220,60,60), outline="white", width=2)
                                    _lbl = _pt[2] if len(_pt)>2 else ""
                                    if _lbl:
                                        try:
                                            _tb = _draw_lbl.textbbox((_cx+12,_cy-14),_lbl,font=_font_pt)
                                            _ov = _Img.new("RGBA",cropped.size,(0,0,0,0))
                                            _Draw.Draw(_ov).rounded_rectangle([_tb[0]-3,_tb[1]-3,_tb[2]+3,_tb[3]+3],radius=3,fill=(0,0,0,160))
                                            cropped = _Img.alpha_composite(cropped.convert("RGBA"),_ov).convert("RGB")
                                            _draw_lbl = _Draw.Draw(cropped)
                                        except Exception:
                                            pass
                                        _draw_lbl.text((_cx+12,_cy-14),_lbl,font=_font_pt,fill=(255,255,255))
                            except Exception as _le:
                                print(f"  VIIRS place labels failed: {_le}")
                        cropped = _annotate_img(cropped, _viirs_date_str, _mpp)
                        buf = _io.BytesIO()
                        cropped.save(buf, "PNG", optimize=True)
                        out_path.write_bytes(buf.getvalue())
                        src = "NOAA-20" if "NOAA20" in layer else "SNPP"
                        print(f"  VIIRS ({src}): {_viirs_date_str} → {out_path.name} ({out_path.stat().st_size//1024} kB)")
                        return _viirs_date_str  # full ISO datetime if CMR succeeded
                except Exception as e:
                    print(f"  VIIRS {layer} {_viirs_date_str} failed: {e}")
        print("  VIIRS unavailable")
        return None

    print("Fetching VIIRS true color…")
    viirs_date = fetch_viirs_image(img_dir / "viirs.png")
    # If fetch failed but a previous image exists, carry forward the old date
    if not viirs_date and (img_dir / "viirs.png").exists():
        try:
            _prev_dj = out_dir / "data.json"
            if _prev_dj.exists():
                import json as _jv
                _prev = _jv.loads(_prev_dj.read_text(encoding="utf-8"))
                if _prev.get("viirs_date"):
                    viirs_date = _prev["viirs_date"]
                    print(f"  VIIRS: carrying forward previous date {viirs_date}")
        except Exception:
            pass

    # ── Sentinel-2 true color (Sentinel Hub Copernicus) ──
    def get_sh_token():
        import os
        cid_val = os.environ.get("SENTINEL_HUB_CLIENT_ID", "")
        sec_val = os.environ.get("SENTINEL_HUB_CLIENT_SECRET", "")
        if not cid_val or not sec_val:
            return None
        try:
            r = requests.post(
                "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
                data={"grant_type": "client_credentials",
                      "client_id": cid_val, "client_secret": sec_val},
                timeout=20,
            )
            return r.json().get("access_token")
        except Exception as e:
            print(f"  SH token failed: {e}")
            return None

    def fetch_s2_image(token, half_width_m, out_path, max_days_back=30):
        import io as _io
        utm_epsg = cfg.get("utm_epsg", "32608")
        cx = cfg.get("utm_center_x")
        cy = cfg.get("utm_center_y")
        if not cx or not cy:
            print("  S2: no utm_center_x/y in config, skipping")
            return None
        bbox = [cx - half_width_m, cy - half_width_m, cx + half_width_m, cy + half_width_m]
        # Highlight Optimized Natural Color: sigmoid tone-map + gamma, matching Copernicus Browser
        # adj(v) = (gain*v / (gain*v + C))^(1/gamma)
        # gain=2.5, C=0.55, gamma=1.6 → lifts dark land/water, compresses clouds without clipping
        evalscript = (
            "//VERSION=3\n"
            "function setup(){return{input:[{bands:[\"B04\",\"B03\",\"B02\",\"dataMask\"]}],"
            "output:{bands:4,sampleType:\"AUTO\"}};}\n"
            "function evaluatePixel(s){\n"
            "  if(!s.dataMask)return[0,0,0,0];\n"
            "  const g=2.5,C=0.55,gm=1.6;\n"
            "  function adj(v){var x=v*g;return Math.pow(x/(x+C),1/gm);}\n"
            "  return[adj(s.B04),adj(s.B03),adj(s.B02),1];\n"
            "}"
        )
        # Find most recent acquisition date from catalog first
        try:
            from datetime import timedelta as _td, datetime as _dtparse
            end_dt = now_utc
            start_dt = end_dt - _td(days=max_days_back)
            _cat_r = requests.post(
                "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search",
                json={
                    "bbox": [lon - 0.5, lat - 0.3, lon + 0.5, lat + 0.3],
                    "datetime": (f"{start_dt.strftime('%Y-%m-%dT00:00:00Z')}/"
                                 f"{end_dt.strftime('%Y-%m-%dT23:59:59Z')}"),
                    "collections": ["sentinel-2-l2a"],
                    "limit": 10,
                },
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=15,
            )
            _feats = _cat_r.json().get("features", [])
            if _feats:
                _feats.sort(key=lambda f: f.get("properties", {}).get("datetime", ""), reverse=True)
                _acq_iso = _feats[0]["properties"]["datetime"]
                _acq_dt = _dtparse.fromisoformat(_acq_iso.replace("Z", "+00:00"))
                _proc_from = (_acq_dt - _td(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
                _proc_to   = (_acq_dt + _td(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                _acq_iso = None
                _proc_from = start_dt.strftime("%Y-%m-%dT00:00:00Z")
                _proc_to   = end_dt.strftime("%Y-%m-%dT23:59:59Z")
        except Exception as _cat_pre:
            print(f"  S2 pre-catalog failed: {_cat_pre}")
            from datetime import timedelta as _td
            end_dt = now_utc
            start_dt = end_dt - _td(days=max_days_back)
            _acq_iso = None
            _proc_from = start_dt.strftime("%Y-%m-%dT00:00:00Z")
            _proc_to   = end_dt.strftime("%Y-%m-%dT23:59:59Z")

        # Fetch via Process API using the specific acquisition date window
        try:
            proc_r = requests.post(
                "https://sh.dataspace.copernicus.eu/api/v1/process",
                json={
                    "input": {
                        "bounds": {
                            "bbox": bbox,
                            "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{utm_epsg}"},
                        },
                        "data": [{
                            "dataFilter": {
                                "timeRange": {
                                    "from": _proc_from,
                                    "to": _proc_to,
                                },
                                "mosaickingOrder": "mostRecent",
                                "maxCloudCoverage": 100,
                            },
                            "type": "sentinel-2-l2a",
                        }],
                    },
                    "output": {
                        "width": 1200, "height": 1200,
                        "responses": [{"identifier": "default",
                                       "format": {"type": "image/png"}}],
                    },
                    "evalscript": evalscript,
                },
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                timeout=60,
            )
            ct = proc_r.headers.get("content-type", "")
            if proc_r.status_code != 200 or "image" not in ct:
                print(f"  S2 process failed: {proc_r.status_code} {proc_r.text[:200]}")
                return None
            import io as _io4
            from PIL import Image as _Img4, ImageStat as _IStat4
            s2_rgba = _Img4.open(_io4.BytesIO(proc_r.content)).convert("RGBA")
            # Coverage check: site center must have valid (non-transparent) pixels
            _cw, _ch = s2_rgba.size
            _cpx = int(_cw * 0.4), int(_ch * 0.6)  # centre sample region
            _alpha_band = s2_rgba.split()[3]
            _center_crop = _alpha_band.crop((_cw//4, _ch//4, 3*_cw//4, 3*_ch//4))
            _valid_frac = sum(1 for p in _center_crop.getdata() if p > 10) / (_center_crop.width * _center_crop.height)
            if _valid_frac < 0.3:
                print(f"  S2 {half_width_m//1000}km: <30% centre coverage ({_valid_frac:.1%}), skipping")
                return None
            # Composite over dark ocean background
            bg_col = (10, 15, 26)
            s2_bg = _Img4.new("RGB", s2_rgba.size, bg_col)
            s2_bg.paste(s2_rgba, mask=s2_rgba.split()[3])
            s2_img = s2_bg
            # Brightness check
            _br = sum(_IStat4.Stat(s2_img).mean) / 3.0
            if _br < 5.0:
                print(f"  S2 {half_width_m//1000}km: too dark ({_br:.1f}), skipping")
                return None
            # Use acquisition datetime from catalog pre-fetch
            date_str = _acq_iso or end_dt.strftime("%Y-%m-%d")
            # Coastline overlay from local OSM GeoJSON
            import json as _json_s2, math as _math_s2
            from PIL import ImageDraw as _Draw_s2, ImageFont as _Font_s2
            # Define _ll_to_utm unconditionally so place labels can use it even
            # when there is no coastline GeoJSON to draw
            try:
                from pyproj import Transformer as _Tr
                _proj_s2 = _Tr.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
                def _ll_to_utm(plat, plon):
                    return _proj_s2.transform(plon, plat)
            except Exception:
                import math as _pm
                _K0, _E, _a = 0.9996, 0.0818191908426215, 6378137.0
                _zone = int(utm_epsg) - 32600
                _lon0 = _pm.radians((_zone - 1) * 6 - 180 + 3)
                def _ll_to_utm(plat, plon):
                    lat_r, lon_r = _pm.radians(plat), _pm.radians(plon)
                    N = _a / _pm.sqrt(1 - (_E*_pm.sin(lat_r))**2)
                    T = _pm.tan(lat_r)**2; C = (_E**2/(1-_E**2))*_pm.cos(lat_r)**2
                    A = _pm.cos(lat_r)*(lon_r - _lon0)
                    M = _a*(lat_r*(1-_E**2/4-3*_E**4/64)-_pm.sin(2*lat_r)*(3*_E**2/8+3*_E**4/32)+_pm.sin(4*lat_r)*(15*_E**4/256))
                    x = _K0*N*(A+(1-T+C)*A**3/6) + 500000
                    y = _K0*(M+N*_pm.tan(lat_r)*(A**2/2+(5-T+9*C)*A**4/24))
                    if plat < 0: y += 10000000
                    return x, y
            _mpp_s2 = (2 * half_width_m) / 1200.0
            _cx_s2 = cfg.get("utm_center_x", cx)
            _cy_s2 = cfg.get("utm_center_y", cy)
            def _utm_to_px(ux, uy):
                _px = 600 + (ux - _cx_s2) / _mpp_s2
                _py = 600 - (uy - _cy_s2) / _mpp_s2
                return _px, _py
            _coast_path_s2 = COMMUNITIES_DIR / cid / cfg.get("coastline_geojson_path", "coastline_data.geojson")
            if _coast_path_s2.exists():
                try:
                    with open(_coast_path_s2) as _cf2:
                        _coast2 = _json_s2.load(_cf2)
                    _draw_cs = _Draw_s2.Draw(s2_img)
                    for _feat2 in _coast2.get("features", []):
                        _geom2 = _feat2.get("geometry", {})
                        if _geom2.get("type") != "LineString":
                            continue
                        _prev2 = None
                        for _clon2, _clat2 in _geom2.get("coordinates", []):
                            _ux, _uy = _ll_to_utm(_clat2, _clon2)
                            _ppx, _ppy = _utm_to_px(_ux, _uy)
                            if _prev2 is not None:
                                _draw_cs.line([_prev2, (_ppx, _ppy)], fill=(40, 40, 40), width=4)
                                _draw_cs.line([_prev2, (_ppx, _ppy)], fill=(255, 255, 255), width=2)
                            _prev2 = (_ppx, _ppy)
                    print(f"  S2: coastline overlay drawn")
                except Exception as _ce2:
                    print(f"  S2 coastline overlay failed: {_ce2}")
            # Place-name labels
            _pts_s2 = cfg.get("map_points", [])
            if _pts_s2:
                try:
                    _draw_lbl2 = _Draw_s2.Draw(s2_img)
                    _font_s2 = _Font_s2.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26) if hasattr(_Font_s2, 'truetype') else _Font_s2.load_default()
                    _MARGIN = 20
                    for _pt2 in _pts_s2:
                        _ux2, _uy2 = _ll_to_utm(_pt2[0], _pt2[1])
                        _ppx2, _ppy2 = _utm_to_px(_ux2, _uy2)
                        if not (-_MARGIN <= _ppx2 <= 1200 + _MARGIN and -_MARGIN <= _ppy2 <= 1200 + _MARGIN):
                            continue
                        _rdot = 7
                        _draw_lbl2.ellipse([_ppx2 - _rdot, _ppy2 - _rdot, _ppx2 + _rdot, _ppy2 + _rdot], fill=(220, 60, 60), outline="white", width=2)
                        _lbl2 = _pt2[2] if len(_pt2) > 2 else ""
                        _dy2 = _pt2[3] if len(_pt2) > 3 else -14
                        if _lbl2:
                            try:
                                _tb2 = _draw_lbl2.textbbox((_ppx2 + 12, _ppy2 + _dy2), _lbl2, font=_font_s2)
                                _ov2 = _Img4.new("RGBA", s2_img.size, (0, 0, 0, 0))
                                _Draw_s2.Draw(_ov2).rounded_rectangle([_tb2[0]-3, _tb2[1]-3, _tb2[2]+3, _tb2[3]+3], radius=3, fill=(0, 0, 0, 160))
                                s2_img = _Img4.alpha_composite(s2_img.convert("RGBA"), _ov2).convert("RGB")
                                _draw_lbl2 = _Draw_s2.Draw(s2_img)
                            except Exception:
                                pass
                            _draw_lbl2.text((_ppx2 + 12, _ppy2 + _dy2), _lbl2, font=_font_s2, fill=(255, 255, 255))
                except Exception as _le2:
                    print(f"  S2 place labels failed: {_le2}")
            # Scale: 2*half_width_m across 1200px
            _mpp = (2 * half_width_m) / 1200.0
            s2_img = _annotate_img(s2_img, date_str, _mpp)
            buf = _io4.BytesIO()
            s2_img.save(buf, "PNG", optimize=True)
            out_path.write_bytes(buf.getvalue())
            print(f"  S2 {half_width_m//1000}km: {date_str} → {out_path.name} ({out_path.stat().st_size//1024} kB)")
            return date_str
        except Exception as e:
            print(f"  S2 process error: {e}")
        return None

    print("Fetching Sentinel-2 true color…")
    sh_token = get_sh_token()
    s2_date = None
    if sh_token:
        s2_date = fetch_s2_image(sh_token, 150_000, img_dir / "s2_150.png")
        if s2_date:
            fetch_s2_image(sh_token, 25_000, img_dir / "s2_50.png")
    else:
        print("  Sentinel-2 skipped (no SH credentials in env)")
    # Carry forward old S2 date if fetch failed but image exists
    if not s2_date and (img_dir / "s2_150.png").exists():
        try:
            _prev_dj2 = out_dir / "data.json"
            if _prev_dj2.exists():
                import json as _js2
                _prev2 = _js2.loads(_prev_dj2.read_text(encoding="utf-8"))
                if _prev2.get("s2_date"):
                    s2_date = _prev2["s2_date"]
                    print(f"  S2: carrying forward previous date {s2_date}")
        except Exception:
            pass

    marine = None
    if cfg.get("marine_zone_id"):
        print(f"Fetching marine forecast (zone {cfg['marine_zone_id']})…")
        marine = fetch_marine_forecast(cfg["marine_zone_id"])
        if not marine:
            print("  Marine forecast unavailable")

    print("Fetching EC alerts…")
    alerts = fetch_ec_alerts()

    # ── Write data.json ──
    data = {
        "community": {
            "id": cid,
            "name": cfg.get("site_display_name", cfg.get("name", cid)),
            "name_alt": cfg.get("name_alt", ""),
            "lat": lat,
            "lon": lon,
            "tz": tz,
            "institution": cfg.get("institution_text", ""),
            "blocks": cfg.get("blocks", []),
            "webcam_url": cfg.get("webcam_url", ""),
            "webcam_label": cfg.get("webcam_label", ""),
            "webcam_page_url": cfg.get("webcam_page_url", ""),
        },
        "updated_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modis_date": modis_date,
        "viirs_date": viirs_date,
        "s2_date": s2_date,
        "weather": weather,
        "total_water_level": total_water_level,
        "tide": tide,
        "rivers": rivers,
        "wave": wave,
        "marine": marine,
        "alerts": {"weather": alerts},
        "external_links": cfg.get("external_links", []),
    }

    data_path = out_dir / "data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {data_path}")

    # ── Copy chart PNGs ──
    charts_src = COMMUNITIES_DIR / cid / "charts"
    copied = 0
    if charts_src.exists():
        for png in sorted(charts_src.glob("*.png")):
            import shutil
            shutil.copy2(png, img_dir / png.name)
            copied += 1
        print(f"Copied {copied} chart PNGs")
    else:
        print(f"  No charts/ folder for {cid}")

    print("Done.")


if __name__ == "__main__":
    main()
