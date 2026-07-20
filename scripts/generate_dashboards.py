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


_OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
_OVERPASS_HEADERS = {"User-Agent": "alfred-portal/1.0 (arctic environmental dashboard; hugues.lantuit@awi.de)"}


def _overpass_post(query, timeout=75):
    """POST an Overpass query, retrying with a backup server on 5xx or timeout."""
    import requests as _requests
    last_exc = None
    for server in _OVERPASS_SERVERS:
        try:
            r = _requests.post(server, data={"data": query}, headers=_OVERPASS_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            print(f"Overpass {server} failed: {e} — trying next server")
            last_exc = e
    raise last_exc


_GEOJSON_MAX_BYTES = 40 * 1024 * 1024  # 40 MB hard limit — GitHub rejects files > 100 MB
_COORD_PRECISION = 4  # ~11 m; enough for display, cuts file size significantly


def _round_coords(coords):
    return [[round(c[0], _COORD_PRECISION), round(c[1], _COORD_PRECISION)] for c in coords]


def _write_geojson_safe(community_id, label, geojson, out_path):
    """Serialize geojson to out_path; skip if the result would exceed GitHub's size limit."""
    import json as _json
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    data = _json.dumps(geojson, separators=(",", ":"))
    if len(data) > _GEOJSON_MAX_BYTES:
        print(
            f"[{community_id}] {label}: output is {len(data)/1e6:.1f} MB — exceeds "
            f"{_GEOJSON_MAX_BYTES/1e6:.0f} MB limit, skipping file write to avoid GitHub rejection"
        )
        return False
    with open(out_path, "w") as f:
        f.write(data)
    return True


def ensure_coastline_geojson(community_id, bbox_latlon, out_path):
    """Fetch natural=coastline ways from Overpass API and save as GeoJSON LineStrings."""
    import json as _json
    s, w, n, e = bbox_latlon
    query = f"[out:json][timeout:60];way[natural=coastline]({s},{w},{n},{e});out geom qt 2000;"
    print(f"[{community_id}] COASTLINE: fetching from Overpass ({s},{w},{n},{e})")
    try:
        r = _overpass_post(query)
        elements = r.json().get("elements", [])
        features = []
        for el in elements:
            if el.get("type") == "way" and "geometry" in el:
                coords = _round_coords([[pt["lon"], pt["lat"]] for pt in el["geometry"]])
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": coords},
                    "properties": {},
                })
        geojson = {"type": "FeatureCollection", "features": features}
        if _write_geojson_safe(community_id, "COASTLINE", geojson, out_path):
            print(f"[{community_id}] COASTLINE: {len(features)} segments saved to {out_path}")
    except Exception as e:
        print(f"[{community_id}] COASTLINE FETCH FAILED: {e}")


def _assemble_osm_rings(members, role):
    """
    Connect Overpass member-way segments (all sharing a given role) into
    closed rings. Each segment is a list of (lon, lat) tuples. Segments
    are joined end-to-end; the result is closed if not already so.
    Returns a list of rings (each ring is a list of [lon, lat] pairs).
    """
    segments = []
    for m in members:
        if m.get("role") == role and m.get("type") == "way" and "geometry" in m:
            pts = [(pt["lon"], pt["lat"]) for pt in m["geometry"]]
            if pts:
                segments.append(pts)
    if not segments:
        return []
    rings = []
    while segments:
        ring = list(segments.pop(0))
        changed = True
        while changed and segments:
            changed = False
            for i, seg in enumerate(segments):
                if ring[-1] == seg[0]:
                    ring.extend(seg[1:]); segments.pop(i); changed = True; break
                elif ring[-1] == seg[-1]:
                    ring.extend(reversed(seg[:-1])); segments.pop(i); changed = True; break
                elif ring[0] == seg[-1]:
                    ring = list(seg) + ring[1:]; segments.pop(i); changed = True; break
                elif ring[0] == seg[0]:
                    ring = list(reversed(seg)) + ring[1:]; segments.pop(i); changed = True; break
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        if len(ring) >= 4:
            rings.append([[c[0], c[1]] for c in ring])
    return rings


