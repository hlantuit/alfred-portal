#!/usr/bin/env python3
"""
Generate GitHub Pages output for a community dashboard.

Writes docs/<community-id>/data.json  (fetched by index.html on every page load)
Copies chart PNGs from communities/<community-id>/charts/ → docs/<community-id>/img/

The existing Notion pipeline is not touched.
"""

import argparse
import json
import math
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

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


def get_with_retry(url, params=None, timeout=30, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt+1}/{retries-1} for {url}: {e}")
    raise RuntimeError("unreachable")


# ── Weather ──────────────────────────────────────────────────────────────────

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
            "hourly": "cloudcover,windspeed_10m,winddirection_10m,pressure_msl",
            "daily": ",".join([
                "temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max", "windspeed_10m_max",
                "windgusts_10m_max", "weathercode",
            ]),
            "timezone": tz,
            "forecast_days": 10,
            "models": "gem_seamless",
        },
    )
    d = r.json()
    cur = d.get("current", {})
    daily = d.get("daily", {})

    now_label_idx = 0  # today
    forecast = []
    dates = daily.get("time", [])
    for i, date in enumerate(dates):
        dt = datetime.strptime(date, "%Y-%m-%d")
        if i == 0:
            day_label = "Today"
        elif i == 1:
            day_label = "Tomorrow"
        else:
            day_label = dt.strftime("%a %-d")
        forecast.append({
            "date": date,
            "day": day_label,
            "weathercode": daily.get("weathercode", [None] * 10)[i],
            "hi": round(daily.get("temperature_2m_max", [None] * 10)[i] or 0),
            "lo": round(daily.get("temperature_2m_min", [None] * 10)[i] or 0),
            "precip_prob": daily.get("precipitation_probability_max", [0] * 10)[i] or 0,
            "wind_max": round(daily.get("windspeed_10m_max", [0] * 10)[i] or 0),
            "gusts_max": round(daily.get("windgusts_10m_max", [0] * 10)[i] or 0),
        })

    # Hourly series for sparklines (next 48h)
    hourly = d.get("hourly", {})
    times_h = hourly.get("time", [])
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    start_i = next((i for i, t in enumerate(times_h) if t[:13] >= now_str), 0)
    def h_slice(key, n=48):
        vals = hourly.get(key, [])
        return [round(v, 1) if v is not None else None for v in vals[start_i:start_i+n]]

    return {
        "temperature": round(cur.get("temperature_2m", 0) or 0, 1),
        "feels_like": round(cur.get("apparent_temperature", 0) or 0, 1),
        "humidity": round(cur.get("relativehumidity_2m", 0) or 0),
        "wind_speed": round(cur.get("windspeed_10m", 0) or 0),
        "wind_direction": round(cur.get("winddirection_10m", 0) or 0),
        "wind_dir_label": wind_dir_label(cur.get("winddirection_10m", 0) or 0),
        "weathercode": cur.get("weathercode", 0),
        "condition": WMO_LABELS.get(cur.get("weathercode", 0), "Unknown"),
        "pressure": round(cur.get("surface_pressure", 0) or 0),
        "cloudcover": round(cur.get("cloudcover", 0) or 0),
        "forecast": forecast,
        "hourly_temp": h_slice("temperature_2m"),
        "hourly_wind": h_slice("windspeed_10m"),
        "hourly_wind_dir": h_slice("winddirection_10m"),
        "hourly_pressure": h_slice("pressure_msl"),
        "hourly_cloud": h_slice("cloudcover"),
    }


