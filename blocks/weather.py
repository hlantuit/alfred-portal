"""
weather block — current conditions from Open-Meteo ECMWF IFS 0.25°.

render(community) -> list of Notion blocks
"""

from datetime import datetime, timezone
import math

try:
    import requests
except ImportError:
    raise ImportError("requests not installed — run: pip install requests")

WMO_LABELS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + hail",
}

COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
           "S","SSW","SW","WSW","W","WNW","NW","NNW"]


def _compass(deg):
    if deg is None:
        return "—"
    return COMPASS[round(deg / 22.5) % 16]


def _fetch(lat, lon):
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,wind_u_component_10m,wind_v_component_10m,weather_code",
            "models": "ecmwf_ifs025",
            "timezone": "UTC",
            "start_date": now_iso,
            "end_date": now_iso,
        },
        timeout=20,
    )
    r.raise_for_status()
    data   = r.json()
    now_h  = datetime.now(timezone.utc).hour
    hourly = data["hourly"]
    idx    = min(range(len(hourly["time"])), key=lambda i: abs(i - now_h))
    u_ms   = hourly["wind_u_component_10m"][idx] or 0.0
    v_ms   = hourly["wind_v_component_10m"][idx] or 0.0
    spd_ms = math.sqrt(u_ms**2 + v_ms**2)
    wind_dir = (math.degrees(math.atan2(-u_ms, -v_ms)) + 360) % 360 if spd_ms > 0 else None
    return {
        "temp_c":   hourly["temperature_2m"][idx],
        "wind_kmh": round(spd_ms * 3.6, 1),
        "wind_dir": round(wind_dir, 1) if wind_dir is not None else None,
        "wmo_code": hourly["weather_code"][idx],
    }


def _txt(content, bold=False, color="default"):
    ann = {"bold": bold, "color": color}
    return {"type": "text", "text": {"content": content}, "annotations": ann}


def _row(label, value):
    return {
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": [
            _txt(f"{label}: ", bold=False, color="gray"),
            _txt(value, bold=True),
        ]}
    }


def render(community):
    lat, lon = community["lat"], community["lon"]
    try:
        w = _fetch(lat, lon)
    except Exception as e:
        return [{
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [_txt(f"Weather data unavailable: {e}")],
                "icon": {"emoji": "⚠️"},
                "color": "yellow_background",
            }
        }]

    temp_str  = f"{w['temp_c']:.1f} °C" if w["temp_c"]  is not None else "—"
    wind_str  = (f"{w['wind_kmh']:.0f} km/h {_compass(w['wind_dir'])}"
                 if w["wind_kmh"] is not None else "—")
    cond_str  = WMO_LABELS.get(w["wmo_code"], f"Code {w['wmo_code']}") if w["wmo_code"] is not None else "—"

    return [
        {
            "object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [_txt("Current Weather")]}
        },
        _row("Conditions",   cond_str),
        _row("Temperature",  temp_str),
        _row("Wind",         wind_str),
        {"object": "block", "type": "divider", "divider": {}},
    ]
