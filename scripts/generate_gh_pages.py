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
            "hourly": "cloudcover,windspeed_10m,winddirection_10m,pressure_msl,temperature_2m,precipitation",
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
        "hourly_pressure_10d": h_10d("pressure_msl"),
        "hourly_cloud_10d": h_10d("cloudcover"),
        "hourly_precip_10d": h_10d("precipitation", decimals=2),
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

    # Try observed (wlo) first — predicted (wlp) is not available at all stations
    points = None
    for ts_code in ("wlo", "wlp"):
        try:
            r = requests.get(
                f"https://api-iwls.dfo-mpo.gc.ca/api/v1/stations/{station_id}/data",
                params={
                    "time-series-code": ts_code,
                    "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                timeout=30,
            )
            if r.status_code == 200:
                pts = r.json()
                if pts:
                    print(f"  IWLS: using time-series-code={ts_code}")
                    points = pts
                    break
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


# ── River level (WSC — OGC real-time API then Datamart CSV fallback) ─────

def fetch_river(station_id, provterr):
    """Fetch latest water level. Tries OGC API first, falls back to
    MSC Datamart hourly CSV."""

    # ── Method 1: OGC real-time API (same source as dashboard_lib) ──
    try:
        r = get_with_retry(
            "https://api.weather.gc.ca/collections/hydrometric-realtime/items",
            params={"station_number": station_id, "limit": 200},
            timeout=30,
        )
        features = r.json().get("features", [])
        rows = []
        for f in features:
            props = f.get("properties", {})
            level = props.get("LEVEL")
            ts = props.get("DATETIME")
            if level is not None and ts:
                rows.append({"t": ts, "m": round(float(level), 3)})
        if rows:
            rows.sort(key=lambda x: x["t"])
            current_m = rows[-1]["m"]
            step = max(1, len(rows) // 48)
            print(f"  WSC OGC API: {len(rows)} rows for {station_id}, current={current_m}")
            return {"current_m": current_m, "series": rows[::step]}
        print(f"  WSC OGC API: 0 features for {station_id}")
    except Exception as e:
        print(f"  WSC OGC API failed for {station_id}: {e}")

    # ── Method 2: MSC Datamart hourly CSV ──
    prov = provterr.upper()
    url = (
        f"https://dd.weather.gc.ca/hydrometric/csv/{prov}/hourly/"
        f"{prov}_{station_id}_hourly_hydrometric.csv"
    )
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
            print(f"  WSC Datamart: no valid rows for {station_id}")
            return None
        current_m = rows[-1]["m"]
        step = max(1, len(rows) // 48)
        print(f"  WSC Datamart CSV: {len(rows)} rows, current={current_m}")
        return {"current_m": current_m, "series": rows[::step]}
    except Exception as e:
        print(f"  WSC Datamart failed for {station_id}: {e}")
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
            s = re.sub(r'<(?:br|BR)\s*/?>', '\n', s)
            s = re.sub(r'<[^>]+>', ' ', s)
            s = re.sub(r'\n{3,}', '\n\n', s)
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
                    # Crop to exact 4:1 aspect ratio so every browser sees the same geographic crop
                    try:
                        from PIL import Image as _Img
                        import io as _io
                        img = _Img.open(_io.BytesIO(r.content))
                        iw, ih = img.size
                        target_h = iw // 4
                        if ih > target_h:
                            top = (ih - target_h) // 2
                            img = img.crop((0, top, iw, top + target_h))
                        buf = _io.BytesIO()
                        img.save(buf, "JPEG", quality=88)
                        out_path.write_bytes(buf.getvalue())
                    except Exception as crop_err:
                        print(f"  MODIS crop failed ({crop_err}), saving raw")
                        out_path.write_bytes(r.content)
                    print(f"  MODIS banner: {d} → {out_path.name} ({out_path.stat().st_size//1024} kB)")
                    return True
            except Exception as e:
                print(f"  MODIS banner day -{delta} failed: {e}")
        return False

    banner_path = img_dir / "modis_banner.jpg"
    if not fetch_modis_banner(lat, lon, banner_path):
        print("  MODIS banner unavailable")

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