# ── Tides / Total Water Level ─────────────────────────────────────────────

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
        return None

    from_dt = now_utc - timedelta(hours=1)
    to_dt = now_utc + timedelta(hours=36)

    try:
        r = get_with_retry(
            f"https://api-iwls.dfo-mpo.gc.ca/api/v1/stations/{station_id}/data",
            params={
                "time-series-code": "wlp",
                "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            timeout=30,
        )
        points = r.json()
    except Exception as e:
        print(f"  IWLS data fetch failed: {e}")
        return None

    if not points:
        return None

    # Find current value (closest to now)
    now_ts = now_utc.timestamp()
    best = min(points, key=lambda p: abs(
        datetime.fromisoformat(p["eventDate"].replace("Z", "+00:00")).timestamp() - now_ts
    ))
    current_m = round(best.get("value", 0), 3)

    # Trend: compare now vs 30 min ago
    trend = "steady"
    idx_now = points.index(best)
    if idx_now > 0:
        prev = points[max(0, idx_now - 6)]  # ~30 min back (5-min intervals)
        delta = best.get("value", 0) - prev.get("value", 0)
        if delta > 0.03:
            trend = "rising"
        elif delta < -0.03:
            trend = "falling"

    # Next high and low
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

    def fmt_extreme(pts_list):
        if not pts_list:
            return None
        p = pts_list[0]
        return {
            "time_utc": p["eventDate"],
            "m": round(p.get("value", 0), 2),
        }

    # 48-hour series for sparkline
    series = [{"t": p["eventDate"], "m": round(p.get("value", 0), 3)} for p in points]

    return {
        "current_m": current_m,
        "trend": trend,
        "next_high": fmt_extreme(highs),
        "next_low": fmt_extreme(lows),
        "series_48h": series,
    }


# ── River level (WSC) ────────────────────────────────────────────────────

def fetch_river(station_id):
    try:
        # Get axes to find the right parameter
        axes_r = get_with_retry(
            f"https://wateroffice.ec.gc.ca/services/real_time_graph_axes/json/inline"
            f"?station_id={station_id}",
            timeout=20,
        )
        axes = axes_r.json()
        param_ids = [a["parameterid"] for a in axes]
        param1 = next((p for p in param_ids if p in ("46", "3")), param_ids[0] if param_ids else "46")

        import datetime as _dt
        today = _dt.date.today()
        yesterday = today - _dt.timedelta(days=2)
        wo_url = (
            f"https://wateroffice.ec.gc.ca/services/real_time_graph/json/inline"
            f"?station={station_id}&start_date={yesterday}&end_date={today}"
            f"&param1={param1}&param2={param1}"
        )
        data_r = get_with_retry(wo_url, timeout=60)
        data = data_r.json()

        series_raw = data.get("series", {}).get(param1, {}).get("data", [])
        if not series_raw:
            return None

        series_sorted = sorted(series_raw, key=lambda x: x[0])
        latest = series_sorted[-1]
        current_m = round(latest[1], 3) if latest[1] is not None else None

        # Build a thinned 48h series (every 15 min → ~192 pts, thin to ~48)
        step = max(1, len(series_sorted) // 48)
        series = [{"t": p[0], "m": round(p[1], 3)} for p in series_sorted[::step] if p[1] is not None]

        return {"current_m": current_m, "series_48h": series}
    except Exception as e:
        print(f"  WSC fetch failed for {station_id}: {e}")
        return None


# ── Marine forecast ─────────────────────────────────────────────────────

def fetch_marine_forecast(zone_id):
    import xml.etree.ElementTree as ET
    try:
        r = get_with_retry(f"https://weather.gc.ca/rss/marine/{zone_id}_e.xml", timeout=20)
        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//atom:entry", ns) or root.findall(".//entry")
        out = []
        for e in entries[:4]:
            title_el = e.find("atom:title", ns) or e.find("title")
            summary_el = e.find("atom:summary", ns) or e.find("summary")
            if title_el is not None and summary_el is not None:
                out.append({
                    "title": (title_el.text or "").strip(),
                    "text": (summary_el.text or "").strip()[:400],
                })
        return out or None
    except Exception as e:
        print(f"  Marine forecast fetch failed for zone {zone_id}: {e}")
        return None


# ── Alerts (EC atom) ─────────────────────────────────────────────────────

def fetch_ec_alerts(prov="yt"):
    import xml.etree.ElementTree as ET
    try:
        r = get_with_retry(f"https://weather.gc.ca/rss/warning/{prov}_e.xml", timeout=20)
        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//atom:entry", ns) or root.findall(".//entry")
        alerts = []
        for e in entries[:10]:
            title_el = e.find("atom:title", ns) or e.find("title")
            t = (title_el.text or "").strip() if title_el is not None else ""
            if t and "No watches or warnings" not in t:
                alerts.append(t)
        return alerts
    except Exception as e:
        print(f"  EC alerts fetch failed: {e}")
        return []


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--community", required=True, help="Community ID, e.g. shingle-point")
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
    if cfg.get("tide_station_code"):
        print(f"Fetching tides ({cfg['tide_station_code']})…")
        tide = fetch_tide(cfg["tide_station_code"], cfg.get("tide_station_name", ""), now_utc)

    river_data = []
    for stn in cfg.get("hydrometric_stations", []):
        print(f"Fetching river level ({stn['station_id']})…")
        rd = fetch_river(stn["station_id"])
        if rd:
            river_data.append({**rd, "station_id": stn["station_id"], "river_name": stn.get("river_name", "")})

    marine = None
    if cfg.get("marine_zone_id"):
        print(f"Fetching marine forecast (zone {cfg['marine_zone_id']})…")
        marine = fetch_marine_forecast(cfg["marine_zone_id"])

    print("Fetching EC alerts…")
    alerts = fetch_ec_alerts()

    # ── Assemble data.json ──
    data = {
        "community": {
            "id": cid,
            "name": cfg.get("site_display_name", cfg.get("name", cid)),
            "name_alt": cfg.get("name_alt", ""),
            "lat": lat,
            "lon": lon,
            "tz": tz,
            "institution": cfg.get("institution_text", ""),
        },
        "updated_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weather": weather,
        "tide": tide,
        "rivers": river_data,
        "marine": marine,
        "alerts": {
            "weather": alerts,
        },
        "external_links": cfg.get("external_links", []),
    }

    data_path = out_dir / "data.json"
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"Wrote {data_path}")

    # ── Copy chart PNGs ──
    charts_src = COMMUNITIES_DIR / cid / "charts"
    if charts_src.exists():
        for png in charts_src.glob("*.png"):
            dst = img_dir / png.name
            shutil.copy2(png, dst)
            print(f"  copied {png.name}")
    else:
        print(f"  no charts/ folder found for {cid}")

    print("Done.")


if __name__ == "__main__":
    main()
