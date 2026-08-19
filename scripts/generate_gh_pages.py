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
            "hourly": "cloudcover,windspeed_10m,winddirection_10m,pressure_msl,temperature_2m",
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
            "uv_max": _r1(dget("uv_index_max", i)),
            "sunrise": dget("sunrise", i),
            "sunset": dget("sunset", i),
        })

    # 10-day daily temp arrays for chart
    daily_hi = [fc["hi"] for fc in forecast]
    daily_lo = [fc["lo"] for fc in forecast]
    daily_dates = [fc["date"] for fc in forecast]

    # Hourly (next 48h) for wind/pressure/cloud detail charts
    times_h = hourly.get("time", [])
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    start_i = next((i for i, t in enumerate(times_h) if t[:13] >= now_str), 0)
    def h_slice(key, n=48):
        vals = hourly.get(key, [])
        return [round(v, 1) if v is not None else None for v in vals[start_i:start_i+n]]

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
        "daily_dates": daily_dates,
        "hourly_wind": h_slice("windspeed_10m"),
        "hourly_wind_dir": h_slice("winddirection_10m"),
        "hourly_pressure": h_slice("pressure_msl"),
        "hourly_cloud": h_slice("cloudcover"),
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


# ── River level (MSC Datamart CSV) ───────────────────────────────────────

def fetch_river(station_id, provterr):
    """Fetch latest water level from the MSC Datamart hourly CSV — more stable
    than wateroffice.ec.gc.ca scraping endpoints."""
    prov = provterr.upper()
    url = (
        f"https://dd.weather.gc.ca/hydrometric/csv/{prov}/hourly/"
        f"{prov}_{station_id}_hourly_hydrometric.csv"
    )
    try:
        r = get_with_retry(url, timeout=30)
        lines = [l for l in r.text.strip().splitlines() if l.strip()]
        # Header: ID,Date,Water Level (m),Grade,Symbol,QA/QC,Discharge (cms),...
        header = [c.strip().strip('"') for c in lines[0].split(',')]
        level_idx = next(
            (i for i, h in enumerate(header) if "Water Level" in h or "Niveau" in h),
            2,
        )
        data_rows = []
        for line in lines[1:]:
            parts = line.split(',')
            if len(parts) > level_idx and parts[level_idx].strip():
                try:
                    m = float(parts[level_idx].strip().strip('"'))
                    ts = parts[1].strip().strip('"') if len(parts) > 1 else ""
                    data_rows.append({"t": ts, "m": round(m, 3)})
                except ValueError:
                    continue

        if not data_rows:
            print(f"  WSC: no valid rows for {station_id}")
            return None

        current_m = data_rows[-1]["m"]
        # Thin to ~48 points
        step = max(1, len(data_rows) // 48)
        series = data_rows[::step]

        return {"current_m": current_m, "series": series}

    except Exception as e:
        print(f"  WSC Datamart fetch failed for {station_id}: {e}")
        return None


# ── Marine forecast (EC RSS) ──────────────────────────────────────────────

def fetch_marine_forecast(zone_id):
    url = f"https://weather.gc.ca/rss/marine/{zone_id}_e.xml"
    try:
        r = get_with_retry(url, timeout=30,
                           headers={"User-Agent": "alfred-portal/1.0 (research dashboard)"})
        # Namespace-agnostic parse: strip namespace prefixes for simplicity
        text = r.text
        import re
        # Remove namespace declarations and prefixes
        text = re.sub(r' xmlns[^"]*"[^"]*"', '', text)
        text = re.sub(r'<(\w+):(\w+)', r'<\2', text)
        text = re.sub(r'</(\w+):(\w+)', r'</\2', text)

        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        entries = root.findall(".//entry")
        out = []
        for e in entries[:5]:
            title_el = e.find("title")
            summary_el = e.find("summary") or e.find("content") or e.find("description")
            if title_el is not None:
                title = (title_el.text or "").strip()
                # Skip "No watches or warnings" entries
                if "No watches" in title or not title:
                    continue
                summary = ""
                if summary_el is not None:
                    summary = (summary_el.text or "").strip()
                    # Strip HTML tags if present
                    summary = re.sub(r'<[^>]+>', '', summary)
                    summary = summary[:500]
                out.append({"title": title, "text": summary})
        return out or None
    except Exception as e:
        print(f"  Marine forecast fetch failed for zone {zone_id}: {e}")
        return None


# ── EC alerts ─────────────────────────────────────────────────────────────

def fetch_ec_alerts(prov="yt"):
    import re
    url = f"https://weather.gc.ca/rss/warning/{prov}_e.xml"
    try:
        r = get_with_retry(url, timeout=20,
                           headers={"User-Agent": "alfred-portal/1.0 (research dashboard)"})
        text = r.text
        text = re.sub(r' xmlns[^"]*"[^"]*"', '', text)
        text = re.sub(r'<(\w+):(\w+)', r'<\2', text)
        text = re.sub(r'</(\w+):(\w+)', r'</\2', text)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        alerts = []
        for e in root.findall(".//entry"):
            title_el = e.find("title")
            if title_el is not None:
                t = (title_el.text or "").strip()
                if t and "No watches" not in t:
                    alerts.append(t)
        return alerts[:5]
    except Exception as e:
        print(f"  EC alerts fetch failed: {e}")
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
    if cfg.get("tide_station_code"):
        print(f"Fetching tides ({cfg['tide_station_code']})…")
        tide = fetch_tide(cfg["tide_station_code"], cfg.get("tide_station_name", ""), now_utc)

    rivers = []
    for stn in cfg.get("hydrometric_stations", []):
        sid = stn["station_id"]
        prov = stn.get("provterr", "NT")
        print(f"Fetching river level ({sid})…")
        rd = fetch_river(sid, prov)
        if rd:
            rivers.append({
                **rd,
                "station_id": sid,
                "river_name": stn.get("river_name", ""),
                "heading": stn.get("heading", ""),
            })
        else:
            print(f"  River fetch failed for {sid}")

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
        },
        "updated_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "weather": weather,
        "tide": tide,
        "rivers": rivers,
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