def ensure_water_bodies_geojson(community_id, bbox_latlon, out_path):
    """Fetch OSM water body polygons from Overpass and save as GeoJSON.

    Handles both simple ways and multipolygon relations so that large
    mapped features (e.g. Mackenzie River delta channels) are included.
    """
    import json as _json
    s, w, n, e = bbox_latlon
    query = (
        f"[out:json][timeout:90];"
        f"("
        f"  way[natural=water]({s},{w},{n},{e});"
        f"  way[waterway=river]({s},{w},{n},{e});"
        f"  way[waterway=riverbank]({s},{w},{n},{e});"
        f"  relation[natural=water]({s},{w},{n},{e});"
        f"  relation[waterway=riverbank]({s},{w},{n},{e});"
        f");"
        f"out geom;"
    )
    print(f"[{community_id}] WATER BODIES: fetching from Overpass ({s:.3f},{w:.3f},{n:.3f},{e:.3f})")
    try:
        r = _overpass_post(query)
        elements = r.json().get("elements", [])
        features = []
        for el in elements:
            etype = el.get("type")

            if etype == "way" and "geometry" in el:
                coords = _round_coords([[pt["lon"], pt["lat"]] for pt in el["geometry"]])
                if coords and coords[0] == coords[-1] and len(coords) >= 4:
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [coords]},
                        "properties": {},
                    })
                # Unclosed ways (e.g. riverbank segments) are skipped —
                # they only appear as part of relations handled below.

            elif etype == "relation":
                members = el.get("members", [])
                outer_rings = _assemble_osm_rings(members, "outer")
                inner_rings = _assemble_osm_rings(members, "inner")
                if not outer_rings:
                    continue
                if len(outer_rings) == 1:
                    all_rings = [outer_rings[0]] + inner_rings
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": all_rings},
                        "properties": {},
                    })
                else:
                    polys = [[outer] + inner_rings for outer in outer_rings]
                    features.append({
                        "type": "Feature",
                        "geometry": {"type": "MultiPolygon", "coordinates": polys},
                        "properties": {},
                    })

        geojson = {"type": "FeatureCollection", "features": features}
        if _write_geojson_safe(community_id, "WATER BODIES", geojson, out_path):
            print(f"[{community_id}] WATER BODIES: {len(features)} features saved to {out_path}")
    except Exception as e:
        print(f"[{community_id}] WATER BODIES FETCH FAILED: {e}")


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
    lib.COMMUNITY_ID = sid
    lib.CHARTS_SAVE_DIR = os.path.join(COMMUNITIES_DIR, sid, "charts")

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
                mini_strip_bytes = lib.build_mini_forecast_strip(gem_days[:5]) if gem_days else None
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

    tide_text = tide_chart_bytes = tide_chart_caption = tide_url = None
    tide_station_code = community.get("tide_station_code")
    tide_station_name = community.get("tide_station_name", "")
    noaa_tide_station_id   = community.get("noaa_tide_station_id")
    noaa_tide_station_name = community.get("noaa_tide_station_name", "")
    if "weather" in enabled and tide_station_code:
        station_id  = lib.find_iwls_station_id(tide_station_code)
        tide_points = lib.fetch_tide_predictions(station_id, now_utc, hours_ahead=24*7) if station_id else None
        tide_text   = lib.format_tide_text(tide_points, now_utc, tide_station_code, tide_station_name)
        tide_chart_bytes, tide_chart_caption = lib.build_tide_chart(tide_points, now_utc, tz_name)
    elif "weather" in enabled and noaa_tide_station_id:
        tide_points = lib.fetch_tide_predictions_noaa(noaa_tide_station_id, now_utc, hours_ahead=24*7)
        tide_text   = lib.format_tide_text(tide_points, now_utc, noaa_tide_station_id, noaa_tide_station_name)
        tide_chart_bytes, tide_chart_caption = lib.build_tide_chart(tide_points, now_utc, tz_name)
        tide_station_code = noaa_tide_station_id  # so build_todays_conditions_section shows tide card
        tide_url = f"https://tidesandcurrents.noaa.gov/stationhome.html?id={noaa_tide_station_id}"

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
    # Snow depth card (terrestrial/lake/riverine sites)                   #
    # ------------------------------------------------------------------ #
    snow_card = None
    if "snow_depth" in enabled:
        print(f"[{sid}] STARTING: snow depth card")
        snow_card = lib.build_snow_depth_card(lat, lon, now_utc)
    if "webcam" in enabled:
        webcam_url = community.get("webcam_url")
        if webcam_url:
            print(f"[{sid}] STARTING: webcam card")
            snow_card = lib.build_webcam_card(
                webcam_url,
                webcam_label=community.get("webcam_label", "Webcam"),
                webcam_page_url=community.get("webcam_page_url"),
            )

    logo_url       = None
    logo_png_bytes = None

    # ------------------------------------------------------------------ #
    # Parallel fetches: MODIS, water level, Sentinel-1, sea ice, wave,    #
    # wildfire, hydrometric                                                #
    # ------------------------------------------------------------------ #
    modis_bytes = modis_date = None
    modis_block_obj = None
    gdsps_times = gdsps_values = gdsps_yearly_mean = None
    topaz_times = topaz_values = topaz_yearly_mean = None
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
        utm_zone = community.get("utm_zone")
        utm_epsg = community.get("utm_epsg")
        # Compute UTM center dynamically from lat/lon — never trust hardcoded values
        utm_center_x, utm_center_y = lib.latlon_to_utm(lat, lon, zone=utm_zone) if utm_zone else (None, None)
        # Compute EPSG:3413 center dynamically too
        modis_cx, modis_cy = lib.compute_3413_center(lat, lon)
        _wb_rel = community.get("water_bodies_geojson_path")
        water_bod = (
            os.path.join(COMMUNITIES_DIR, community["id"], _wb_rel)
            if _wb_rel else None
        )
        if water_bod and not os.path.exists(water_bod) and "lake_river_ice" in enabled:
            # Pad must cover the full lake-ice Sentinel-1 frame (50 km half-width).
            # At Arctic latitudes lon degrees are compressed: use 80 km to be safe.
            import math as _math
            lat_pad = 80_000 / 111_000          # ~0.72°
            lon_pad = 80_000 / (111_000 * _math.cos(_math.radians(lat)))
            bbox = (lat - lat_pad, lon - lon_pad, lat + lat_pad, lon + lon_pad)
            ensure_water_bodies_geojson(sid, bbox, water_bod)
        # Coastline GeoJSON — used by Sentinel-1 SAR/ice only, NOT by MODIS
        _coastline_rel = community.get("coastline_geojson_path")
        coastline = (
            os.path.join(COMMUNITIES_DIR, community["id"], _coastline_rel)
            if _coastline_rel else None
        )
        if coastline and not os.path.exists(coastline):
            pts = community.get("map_points", [])
            lats = [p[0] for p in pts] + [lat]
            lons = [p[1] for p in pts] + [lon]
            pad = 2.0
            bbox_ll = (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
            ensure_coastline_geojson(sid, bbox_ll, coastline)
        map_pts    = community.get("map_points", [])
        ref_lines  = community.get("map_reference_lines", [])
        hydro_stations = community.get("hydrometric_stations", [])

        extra = 1 if "water_level" in needs_parallel else 0
        workers = max(len(needs_parallel) + len(hydro_stations) + extra, 1)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_modis = fut_wl = fut_topaz = fut_s1 = None
            fut_ice = fut_ice_zoom = fut_lake_ice = None
            fut_wave = fut_fire = None

            if "modis" in enabled:
                # Build bbox from computed center; use config rotation_deg
                h = 150_000 * getattr(lib, "MODIS_OVERSIZE_FACTOR", 1.2)
                modis_bbox = f"{modis_cx-h:.0f},{modis_cy-h:.0f},{modis_cx+h:.0f},{modis_cy+h:.0f}"
                fut_modis = ex.submit(
                    lib.fetch_and_process_modis,
                    bbox_3413=modis_bbox, center_x=modis_cx, center_y=modis_cy,
                    rotation_deg=community.get("modis_rotation_deg", 0.0),
                    points=map_pts, now_utc=now_utc, tz_name=tz_name,
                    reference_lines=ref_lines,
                    # No coastline overlay on MODIS — it adds clutter with no benefit
                )

            if "water_level" in enabled:
                fut_wl = ex.submit(
                    lib.fetch_gdsps_water_level,
                    lat=lat, lon=lon, now_utc=now_utc, site_label=site_label,
                    yearly_mean=community.get("water_level_yearly_mean", 0.0),
                )
                fut_topaz = ex.submit(
                    lib.fetch_copernicus_water_level,
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
                _ssdc_lat = community.get("ssdc_lat")
                _ssdc_lon = community.get("ssdc_lon")
                _ssdc_label = community.get("ssdc_label", "SSDC")
                _ssdc_arrows = (
                    # dx=+65 → tail to the east, dy=-65 → tail to the north
                    # tip_offset=22 → arrowhead stops just short of the SSDC
                    [(_ssdc_lat, _ssdc_lon, _ssdc_label, 65, -65, 22)]
                    if _ssdc_lat is not None and _ssdc_lon is not None
                    else None
                )
                fut_ice_zoom = ex.submit(
                    lib.fetch_and_process_sentinel1_ice,
                    lat=lat, lon=lon, site_label=site_label,
                    utm_zone=utm_zone, utm_epsg=utm_epsg,
                    center_x=utm_center_x, center_y=utm_center_y,
                    points=map_pts, tz_name=tz_name, half_width_m=25_000,
                    reference_lines=ref_lines, coastline_geojson_path=coastline,
                    now_utc=now_utc, arrow_annotations=_ssdc_arrows,
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
                wave_lat = community.get("wave_lat", lat)
                wave_lon = community.get("wave_lon", lon)
                fut_wave = ex.submit(lib.fetch_wave_forecast, wave_lat, wave_lon, now_utc, site_label)

            if "wildfire" in enabled:
                fut_fire = ex.submit(lib.fetch_cwfis_wildfires, lat, lon, 600, now_utc)

            hydrometric_futures = [
                (st, ex.submit(lib.fetch_hydrometric_water_level, st["station_id"], st["provterr"]))
                for st in hydro_stations
            ]

            # Collect results — all with timeouts to prevent indefinite hangs
            if fut_modis:
                try:
                    modis_bytes, modis_date = fut_modis.result(timeout=180)
                except Exception as e:
                    print(f"[{sid}] MODIS FAILED: {e}")
            if fut_wl:
                try:
                    gdsps_times, gdsps_values, gdsps_yearly_mean = fut_wl.result(timeout=180)
                except Exception as e:
                    print(f"[{sid}] WATER LEVEL FAILED: {e}")
            if fut_topaz:
                try:
                    topaz_times, topaz_values, topaz_yearly_mean = fut_topaz.result(timeout=300)
                except Exception as e:
                    print(f"[{sid}] TOPAZ WATER LEVEL FAILED: {e}")
            if fut_s1:
                try:
                    sentinel1_bytes, sentinel1_caption = fut_s1.result(timeout=240)
                except Exception as e:
                    print(f"[{sid}] SENTINEL-1 FAILED: {e}")
            if fut_ice:
                try:
                    sea_ice_bytes, sea_ice_caption = fut_ice.result(timeout=240)
                except Exception as e:
                    print(f"[{sid}] SEA ICE FAILED: {e}")
            if fut_ice_zoom:
                try:
                    sea_ice_zoom_bytes, sea_ice_zoom_caption = fut_ice_zoom.result(timeout=240)
                except Exception as e:
                    print(f"[{sid}] SEA ICE ZOOM FAILED: {e}")
            if fut_lake_ice:
                try:
                    lake_ice_bytes, lake_ice_caption = fut_lake_ice.result(timeout=240)
                except Exception as e:
                    print(f"[{sid}] LAKE ICE FAILED: {e}")
            if fut_wave:
                try:
                    wave_data = fut_wave.result(timeout=120)
                except Exception as e:
                    print(f"[{sid}] WAVE FORECAST FAILED: {e}")
            if fut_fire:
                try:
                    fires = fut_fire.result(timeout=90)
                except Exception as e:
                    print(f"[{sid}] WILDFIRE FAILED: {e}")
            for st, fut in hydrometric_futures:
                try:
                    h_times, h_values, h_unit = fut.result(timeout=90)
                except Exception as e:
                    print(f"[{sid}] HYDROMETRIC[{st['station_id']}] FAILED: {e}")
                    h_times, h_values, h_unit = None, None, "level"
                hydrometric_results.append((st, h_times, h_values, h_unit))

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
            tide_text, tide_chart_bytes, tide_chart_caption, tide_station_code or None,
            sun_text, sun_chart_bytes, sun_chart_caption,
            extra_card=snow_card, tide_url=tide_url,
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
        _wf_h = 150_000 * getattr(lib, "MODIS_OVERSIZE_FACTOR", 1.2)
        _wf_bbox = f"{modis_cx-_wf_h:.0f},{modis_cy-_wf_h:.0f},{modis_cx+_wf_h:.0f},{modis_cy+_wf_h:.0f}"
        blocks += lib.build_wildfire_section(
            fires, lat, lon, now_utc, tz_name,
            bbox_3413=_wf_bbox,
            center_x=modis_cx,
            center_y=modis_cy,
            rotation_deg=community.get("modis_rotation_deg", 0.0),
        )

    if "wave_forecast" in enabled:
        blocks += lib.build_wave_forecast_section(wave_data)

    if "water_level" in enabled:
        wl_text = (
            [("TOPAZ6 now: ", f"{topaz_values[0]:.2f} m (vs. {topaz_yearly_mean:.2f} m yearly mean)" if topaz_yearly_mean is not None else f"{topaz_values[0]:.2f} m")]
            if topaz_values else
            [("GDSPS now: ", f"{gdsps_values[0]:.2f} m")]
            if gdsps_values else "Total water level forecast unavailable."
        )
        wl_chart_bytes, wl_chart_caption = lib.build_water_level_chart(
            topaz_times, topaz_values, tz_name, topaz_yearly_mean,
            gdsps_times=gdsps_times, gdsps_values=gdsps_values,
        )
        blocks += lib.build_total_water_level_section(wl_text, wl_chart_bytes, wl_chart_caption)

    if "hydrometric" in enabled:
        for st, h_times, h_values, h_unit in hydrometric_results:
            h_chart_bytes, h_chart_caption = lib.build_hydrometric_chart(
                h_times, h_values, st["station_id"], st["river_name"], tz_name, unit=h_unit,
            )
            blocks += lib.build_hydrometric_section(h_chart_bytes, h_chart_caption, st["heading"])

    if "modis" in enabled:
        modis_caption_str = (
            f"NASA MODIS Terra, true color, {modis_date}." if modis_date else "MODIS image unavailable."
        )
        blocks += lib.build_modis_section(
            modis_block_obj, modis_caption_str, modis_date, now_utc,
            modis_bbox, site_label,
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
            filename="sea_ice_zoom.png",
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

    _COMMUNITY_TIMEOUT = 900  # 15 min per community; 9 × 15 = 135 min max total
    for i, community in enumerate(communities):
        if i > 0:
            time.sleep(5)
        t0 = time.monotonic()
        try:
            update_community(community, now_utc)
        except Exception as e:
            import traceback
            print(f"ERROR [{community['id']}]: {e}")
            traceback.print_exc()
        elapsed = time.monotonic() - t0
        print(f"[{community['id']}] wall time: {elapsed:.0f}s")

    print("\nAll communities updated.")


if __name__ == "__main__":
    main()
