"""
generate_dashboards.py — updates every community's Notion dashboard.

For each community in communities/:
  1. Reads config.json to get notion_page_id, lat/lon, and optional
     site-specific parameters (tide station, marine zone, MODIS bbox, etc.)
  2. Fetches data and builds Notion blocks using dashboard_lib
  3. Publishes to Notion (clears old content, writes new content)

The "blocks" list in each community's config.json controls which sections
appear. Removing a block name = that section disappears from the dashboard.

Available block names (map to lib.build_*_section calls):
  weather       Current conditions, weather icons, mini forecast strip, wind chart
  marine        Environment Canada marine zone forecast
  alerts        Active weather alerts
  forecast      7-day land forecast strip
  water_level   Copernicus total water level time series
  temperature   30-year temperature history + TDD histogram
  wind_chart    Wind rose / vector chart
  modis         MODIS Terra true-color satellite image
  sentinel1     Sentinel-1 SAR image
  hydrometric   Hydrometric river/lake station water level

Usage:
  python scripts/generate_dashboards.py

Requires:
  NOTION_TOKEN env var (set once as GitHub secret)
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# ---- Ensure dashboard_lib (vendored at repo root) is importable ----
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

try:
    import dashboard_lib as lib
except ImportError as e:
    print(f"ERROR: dashboard_lib not found at {REPO_ROOT}: {e}")
    sys.exit(1)

COMMUNITIES_DIR = os.path.join(REPO_ROOT, "communities")
CACHE_DIR       = os.path.join(REPO_ROOT, "cache")


def load_communities():
    communities = []
    for name in sorted(os.listdir(COMMUNITIES_DIR)):
        cfg_path = os.path.join(COMMUNITIES_DIR, name, "config.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path) as f:
                communities.append(json.load(f))
    return communities


def update_community(community, now_utc):
    sid     = community["id"]
    page_id = community["notion_page_id"]
    enabled = set(community.get("blocks", []))

    # Point lib at this community's Notion page
    lib.PAGE_ID = page_id

    lat     = community["lat"]
    lon     = community["lon"]
    tz_name = community.get("tz_name", "UTC")

    print(f"\n[{sid}] Updating page {page_id}  blocks={sorted(enabled)}")

    now_local = lib.to_local_time(now_utc, tz_name)

    # Per-community cache so sites don't overwrite each other
    os.makedirs(CACHE_DIR, exist_ok=True)
    lib.CACHE_FILE_PATH = os.path.join(CACHE_DIR, f"daily_temps_{sid}.json")

    temp_cache       = lib.load_temp_cache()
    temp_cache_dirty = False

    # ---- Weather + wind (needed by "weather" block) ----
    weather              = None
    weather_icon_block   = None
    wind_icon_block      = None
    mini_forecast_strip_block  = None
    wind_forecast_chart_block  = None
    large_forecast_strip_bytes = None
    land_forecast_caption      = ""

    if "weather" in enabled:
        weather = lib.get_weather(lat, lon)
        if weather["status"] == "ok":
            compass        = lib.degrees_to_compass(weather["winddirection_deg"])
            wind_dir_text  = f"{compass} ({weather['winddirection_deg']:.0f}°)" if compass else "—"
            weather_text   = [
                ("Air temperature: ", f"{weather['temperature_c']} °C"),
                ("Humidity: ",        f"{weather['humidity_pct']} %"),
                ("Pressure: ",        f"{weather['pressure_hpa']} hPa"),
            ]
            weather_source_text = "Source: Open-Meteo (ERA5-based forecast/analysis)"
            wind_now_text  = [
                ("Wind speed: ",     f"{weather['windspeed_kmh']} km/h"),
                ("Wind direction: ", wind_dir_text),
            ]
            wind_source_text = "Source: Open-Meteo (ERA5-based forecast/analysis)"

            _wind_color, _beaufort_label = lib.windspeed_to_beaufort_color(weather.get("windspeed_kmh"))
            wind_now_text.append(("Beaufort force: ", _beaufort_label))

            weather_icon_bytes = lib.render_weather_icon(weather.get("weathercode"))
            wind_icon_bytes    = (
                lib.render_wind_icon(weather["winddirection_deg"], weather["windspeed_kmh"])
                if weather.get("winddirection_deg") is not None and weather.get("windspeed_kmh") is not None
                else None
            )
            weather_icon_big = (
                lib.render_icon_with_big_number(
                    weather_icon_bytes, f"{weather['temperature_c']:.0f}", "°C",
                    number_color=lib.temperature_to_color(weather["temperature_c"]),
                )
                if weather_icon_bytes and weather.get("temperature_c") is not None
                else weather_icon_bytes
            )
            wind_icon_big = (
                lib.render_icon_with_big_number(
                    wind_icon_bytes, f"{weather['windspeed_kmh']:.0f}", "km/h",
                    number_color=_wind_color,
                )
                if wind_icon_bytes and weather.get("windspeed_kmh") is not None
                else wind_icon_bytes
            )

            for blob, name, dest_attr in [
                (weather_icon_big, "weather_icon.png", "weather_icon_block"),
                (wind_icon_big,    "wind_icon.png",    "wind_icon_block"),
            ]:
                if blob:
                    try:
                        uid   = lib.upload_image_to_notion(blob, name)
                        block = lib.image_block_from_upload(uid)
                        if dest_attr == "weather_icon_block":
                            weather_icon_block = block
                        else:
                            wind_icon_block = block
                    except Exception as e:
                        print(f"[{sid}] {name} upload failed: {e}")

            land_forecast_days = lib.get_land_forecast(lat, lon)
            if land_forecast_days:
                mini_strip_days = []
                for d in land_forecast_days:
                    day_compass = lib.degrees_to_compass(d["wind_dir_deg"])
                    wind_label  = f"{d['wind_max_kmh']:.0f} km/h {day_compass or ''}".strip()
                    precip_label = f"{d['precip_mm']:.1f} mm" + (
                        f" ({d['precip_prob_pct']:.0f}%)" if d.get("precip_prob_pct") is not None else ""
                    )
                    mini_strip_days.append({
                        "day_label":   datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a"),
                        "weathercode": d["weathercode"],
                        "temp_min":    d["temp_min"],
                        "temp_max":    d["temp_max"],
                        "wind_label":  wind_label,
                        "precip_label": precip_label,
                    })
                mini_forecast_strip_bytes = lib.build_mini_forecast_strip(mini_strip_days)
                large_forecast_strip_bytes = lib.build_large_forecast_strip(mini_strip_days)
                land_forecast_caption = "Source: Open-Meteo"
                if mini_forecast_strip_bytes:
                    try:
                        uid = lib.upload_image_to_notion(mini_forecast_strip_bytes, "mini_forecast_strip.png")
                        mini_forecast_strip_block = lib.image_block_from_upload(uid)
                    except Exception as e:
                        print(f"[{sid}] mini forecast strip upload failed: {e}")

            wind_forecast_chart_bytes, wind_forecast_chart_caption = lib.build_wind_forecast_mini_chart(
                weather.get("hourly_wind_forecast")
            )
            if wind_forecast_chart_bytes:
                try:
                    uid = lib.upload_image_to_notion(wind_forecast_chart_bytes, "wind_forecast_chart.png")
                    wind_forecast_chart_block = lib.image_block_from_upload(uid)
                except Exception as e:
                    print(f"[{sid}] wind forecast chart upload failed: {e}")

        else:
            weather_text        = "Weather data unavailable — fetch failed. Check Action logs."
            weather_source_text = ""
            wind_now_text       = "Wind data unavailable — fetch failed. Check Action logs."
            wind_source_text    = ""

    # ---- Sun info (used inside weather block) ----
    sun_info         = None
    sun_text         = None
    sun_chart_bytes  = None
    sun_chart_caption = ""
    if "weather" in enabled:
        sun_info          = lib.get_sun_info(lat, lon)
        sun_text          = lib.classify_sun_text(sun_info, lat, lon, now_utc, tz_name)
        sun_chart_bytes, sun_chart_caption = lib.build_sun_curve_chart(lat, lon, now_utc, now_local, tz_name)

    # ---- Tides (used inside weather block) ----
    tide_text        = "No tide station configured."
    tide_chart_bytes = None
    tide_chart_caption = ""
    tide_station_code = community.get("tide_station_code")
    tide_station_name = community.get("tide_station_name", "")
    if "weather" in enabled and tide_station_code:
        station_id = lib.find_iwls_station_id(tide_station_code)
        tide_points = lib.fetch_tide_predictions(station_id, now_utc, hours_ahead=24*7) if station_id else None
        tide_text   = lib.format_tide_text(tide_points, now_utc, tide_station_code, tide_station_name)
        tide_chart_bytes, tide_chart_caption = lib.build_tide_chart(tide_points, now_utc, tz_name)

    # ---- Marine forecast ----
    marine_text         = None
    marine_source_text  = ""
    marine_zone_id      = community.get("marine_zone_id")
    marine_zone_name    = community.get("marine_zone_name", "")
    if "marine" in enabled and marine_zone_id:
        marine_entries = lib.get_marine_forecast(marine_zone_id)
        marine_text, marine_source_text = lib.format_marine_forecast_text(marine_entries, marine_zone_name)

    # ---- Weather alerts ----
    active_alerts = []
    if "alerts" in enabled:
        weather_alert_entries = lib.get_weather_alerts(lat, lon)
        active_alerts = lib.filter_active_alerts(weather_alert_entries)
        print(f"[{sid}] ALERTS: {len(active_alerts)} active")

    # ---- Temperature chart + TDD (historical) ----
    temp_chart_bytes  = None
    temp_chart_caption = ""
    tdd_histogram_bytes = None
    tdd_histogram_caption = ""
    if "temperature" in enabled:
        print(f"[{sid}] STARTING: temperature chart")
        temp_chart_bytes, temp_chart_caption = lib.build_temperature_chart(lat, lon, now_utc, temp_cache)
        print(f"[{sid}] STARTING: TDD histogram")
        tdd_histogram_bytes, tdd_histogram_caption = lib.build_tdd_histogram(lat, lon, now_utc, temp_cache)
        temp_cache_dirty = True

    # ---- Wind vector chart ----
    wind_chart_bytes  = None
    wind_chart_caption = ""
    if "wind_chart" in enabled:
        wind_chart_bytes, wind_chart_caption = lib.build_wind_vector_chart(lat, lon, now_utc)

    # ---- Logo ----
    logo_url       = community.get("logo_url", "")
    logo_png_bytes = lib.fetch_and_convert_logo_to_png(logo_url) if logo_url else None

    # ---- MODIS + water level + Sentinel-1 (parallel) ----
    modis_bytes       = None
    modis_date        = None
    modis_block_obj   = None
    copernicus_times  = None
    copernicus_values = None
    copernicus_yearly_mean = None
    sentinel1_bytes   = None
    sentinel1_caption = "Sentinel-1 SAR image unavailable."
    hydrometric_results = []

    needs_parallel = enabled & {"modis", "water_level", "sentinel1", "hydrometric"}
    if needs_parallel:
        hydrometric_stations = community.get("hydrometric_stations", [])
        workers = len(needs_parallel) + len(hydrometric_stations)
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
            fut_modis = None
            fut_wl    = None
            fut_s1    = None

            if "modis" in enabled:
                modis_center_x = community.get("modis_center_x")
                modis_center_y = community.get("modis_center_y")
                if modis_center_x is not None:
                    modis_bbox = community.get("modis_bbox_3413")
                    if not modis_bbox:
                        half = 150_000 * getattr(lib, "MODIS_OVERSIZE_FACTOR", 1.15)
                        modis_bbox = f"{modis_center_x-half:.0f},{modis_center_y-half:.0f},{modis_center_x+half:.0f},{modis_center_y+half:.0f}"
                    fut_modis = ex.submit(
                        lib.fetch_and_process_modis,
                        bbox_3413=modis_bbox,
                        center_x=modis_center_x, center_y=modis_center_y,
                        rotation_deg=community.get("modis_rotation_deg", 0.0),
                        points=community.get("map_points", []),
                        now_utc=now_utc, tz_name=tz_name,
                        reference_lines=community.get("map_reference_lines", []),
                    )

            if "water_level" in enabled:
                fut_wl = ex.submit(
                    lib.fetch_copernicus_water_level,
                    lat=lat, lon=lon, now_utc=now_utc,
                    site_label=community.get("site_display_name", community["name"]),
                    yearly_mean=community.get("water_level_yearly_mean", 0.0),
                )

            if "sentinel1" in enabled:
                utm_zone   = community.get("utm_zone")
                utm_epsg   = community.get("utm_epsg")
                utm_center_x = community.get("utm_center_x")
                utm_center_y = community.get("utm_center_y")
                if utm_zone and utm_center_x is not None:
                    fut_s1 = ex.submit(
                        lib.fetch_and_process_sentinel1,
                        lat=lat, lon=lon,
                        site_label=community.get("site_display_name", community["name"]),
                        utm_zone=utm_zone, utm_epsg=utm_epsg,
                        center_x=utm_center_x, center_y=utm_center_y,
                        points=community.get("map_points", []),
                        tz_name=tz_name,
                        reference_lines=community.get("map_reference_lines", []),
                        coastline_geojson_path=community.get("coastline_geojson_path", "coastline_data.geojson"),
                        now_utc=now_utc,
                    )

            hydrometric_futures = [
                (st, ex.submit(lib.fetch_hydrometric_water_level, st["station_id"], st["provterr"]))
                for st in hydrometric_stations
            ]

            if fut_modis:
                try:
                    modis_bytes, modis_date = fut_modis.result()
                except Exception as e:
                    print(f"[{sid}] MODIS FAILED: {e}")

            if fut_wl:
                try:
                    copernicus_times, copernicus_values, copernicus_yearly_mean = fut_wl.result()
                except Exception as e:
                    print(f"[{sid}] WATER LEVEL FAILED: {e}")

            if fut_s1:
                try:
                    sentinel1_bytes, sentinel1_caption = fut_s1.result()
                except Exception as e:
                    print(f"[{sid}] SENTINEL-1 FAILED: {e}")

            for st, fut in hydrometric_futures:
                try:
                    h_times, h_values = fut.result()
                except Exception as e:
                    print(f"[{sid}] HYDROMETRIC[{st['station_id']}] FAILED: {e}")
                    h_times, h_values = None, None
                hydrometric_results.append((st, h_times, h_values))

        if modis_bytes:
            modis_block_obj, _ = lib._upload_chart_or_caption(modis_bytes, "modis.png", None)

    # ---- ASSEMBLE PAGE ----
    blocks = []

    institution_text = community.get(
        "institution_text",
        "This dashboard is provided by the Alfred Wegener Institute Helmholtz Centre for Polar and Marine Research.",
    )
    blocks += lib.build_header_blocks(
        now_local, logo_url=logo_url, logo_png_bytes=logo_png_bytes,
        institution_text=institution_text,
    )

    if "weather" in enabled and weather is not None:
        blocks += lib.build_todays_conditions_section(
            weather_text, weather_source_text, weather_icon_block, mini_forecast_strip_block,
            lat, lon, wind_now_text, wind_source_text, wind_icon_block, wind_forecast_chart_block,
            tide_text, tide_chart_bytes, tide_chart_caption, tide_station_code or "",
            sun_text, sun_chart_bytes, sun_chart_caption,
        )

    if "alerts" in enabled:
        blocks += lib.build_active_alerts_section(active_alerts)

    if "forecast" in enabled and large_forecast_strip_bytes is not None:
        blocks += lib.build_land_forecast_section(large_forecast_strip_bytes, land_forecast_caption)

    if "marine" in enabled and marine_text is not None:
        blocks += lib.build_marine_forecast_section(
            marine_text, marine_source_text,
            marine_zone_name, marine_zone_id,
        )

    if "water_level" in enabled:
        wl_text = (
            [("Latest forecast value: ", f"{copernicus_values[0]:.2f} m")]
            if copernicus_values
            else "Total water level forecast unavailable — fetch failed."
        )
        wl_chart_bytes, wl_chart_caption = lib.build_water_level_chart(
            copernicus_times, copernicus_values, tz_name, copernicus_yearly_mean,
        )
        blocks += lib.build_total_water_level_section(wl_text, wl_chart_bytes, wl_chart_caption)

    if "hydrometric" in enabled:
        for st, h_times, h_values in hydrometric_results:
            h_chart_bytes, h_chart_caption = lib.build_hydrometric_chart(
                h_times, h_values, st["station_id"], st["river_name"]
            )
            blocks += lib.build_hydrometric_section(h_chart_bytes, h_chart_caption, st["heading"])

    if "modis" in enabled:
        modis_caption_str = (
            f"NASA MODIS Terra, true color, {modis_date}." if modis_date else "MODIS image unavailable."
        )
        blocks += lib.build_modis_section(
            modis_block_obj, modis_caption_str, modis_date, now_utc,
            community.get("modis_bbox_3413", ""),
            community.get("site_display_name", community["name"]),
        )

    if "sentinel1" in enabled:
        blocks += lib.build_sentinel1_section(
            sentinel1_bytes, sentinel1_caption, None,
            community.get("site_display_name", community["name"]),
        )

    if "temperature" in enabled:
        blocks += lib.build_temperature_chart_section(temp_chart_bytes, temp_chart_caption)
        blocks += lib.build_tdd_histogram_section(tdd_histogram_bytes, tdd_histogram_caption)

    if "wind_chart" in enabled:
        blocks += lib.build_wind_chart_section(wind_chart_bytes, wind_chart_caption)

    blocks += lib.build_disclaimer_section()

    lib.publish_blocks_to_notion(blocks)
    print(f"[{sid}] Done — {len(blocks)} blocks written")

    if temp_cache_dirty:
        lib.save_temp_cache(temp_cache)
        print(f"[{sid}] CACHE: saved {len(temp_cache)} years")


def main():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("ERROR: NOTION_TOKEN env var not set")
        sys.exit(1)

    communities = load_communities()
    print(f"Found {len(communities)} communities")

    now_utc = datetime.now(timezone.utc)

    for community in communities:
        try:
            update_community(community, now_utc)
        except Exception as e:
            import traceback
            print(f"ERROR [{community['id']}]: {e}")
            traceback.print_exc()

    print("\nAll communities updated.")


if __name__ == "__main__":
    main()
