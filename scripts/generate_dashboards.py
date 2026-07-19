"""
generate_dashboards.py — updates every community's Notion dashboard.

The "blocks" list in each community's config.json controls which sections
appear. Available block names:

  weather         Current conditions card (weather + wind + tide + sun)
  gem_forecast    GEM/GDPS 10-day forecast strip + temperature/wind/precip charts
  marine          Environment Canada marine zone forecast
  alerts          Active weather alerts
  water_level     GDSPS total water level forecast
  wave_forecast   Wave height/period forecast
  temperature     30-year temperature history chart + TDD histogram
  wind_chart      Historical wind rose / vector chart
  modis           MODIS Terra true-color satellite image
  sentinel1       Sentinel-1 SAR image (grayscale)
  sea_ice         Sentinel-1 sea ice classification (150 km frame)
  sea_ice_zoom    Sentinel-1 sea ice classification (50 km frame)
  lake_river_ice  Sentinel-1 lake/river ice classification zoom
  hydrometric     Hydrometric river/lake station water level
  wildfire        CWFIS wildfire hotspot map
  snow_depth      Snow depth time series

Usage:
  python scripts/generate_dashboards.py

Requires:
  NOTION_TOKEN env var (set once as GitHub secret)
  SENTINEL_HUB_CLIENT_ID / SENTINEL_HUB_CLIENT_SECRET (for ice/SAR blocks)
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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

    lib.PAGE_ID = page_id

    lat      = community["lat"]
    lon      = community["lon"]
    tz_name  = community.get("tz_name", "UTC")
    site_label = community.get("site_display_name", community["name"])

    print(f"\n[{sid}] Updating page {page_id}  blocks={sorted(enabled)}")

    now_local = lib.to_local_time(now_utc, tz_name)

    # Per-community temperature cache
    os.makedirs(CACHE_DIR, exist_ok=True)
    lib.CACHE_FILE_PATH = os.path.join(CACHE_DIR, f"daily_temps_{sid}.json")
    temp_cache       = lib.load_temp_cache()
    temp_cache_dirty = False

    # ------------------------------------------------------------------ #
    # GEM forecast (weather + 10-day strip + charts)                      #
    # ------------------------------------------------------------------ #
    gem_forecast = None
    if "gem_forecast" in enabled or "weather" in enabled:
        gem_forecast = lib.fetch_gem_forecast(lat, lon, now_utc, tz_name)

    # ------------------------------------------------------------------ #
    # Current weather conditions (for the Today's Conditions card)        #
    # ------------------------------------------------------------------ #
    weather = None
    weather_text = weather_source_text = ""
    wind_now_text = wind_source_text = ""
    weather_icon_block = wind_icon_block = None
    mini_forecast_strip_block = wind_forecast_chart_block = None

    if "weather" in enabled:
        weather = lib.get_weather(lat, lon)
        if weather["status"] == "ok":
            compass       = lib.degrees_to_compass(weather["winddirection_deg"])
            wind_dir_text = f"{compass} ({weather['winddirection_deg']:.0f}°)" if compass else "—"
            weather_text  = [
                ("Air temperature: ", f"{weather['temperature_c']} °C"),
                ("Humidity: ",        f"{weather['humidity_pct']} %"),
                ("Pressure: ",        f"{weather['pressure_hpa']} hPa"),
            ]
            weather_source_text = "Source: Open-Meteo (ERA5-based forecast/analysis)"
            _wind_color, _beaufort = lib.windspeed_to_beaufort_color(weather.get("windspeed_kmh"))
            wind_now_text = [
                ("Wind speed: ",     f"{weather['windspeed_kmh']} km/h"),
                ("Wind direction: ", wind_dir_text),
                ("Beaufort force: ", _beaufort),
            ]
            wind_source_text = "Source: Open-Meteo (ERA5-based forecast/analysis)"

            wx_icon  = lib.render_weather_icon(weather.get("weathercode"))
            wnd_icon = (
                lib.render_wind_icon(weather["winddirection_deg"], weather["windspeed_kmh"])
                if weather.get("winddirection_deg") is not None else None
            )
            wx_big = (
                lib.render_icon_with_big_number(
                    wx_icon, f"{weather['temperature_c']:.0f}", "°C",
                    number_color=lib.temperature_to_color(weather["temperature_c"]),
                ) if wx_icon and weather.get("temperature_c") is not None else wx_icon
            )
            wnd_big = (
                lib.render_icon_with_big_number(
                    wnd_icon, f"{weather['windspeed_kmh']:.0f}", "km/h",
                    number_color=_wind_color,
                ) if wnd_icon and weather.get("windspeed_kmh") is not None else wnd_icon
            )
            for blob, name, attr in [
                (wx_big,  "weather_icon.png", "weather_icon_block"),
                (wnd_big, "wind_icon.png",    "wind_icon_block"),
            ]:
                if blob:
                    try:
                        uid = lib.upload_image_to_notion(blob, name)
                        blk = lib.image_block_from_upload(uid)
                        if attr == "weather_icon_block":
                            weather_icon_block = blk
                        else:
                            wind_icon_block = blk
                    except Exception as e:
                        print(f"[{sid}] {name} upload failed: {e}")

            # Mini forecast strip from GEM hourly
            if gem_forecast:
                hourly = gem_forecast.get("hourly", {})
                wind_fc = lib.gem_hourly_wind_forecast(hourly, now_utc, tz_name)
                wind_forecast_chart_block_bytes, _ = lib.build_wind_forecast_mini_chart(wind_fc)
                if wind_forecast_chart_block_bytes:
                    try:
                        uid = lib.upload_image_to_notion(wind_forecast_chart_block_bytes, "wind_forecast_chart.png")
                        wind_forecast_chart_block = lib.image_block_from_upload(uid)
                    except Exception as e:
                        print(f"[{sid}] wind forecast chart upload failed: {e}")

                daily = gem_forecast.get("daily", {})
                gem_days = lib.gem_daily_to_land_forecast_days(daily)
                mini_strip_bytes = lib.build_mini_forecast_strip(gem_days) if gem_days else None
                if mini_strip_bytes:
                    try:
                        uid = lib.upload_image_to_notion(mini_strip_bytes, "mini_forecast_strip.png")
                        mini_forecast_strip_block = lib.image_block_from_upload(uid)
                    except Exception as e:
                        print(f"[{sid}] mini forecast strip upload failed: {e}")

        else:
            weather_text  = "Weather data unavailable — fetch failed."
            wind_now_text = "Wind data unavailable — fetch failed."

    # ------------------------------------------------------------------ #
    # Sun + tides (both used in Today's Conditions card)                  #
    # ------------------------------------------------------------------ #
    sun_info = sun_text = sun_chart_bytes = sun_chart_caption = None
    if "weather" in enabled:
        sun_info = lib.get_sun_info(lat, lon)
        sun_text = lib.classify_sun_text(sun_info, lat, lon, now_utc, tz_name)
        sun_chart_bytes, sun_chart_caption = lib.build_sun_curve_chart(lat, lon, now_utc, now_local, tz_name)

    tide_text = tide_chart_bytes = tide_chart_caption = None
    tide_station_code = community.get("tide_station_code")
    tide_station_name = community.get("tide_station_name", "")
    if "weather" in enabled and tide_station_code:
        station_id  = lib.find_iwls_station_id(tide_station_code)
        tide_points = lib.fetch_tide_predictions(station_id, now_utc, hours_ahead=24*7) if station_id else None
        tide_text   = lib.format_tide_text(tide_points, now_utc, tide_station_code, tide_station_name)
        tide_chart_bytes, tide_chart_caption = lib.build_tide_chart(tide_points, now_utc, tz_name)

    # ------------------------------------------------------------------ #
    # Marine forecast                                                      #
    # ------------------------------------------------------------------ #
    marine_text = marine_source_text = None
    marine_zone_id   = community.get("marine_zone_id")
    marine_zone_name = community.get("marine_zone_name", "")
    if "marine" in enabled and marine_zone_id:
        entries = lib.get_marine_forecast(marine_zone_id)
        marine_text, marine_source_text = lib.format_marine_forecast_text(entries, marine_zone_name)

    # ------------------------------------------------------------------ #
    # Weather alerts                                                       #
    # ------------------------------------------------------------------ #
    active_alerts = []
    if "alerts" in enabled:
        active_alerts = lib.filter_active_alerts(lib.get_weather_alerts(lat, lon))
        print(f"[{sid}] ALERTS: {len(active_alerts)} active")

    # ------------------------------------------------------------------ #
    # Temperature chart + TDD histogram                                   #
    # ------------------------------------------------------------------ #
    temp_chart_bytes = temp_chart_caption = None
    tdd_bytes = tdd_caption = None
    if "temperature" in enabled:
        print(f"[{sid}] STARTING: temperature chart")
        temp_chart_bytes, temp_chart_caption = lib.build_temperature_chart(lat, lon, now_utc, temp_cache)
        print(f"[{sid}] STARTING: TDD histogram")
        tdd_bytes, tdd_caption = lib.build_tdd_histogram(lat, lon, now_utc, temp_cache)
        temp_cache_dirty = True

    # ------------------------------------------------------------------ #
    # Wind vector chart                                                    #
    # ------------------------------------------------------------------ #
    wind_chart_bytes = wind_chart_caption = None
    if "wind_chart" in enabled:
        wind_chart_bytes, _, wind_chart_caption = lib.build_wind_charts_combined(lat, lon, now_utc)

    # ------------------------------------------------------------------ #
    # Logo                                                                 #
    # ------------------------------------------------------------------ #
    logo_url       = community.get("logo_url", "")
    logo_png_bytes = lib.fetch_and_convert_logo_to_png(logo_url) if logo_url else None

    # ------------------------------------------------------------------ #
    # Parallel fetches: MODIS, water level, Sentinel-1, sea ice, wave,    #
    # wildfire, hydrometric                                                #
    # ------------------------------------------------------------------ #
    modis_bytes = modis_date = None
    modis_block_obj = None
    gdsps_times = gdsps_values = gdsps_yearly_mean = None
    sentinel1_bytes = None
    sentinel1_caption = "Sentinel-1 SAR image unavailable."
    sea_ice_bytes = sea_ice_caption = None
    sea_ice_zoom_bytes = sea_ice_zoom_caption = None
    lake_ice_bytes = lake_ice_caption = None
    wave_data = None
    fires = []
    hydrometric_results = []

    needs_parallel = enabled & {
        "modis", "water_level", "sentinel1", "sea_ice", "sea_ice_zoom",
        "lake_river_ice", "wave_forecast", "wildfire", "hydrometric",
    }
    if needs_parallel:
        utm_zone   = community.get("utm_zone")
        utm_epsg   = community.get("utm_epsg")
        utm_center_x = community.get("utm_center_x")
        utm_center_y = community.get("utm_center_y")
        modis_cx   = community.get("modis_center_x")
        modis_cy   = community.get("modis_center_y")
        _coastline_rel = community.get("coastline_geojson_path")
        coastline = (
            os.path.join(COMMUNITIES_DIR, community["id"], _coastline_rel)
            if _coastline_rel else None
        )
        water_bod  = community.get("water_bodies_geojson_path")
        map_pts    = community.get("map_points", [])
        ref_lines  = community.get("map_reference_lines", [])
        hydro_stations = community.get("hydrometric_stations", [])

        workers = max(len(needs_parallel) + len(hydro_stations), 1)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_modis = fut_wl = fut_s1 = None
            fut_ice = fut_ice_zoom = fut_lake_ice = None
            fut_wave = fut_fire = None

            if "modis" in enabled and modis_cx is not None:
                bbox = community.get("modis_bbox_3413")
                if not bbox:
                    h = 150_000 * getattr(lib, "MODIS_OVERSIZE_FACTOR", 1.2)
                    bbox = f"{modis_cx-h:.0f},{modis_cy-h:.0f},{modis_cx+h:.0f},{modis_cy+h:.0f}"
                fut_modis = ex.submit(
                    lib.fetch_and_process_modis,
                    bbox_3413=bbox, center_x=modis_cx, center_y=modis_cy,
                    rotation_deg=community.get("modis_rotation_deg", 0.0),
                    points=map_pts, now_utc=now_utc, tz_name=tz_name,
                    reference_lines=ref_lines,
                )

            if "water_level" in enabled:
                fut_wl = ex.submit(
                    lib.fetch_gdsps_water_level,
                    lat=lat, lon=lon, now_utc=now_utc, site_label=site_label,
                    yearly_mean=community.get("water_level_yearly_mean", 0.0),
                )

            if "sentinel1" in enabled and utm_zone and utm_center_x is not None:
                fut_s1 = ex.submit(
                    lib.fetch_and_process_sentinel1,
                    lat=lat, lon=lon, site_label=site_label,
                    utm_zone=utm_zone, utm_epsg=utm_epsg,
                    center_x=utm_center_x, center_y=utm_center_y,
                    points=map_pts, tz_name=tz_name, reference_lines=ref_lines,
                    coastline_geojson_path=coastline, now_utc=now_utc,
                )

            if "sea_ice" in enabled and utm_zone and utm_center_x is not None:
                fut_ice = ex.submit(
                    lib.fetch_and_process_sentinel1_ice,
                    lat=lat, lon=lon, site_label=site_label,
                    utm_zone=utm_zone, utm_epsg=utm_epsg,
                    center_x=utm_center_x, center_y=utm_center_y,
                    points=map_pts, tz_name=tz_name, half_width_m=150_000,
                    reference_lines=ref_lines, coastline_geojson_path=coastline,
                    now_utc=now_utc,
                )

            if "sea_ice_zoom" in enabled and utm_zone and utm_center_x is not None:
                fut_ice_zoom = ex.submit(
                    lib.fetch_and_process_sentinel1_ice,
                    lat=lat, lon=lon, site_label=site_label,
                    utm_zone=utm_zone, utm_epsg=utm_epsg,
                    center_x=utm_center_x, center_y=utm_center_y,
                    points=map_pts, tz_name=tz_name, half_width_m=50_000,
                    reference_lines=ref_lines, coastline_geojson_path=coastline,
                    now_utc=now_utc,
                )

            if "lake_river_ice" in enabled and utm_zone and utm_center_x is not None:
                fut_lake_ice = ex.submit(
                    lib.fetch_and_process_sentinel1_lake_ice,
                    lat=lat, lon=lon, site_label=site_label,
                    utm_zone=utm_zone, utm_epsg=utm_epsg,
                    center_x=utm_center_x, center_y=utm_center_y,
                    points=map_pts, tz_name=tz_name, half_width_m=50_000,
                    reference_lines=ref_lines,
                    water_bodies_geojson_path=water_bod, now_utc=now_utc,
                )

            if "wave_forecast" in enabled:
                fut_wave = ex.submit(lib.fetch_wave_forecast, lat, lon, now_utc)

            if "wildfire" in enabled:
                fut_fire = ex.submit(lib.fetch_cwfis_wildfires, lat, lon, 600, now_utc)

            hydrometric_futures = [
                (st, ex.submit(lib.fetch_hydrometric_water_level, st["station_id"], st["provterr"]))
                for st in hydro_stations
            ]

            # Collect results
            if fut_modis:
                try:
                    modis_bytes, modis_date = fut_modis.result()
                except Exception as e:
                    print(f"[{sid}] MODIS FAILED: {e}")
            if fut_wl:
                try:
                    gdsps_times, gdsps_values, gdsps_yearly_mean = fut_wl.result()
                except Exception as e:
                    print(f"[{sid}] WATER LEVEL FAILED: {e}")
            if fut_s1:
                try:
                    sentinel1_bytes, sentinel1_caption = fut_s1.result()
                except Exception as e:
                    print(f"[{sid}] SENTINEL-1 FAILED: {e}")
            if fut_ice:
                try:
                    sea_ice_bytes, sea_ice_caption = fut_ice.result()
                except Exception as e:
                    print(f"[{sid}] SEA ICE FAILED: {e}")
            if fut_ice_zoom:
                try:
                    sea_ice_zoom_bytes, sea_ice_zoom_caption = fut_ice_zoom.result()
                except Exception as e:
                    print(f"[{sid}] SEA ICE ZOOM FAILED: {e}")
            if fut_lake_ice:
                try:
                    lake_ice_bytes, lake_ice_caption = fut_lake_ice.result()
                except Exception as e:
                    print(f"[{sid}] LAKE ICE FAILED: {e}")
            if fut_wave:
                try:
                    wave_data = fut_wave.result()
                except Exception as e:
                    print(f"[{sid}] WAVE FORECAST FAILED: {e}")
            if fut_fire:
                try:
                    fires = fut_fire.result()
                except Exception as e:
                    print(f"[{sid}] WILDFIRE FAILED: {e}")
            for st, fut in hydrometric_futures:
                try:
                    h_times, h_values = fut.result()
                except Exception as e:
                    print(f"[{sid}] HYDROMETRIC[{st['station_id']}] FAILED: {e}")
                    h_times, h_values = None, None
                hydrometric_results.append((st, h_times, h_values))

        if modis_bytes:
            modis_block_obj, _ = lib._upload_chart_or_caption(modis_bytes, "modis.png", None)

    # ------------------------------------------------------------------ #
    # ASSEMBLE PAGE                                                        #
    # ------------------------------------------------------------------ #
    institution_text = community.get(
        "institution_text",
        "This dashboard is provided by the Alfred Wegener Institute Helmholtz Centre for Polar and Marine Research.",
    )
    blocks = []
    blocks += lib.build_header_blocks(
        now_local, logo_url=logo_url, logo_png_bytes=logo_png_bytes,
        institution_text=institution_text, tz_name=tz_name,
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

    if "gem_forecast" in enabled:
        blocks += lib.build_gem_forecast_section(gem_forecast, tz_name, now_utc=now_utc)

    if "marine" in enabled and marine_text is not None:
        blocks += lib.build_marine_forecast_section(
            marine_text, marine_source_text, marine_zone_name, marine_zone_id,
        )

    if "wildfire" in enabled:
        blocks += lib.build_wildfire_section(
            fires, lat, lon, now_utc, tz_name,
            bbox_3413=community.get("modis_bbox_3413"),
            center_x=community.get("modis_center_x"),
            center_y=community.get("modis_center_y"),
            rotation_deg=community.get("modis_rotation_deg", 0.0),
        )

    if "wave_forecast" in enabled:
        blocks += lib.build_wave_forecast_section(wave_data)

    if "water_level" in enabled:
        wl_text = (
            [("Latest forecast value: ", f"{gdsps_values[0]:.2f} m")]
            if gdsps_values else "Total water level forecast unavailable."
        )
        wl_chart_bytes, wl_chart_caption = lib.build_water_level_chart(
            gdsps_times, gdsps_values, tz_name, gdsps_yearly_mean,
        )
        blocks += lib.build_total_water_level_section(wl_text, wl_chart_bytes, wl_chart_caption)

    if "hydrometric" in enabled:
        for st, h_times, h_values in hydrometric_results:
            h_chart_bytes, h_chart_caption = lib.build_hydrometric_chart(
                h_times, h_values, st["station_id"], st["river_name"], tz_name,
            )
            blocks += lib.build_hydrometric_section(h_chart_bytes, h_chart_caption, st["heading"])

    if "modis" in enabled:
        modis_caption_str = (
            f"NASA MODIS Terra, true color, {modis_date}." if modis_date else "MODIS image unavailable."
        )
        blocks += lib.build_modis_section(
            modis_block_obj, modis_caption_str, modis_date, now_utc,
            community.get("modis_bbox_3413", ""), site_label,
        )

    if "sentinel1" in enabled:
        blocks += lib.build_sentinel1_section(sentinel1_bytes, sentinel1_caption, None, site_label)

    if "sea_ice" in enabled:
        blocks += lib.build_sea_ice_section(
            sea_ice_bytes, sea_ice_caption, site_label,
            title="🧊 Sea Ice — Sentinel-1 Classification",
        )

    if "sea_ice_zoom" in enabled:
        blocks += lib.build_sea_ice_section(
            sea_ice_zoom_bytes, sea_ice_zoom_caption, site_label,
            title="🧊 Sea Ice — Sentinel-1 Classification — Zoom",
        )

    if "lake_river_ice" in enabled:
        blocks += lib.build_lake_ice_section(lake_ice_bytes, lake_ice_caption, site_label)

    if "temperature" in enabled:
        blocks += lib.build_temperature_chart_section(temp_chart_bytes, temp_chart_caption)
        blocks += lib.build_tdd_histogram_section(tdd_bytes, tdd_caption)

    if "wind_chart" in enabled:
        blocks += lib.build_wind_chart_section(wind_chart_bytes, wind_chart_caption)

    # Build disclaimer from active blocks
    sources = []
    if "gem_forecast" in enabled or "weather" in enabled:
        sources += ["gem", "open_meteo"]
    if "modis" in enabled:
        sources.append("modis")
    if any(b in enabled for b in ("sentinel1", "sea_ice", "sea_ice_zoom", "lake_river_ice")):
        sources.append("sentinel1")
    if "water_level" in enabled:
        sources.append("cmems")
    if "wave_forecast" in enabled:
        sources.append("waves")
    if "marine" in enabled or "alerts" in enabled:
        sources += ["marine", "alerts"]
    if "hydrometric" in enabled:
        sources.append("hydrometric")
    if "wildfire" in enabled:
        sources.append("wildfire")
    blocks += lib.build_disclaimer_section(sources)

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

    for i, community in enumerate(communities):
        if i > 0:
            time.sleep(5)
        try:
            update_community(community, now_utc)
        except Exception as e:
            import traceback
            print(f"ERROR [{community['id']}]: {e}")
            traceback.print_exc()

    print("\nAll communities updated.")


if __name__ == "__main__":
    main()
