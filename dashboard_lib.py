"""
dashboard_lib.py — Shared library for Arctic coastal monitoring dashboards.

This module contains every function that is identical across sites
(Herschel Island, Shingle Point, and future sites such as Tuktoyaktuk or
Kendall Island). Site-specific values (coordinates, station IDs, rotation
angles, which sections to display, etc.) live in each site repo's own
config.py, not here.

A site's dashboard_update.py is a thin entrypoint: it imports this
library, imports its own config, and calls config.SECTIONS in order to
assemble the page. See README.md in this repo for the full architecture
and how to add a new site or a new section type.
"""

import os
import io
import json
import math
import time
import requests
import numpy as np
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from notion_client import Client
import matplotlib
matplotlib.use("Agg")  # headless backend, no display needed in CI
import matplotlib.pyplot as plt


# =========================================================
# NOTION CLIENT
# Each site provides its own NOTION_TOKEN/PAGE_ID via environment
# variables (same names as before — only the secret VALUES differ per
# repo, the variable names stay consistent across sites).
# =========================================================
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
PAGE_ID = os.environ.get("NOTION_PAGE_ID", "")  # overridden per-community by generate_dashboards.py

notion = Client(auth=NOTION_TOKEN)

# ---- Image delivery via GitHub raw URLs ----
# When CHARTS_SAVE_DIR is set (by generate_dashboards.py), upload_image_to_notion
# writes the PNG to disk and returns a GitHub raw URL sentinel instead of using
# Notion's file upload API (which has been unreliable).
COMMUNITY_ID = None        # set per-community: lib.COMMUNITY_ID = community["id"]
CHARTS_SAVE_DIR = None     # set per-community: lib.CHARTS_SAVE_DIR = <path>
_GITHUB_REPO   = os.environ.get("GITHUB_REPOSITORY", "hlantuit/alfred-portal")
_GITHUB_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")


# =========================================================
# HISTORICAL DATA CACHE
# Historical (complete, past) years of daily temperature never change
# once fetched — only the current (in-progress) year needs refreshing.
# This cache stores one full calendar year of daily mean temperatures
# per entry, keyed by year, in a JSON file committed back to the site's
# OWN repo by its workflow after each run (see the "Commit cache" step
# in each site's workflow YAML) — the cache file itself stays in each
# site repo, not in this shared library, since the data is site-specific
# even though the caching CODE is shared.
# =========================================================
CACHE_FILE_PATH = "cache/daily_temps_cache.json"


def load_temp_cache():
    """Loads the historical temperature cache from disk, or {} if missing/corrupt."""
    try:
        with open(CACHE_FILE_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"CACHE: could not load {CACHE_FILE_PATH} ({e}), starting with empty cache")
        return {}


def save_temp_cache(cache):
    """Writes the cache back to disk. Creates the cache/ directory if needed."""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE_PATH), exist_ok=True)
        with open(CACHE_FILE_PATH, "w") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"CACHE: failed to save {CACHE_FILE_PATH}: {e}")


# =========================================================
# TIME HELPERS
# 'now' stays naive UTC throughout — every API call, date arithmetic
# ("yesterday", "last 30 days", etc.) and historical fetch depends on
# this being UTC, so it is never converted in place. For DISPLAY
# purposes only, to_local_time() converts a UTC datetime to a site's
# local timezone.
# =========================================================
def to_local_time(utc_dt, tz_name):
    """
    Converts a naive UTC datetime to a site's local time, DST-aware.
    tz_name is an IANA timezone name (e.g. "America/Inuvik") — each
    site's config.py specifies its own, since sites can span different
    timezones (Yukon abandoned the twice-yearly DST switch in 2020,
    while NWT communities like Inuvik still observe it — verify the
    correct zone for each new site rather than assuming).
    """
    return utc_dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))


# =========================================================
# HELPERS — Notion block builders (kept tiny to reduce repetition)
# =========================================================
def heading(text, level=2):
    tag = f"heading_{level}"
    return {"object": "block", "type": tag, tag: {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def _line_to_segments(line):
    """
    Converts one line specification into a list of rich_text segments
    (no trailing newline added here — that's handled by the caller).

    A line can be:
      - a plain string: rendered as-is, no bolding.
      - a (label, value) tuple: label in normal text, value in bold.
      - a list of strings/tuples: each rendered in sequence on the same
        line, mixing plain and bold segments freely.
    """
    if isinstance(line, list):
        segments = []
        for piece in line:
            segments.extend(_line_to_segments(piece))
        return segments

    if isinstance(line, tuple):
        label, value = line
        segments = []
        if label:
            segments.append({"type": "text", "text": {"content": label}})
        segments.append({
            "type": "text",
            "text": {"content": str(value)},
            "annotations": {"bold": True},
        })
        return segments

    return [{"type": "text", "text": {"content": line}}]


def build_bolded_lines(lines):
    """
    Builds a single rich_text array from a list of lines. Lines are
    separated by a newline appended to the end of the last segment of
    the previous line, rather than as a standalone segment, since a lone
    "\\n"-only text object with no preceding content can be dropped by
    Notion in practice.
    """
    segments = []
    for i, line in enumerate(lines):
        if i > 0 and segments:
            segments[-1]["text"]["content"] += "\n"
        segments.extend(_line_to_segments(line))
    return segments


def callout(lines, emoji=None, color="gray_background", children=None):
    """
    Builds a callout block from a list of lines. children: optional list
    of child blocks (e.g. an image block) to nest inside the callout —
    must be included in the same create call, not patched in afterward.
    """
    if isinstance(lines, str):
        lines = [lines]
    callout_obj = {
        "rich_text": build_bolded_lines(lines),
        "color": color,
    }
    if children:
        callout_obj["children"] = children
    return {"object": "block", "type": "callout", "callout": callout_obj}


def disclaimer_paragraph(text):
    """Gray, italicized paragraph block — used for the page's bottom disclaimer."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": text},
                "annotations": {"color": "gray", "italic": True},
            }]
        },
    }


def paragraph(lines):
    """Builds a paragraph block from a list of lines, or a plain string."""
    if isinstance(lines, str):
        lines = [lines]
    return {"object": "block", "paragraph": {"rich_text": build_bolded_lines(lines)}, "type": "paragraph"}


def gray_caption(text):
    """
    Paragraph block in gray text — the closest real equivalent to a
    de-emphasized caption, since Notion's API has no per-block font-size
    control (only a page-wide 'Small text' toggle affecting everything).
    """
    if not text:
        return paragraph("")
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}, "annotations": {"color": "gray"}}]},
    }


def link_paragraph(label, url, prefix=None, prefix_gray=False):
    """
    Paragraph block containing a clickable link, optionally preceded by
    plain (non-linked) text on the same line. Notion always renders
    separate paragraph blocks on separate lines regardless of content,
    so a shared-line caption+link needs to be built as one block, not two.
    """
    rich_text = []
    if prefix:
        prefix_segment = {"type": "text", "text": {"content": prefix}}
        if prefix_gray:
            prefix_segment["annotations"] = {"color": "gray"}
        rich_text.append(prefix_segment)
    rich_text.append({"type": "text", "text": {"content": label, "link": {"url": url}}})
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def table_row(cell_lines_list):
    """Builds a single table_row block, one 'lines' spec per column."""
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {"cells": [build_bolded_lines([cell]) for cell in cell_lines_list]},
    }


def table(header_cells, rows, has_column_header=True):
    """
    Builds a table block. All rows — header included — must be supplied
    as nested children in the same create call; Notion's table_width is
    fixed at creation and rows cannot be patched in afterward.
    """
    width = len(header_cells)
    all_rows = [table_row(header_cells)] + [table_row(r) for r in rows]
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": has_column_header,
            "has_row_header": False,
            "children": all_rows,
        },
    }


def columns(*column_block_lists, width_ratios=None):
    """
    Builds a column_list block with N columns. Notion requires all
    column content to be created in the same request as the column_list
    itself — content cannot be patched into columns afterward.
    """
    column_objs = []
    for i, blocks in enumerate(column_block_lists):
        column_data = {"children": blocks}
        if width_ratios:
            column_data["width_ratio"] = width_ratios[i]
        column_objs.append({"object": "block", "type": "column", "column": column_data})

    return {"object": "block", "type": "column_list", "column_list": {"children": column_objs}}


def upload_image_to_notion(image_bytes, filename="image.png"):
    """
    Uploads raw image bytes to Notion's file upload API and returns the
    upload id — used instead of external image URLs because Notion's
    external-URL fetcher is unreliable for query-string-based image
    services (no file extension, content negotiated at request time).

    If CHARTS_SAVE_DIR is set, writes the PNG to disk and returns a GitHub
    raw URL sentinel (``__ext__https://...``) — the Notion upload API is
    bypassed entirely.  image_block_from_upload() converts the sentinel to
    an external image block automatically.

    Otherwise falls back to Notion's single-part upload flow:
      1. POST /v1/file_uploads  →  {id, upload_url}
      2. PUT upload_url with raw bytes + Content-Type: image/png
    """
    if CHARTS_SAVE_DIR and COMMUNITY_ID:
        os.makedirs(CHARTS_SAVE_DIR, exist_ok=True)
        img_path = os.path.join(CHARTS_SAVE_DIR, filename)
        with open(img_path, "wb") as _f:
            _f.write(image_bytes)
        github_url = (
            f"https://raw.githubusercontent.com/{_GITHUB_REPO}/{_GITHUB_BRANCH}"
            f"/communities/{COMMUNITY_ID}/charts/{filename}"
        )
        print(f"IMAGE SAVED: {img_path} → {github_url}")
        return f"__ext__{github_url}"

    # Notion file upload API fallback (used when running locally without git context).
    create_resp = requests.post(
        "https://api.notion.com/v1/file_uploads",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        json={"content_type": "image/png", "name": filename},
        timeout=20,
    )
    create_resp.raise_for_status()
    create_json = create_resp.json()
    upload_id  = create_json["id"]
    upload_url = create_json.get("upload_url")

    if upload_url:
        put_resp = requests.put(
            upload_url,
            headers={"Content-Type": "image/png"},
            data=image_bytes,
            timeout=60,
        )
        put_resp.raise_for_status()
    else:
        print(f"NOTION UPLOAD DEBUG: no upload_url — create keys={list(create_json.keys())}")
        send_resp = requests.post(
            f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "image/png",
            },
            data=image_bytes,
            timeout=60,
        )
        if send_resp.status_code != 200:
            send_resp = requests.post(
                f"https://api.notion.com/v1/file_uploads/{upload_id}/send",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Notion-Version": "2022-06-28",
                },
                files={"file": (filename, image_bytes, "image/png")},
                timeout=60,
            )
        send_resp.raise_for_status()

    return upload_id


def image_block_from_upload(upload_id):
    if isinstance(upload_id, str) and upload_id.startswith("__ext__"):
        return external_image_block(upload_id[7:])
    return {
        "object": "block",
        "type": "image",
        "image": {"type": "file_upload", "file_upload": {"id": upload_id}},
    }


def external_image_block(url):
    """
    Image block referencing a directly-hosted external URL. Per Notion's
    API docs, the URL must be directly hosted — not a URL pointing to a
    service that retrieves the image — and .svg is among supported types.
    """
    return {"object": "block", "type": "image", "image": {"type": "external", "external": {"url": url}}}


def fetch_and_convert_logo_to_png(svg_url, output_width=120):
    """
    Fetches an SVG logo and converts it to a fixed-pixel-width PNG.
    Embedding raw SVG via external_image_block() rendered inconsistently
    across devices in practice (a fixed-pixel PNG, uploaded the same way
    as every chart, gives precise size control independent of that).

    Returns PNG bytes, or None on failure, so a problem here never blocks
    the rest of the dashboard from updating.
    """
    try:
        import cairosvg

        resp = requests.get(svg_url, timeout=15)
        resp.raise_for_status()
        return cairosvg.svg2png(bytestring=resp.content, output_width=output_width)
    except Exception as e:
        print("LOGO SVG-TO-PNG CONVERSION FAILED:", e)
        return None


def png_on_white(png_bytes):
    """Composite a transparent PNG onto a white background.

    Charts are generated with transparent backgrounds so they blend into
    Notion's white page. When a user opens the image full-screen in a mobile
    browser it renders over dark or patterned content and becomes unreadable.
    Compositing onto white fixes the full-screen case while looking identical
    inside Notion (white page = white background).
    """
    from PIL import Image as _PImage
    img = _PImage.open(io.BytesIO(png_bytes)).convert("RGBA")
    bg = _PImage.new("RGBA", img.size, (255, 255, 255, 255))
    bg.paste(img, mask=img.split()[3])
    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG", optimize=True)
    out.seek(0)
    return out.read()


def fig_to_png_bytes(fig, white_bg=False):
    """Renders a matplotlib figure to PNG bytes in memory, then closes it.

    white_bg=True composites onto white before returning — use for standalone
    full-width charts (GEM, temperature history, TDD, wind, water level, waves)
    so they are readable when opened full-screen on mobile. Leave False for
    charts embedded inside coloured Notion callout cards (sun, tide, snow,
    wind forecast mini-chart) where the card background must show through.
    """
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    raw = buf.read()
    return png_on_white(raw) if white_bg else raw


def get_with_retry(url, params=None, timeout=20, retries=1, backoff_seconds=3):
    """
    Wraps requests.get with automatic retries on timeout or connection
    errors. Several Environment Canada and Open-Meteo endpoints have
    shown transient connection timeouts in practice on real runs
    (sometimes correlated across multiple endpoints on the same run,
    suggesting shared underlying infrastructure rather than independent
    failures), so retrying meaningfully improves the odds of getting
    real data without dramatically increasing total run time when things
    are already working normally.
    """
    last_exception = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            if attempt < retries:
                time.sleep(backoff_seconds)
                continue
            raise
        except Exception:
            raise
    raise last_exception


# =========================================================
# MODULE — WEATHER (temperature, wind, humidity, pressure)
# Source: Open-Meteo current_weather + hourly (free, no key needed)
# =========================================================
def fmt_temp(value):
    """
    Rounds a temperature to the nearest whole degree as a string, without
    the "-0" that f"{x:.0f}" produces for small negative values (e.g.
    -0.3 formats to "-0" with plain string formatting, since rounding
    happens inside the format call on the float -0.0; round()-ing first
    to a plain int sidesteps that, since int 0 has no sign).
    """
    if value is None:
        return "—"
    r = round(value)
    if r == 0:
        r = 0
    return str(int(r))


def degrees_to_compass(deg):
    """Converts wind direction in degrees to a 16-point compass label."""
    if deg is None:
        return None
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(deg / 22.5) % 16
    return directions[idx]


def get_weather(lat, lon):
    """
    Fetches current weather conditions for the given coordinates.
    Returns a dict with temperature_c, windspeed_kmh, winddirection_deg,
    weathercode, humidity_pct, pressure_hpa, hourly_wind_forecast, and
    status ("ok" or "missing").
    """
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": True,
            "hourly": "relativehumidity_2m,pressure_msl,windspeed_10m,winddirection_10m",
            "timezone": "UTC",
        }
        r = get_with_retry(url, params=params, timeout=20, retries=1, backoff_seconds=5)
        data = r.json()

        cw = data["current_weather"]
        current_time = cw["time"]  # e.g. "2026-06-23T07:15" — can be off the hour

        # current_weather's timestamp can fall on a quarter-hour, but the
        # hourly arrays are always on the hour — round down before matching,
        # or exact-match .index() fails whenever the minutes aren't ":00".
        current_dt = datetime.strptime(current_time, "%Y-%m-%dT%H:%M")
        current_hour = current_dt.replace(minute=0).strftime("%Y-%m-%dT%H:%M")

        humidity = None
        pressure = None
        hourly_wind_forecast = None
        try:
            idx = data["hourly"]["time"].index(current_hour)
            humidity = data["hourly"]["relativehumidity_2m"][idx]
            pressure = data["hourly"]["pressure_msl"][idx]
            # Slice the next 48h of wind forecast starting from now, for
            # the Wind card's compact forecast chart — reuses this same
            # request rather than a separate API call.
            hourly_wind_forecast = {
                "time": data["hourly"]["time"][idx:idx+49],
                "windspeed_10m": data["hourly"]["windspeed_10m"][idx:idx+49],
                "winddirection_10m": data["hourly"]["winddirection_10m"][idx:idx+49],
            }
        except (ValueError, KeyError, IndexError) as e:
            print("WEATHER: could not align hourly index:", e)

        return {
            "temperature_c": cw.get("temperature"),
            "windspeed_kmh": cw.get("windspeed"),
            "winddirection_deg": cw.get("winddirection"),
            "weathercode": cw.get("weathercode"),
            "humidity_pct": humidity,
            "pressure_hpa": pressure,
            "hourly_wind_forecast": hourly_wind_forecast,
            "status": "ok",
        }
    except Exception as e:
        print("WEATHER FETCH FAILED:", e)
        return {
            "temperature_c": None,
            "windspeed_kmh": None,
            "winddirection_deg": None,
            "weathercode": None,
            "humidity_pct": None,
            "pressure_hpa": None,
            "hourly_wind_forecast": None,
            "status": "missing",
        }


# =========================================================
# MODULE — WEATHER PICTOGRAMS
# Draws simple icons matching the current WMO weathercode (returned by
# Open-Meteo's current_weather) rather than depending on an external icon
# server staying available — self-contained drawing is more robust than
# a third-party image URL.
#
# WMO weathercode reference (subset relevant to Arctic conditions):
# 0-1: clear/mainly clear, 2: partly cloudy, 3: cloudy, 45/48: fog,
# 51-57: drizzle, 61-67: rain, 71-77: snow, 80-82: showers,
# 85-86: snow showers, 95-99: thunderstorm
# =========================================================
from PIL import Image, ImageDraw, ImageFont

NOTION_ICON_SIZE = 140


def _icon_new_canvas():
    return Image.new("RGBA", (NOTION_ICON_SIZE, NOTION_ICON_SIZE), (0, 0, 0, 0))


def _icon_cloud_bumps(r):
    return [(-1.4, 0.1, 0.8), (-0.5, -0.5, 1.0), (0.5, -0.45, 1.05),
            (1.4, 0.1, 0.75), (-0.9, 0.3, 0.85), (0.9, 0.3, 0.85)]


def _icon_cloud_with_shadow(cx, cy, r, fill, highlight=None):
    """
    Builds a cloud shape with a soft drop shadow and a subtle highlight on
    the upper lobes, for a gentler, more dimensional look than a flat
    single-color fill.
    """
    from PIL import ImageFilter

    img = _icon_new_canvas()
    bumps = _icon_cloud_bumps(r)

    shadow = _icon_new_canvas()
    sd = ImageDraw.Draw(shadow)
    for dx, dy, s in bumps:
        rr = r * s
        sd.ellipse([cx + dx * r - rr, cy + dy * r - rr + 4, cx + dx * r + rr, cy + dy * r + rr + 4], fill=(0, 0, 0, 55))
    shadow = shadow.filter(ImageFilter.GaussianBlur(4))
    img = Image.alpha_composite(img, shadow)

    draw = ImageDraw.Draw(img)
    for dx, dy, s in bumps:
        rr = r * s
        draw.ellipse([cx + dx * r - rr, cy + dy * r - rr, cx + dx * r + rr, cy + dy * r + rr], fill=fill)
    if highlight:
        for dx, dy, s in [(-0.5, -0.5, 1.0), (0.5, -0.45, 1.05)]:
            rr = r * s * 0.55
            draw.ellipse([cx + dx * r - rr, cy + dy * r - rr - 3, cx + dx * r + rr, cy + dy * r + rr - 3], fill=highlight)

    return img


def _icon_sun(cx=None, cy=None, r=30):
    """Bold sun with tapered wedge rays and layered warm-gradient disk."""
    img = _icon_new_canvas()
    if cx is None:
        cx, cy = NOTION_ICON_SIZE // 2, NOTION_ICON_SIZE // 2
    draw = ImageDraw.Draw(img)

    # Tapered wedge rays — triangular polygons, far more graphic than lines
    ray_count = 12
    ray_inner = r + 5
    ray_outer = r + 22
    half_ang = math.pi / 20  # ~9° half-width at base
    for i in range(ray_count):
        a = i * 2 * math.pi / ray_count
        p1 = (cx + math.cos(a - half_ang) * ray_inner, cy + math.sin(a - half_ang) * ray_inner)
        p2 = (cx + math.cos(a + half_ang) * ray_inner, cy + math.sin(a + half_ang) * ray_inner)
        p3 = (cx + math.cos(a) * ray_outer, cy + math.sin(a) * ray_outer)
        draw.polygon([p1, p2, p3], fill=(255, 200, 50, 220))

    # Three concentric circles: warm outer → bright inner highlight
    for rad, color in [(r, (255, 183, 36, 255)), (r - 7, (255, 208, 65, 255)), (r - 16, (255, 240, 140, 255))]:
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=color)

    return img


def _icon_cloud(cx=None, cy=None, r=22, dark=False):
    if cx is None:
        cx, cy = NOTION_ICON_SIZE // 2, NOTION_ICON_SIZE // 2 + 5
    fill = (158, 165, 175, 255) if dark else (255, 255, 255, 255)
    highlight = (190, 196, 204, 255) if dark else (245, 248, 250, 255)
    return _icon_cloud_with_shadow(cx, cy, r, fill, highlight)


def _icon_partly_cloudy():
    sun = _icon_sun(cx=NOTION_ICON_SIZE // 2 - 15, cy=NOTION_ICON_SIZE // 2 - 15, r=18)
    cloud = _icon_cloud(cx=NOTION_ICON_SIZE // 2 + 13, cy=NOTION_ICON_SIZE // 2 + 15, r=18)
    return Image.alpha_composite(sun, cloud)


def _icon_rain(heavy=False):
    cx, cy = NOTION_ICON_SIZE // 2, NOTION_ICON_SIZE // 2 - 5
    r = 20
    fill = (148, 158, 172, 255) if heavy else (195, 202, 212, 255)
    highlight = (175, 182, 194, 255) if heavy else (220, 225, 232, 255)
    img = _icon_cloud_with_shadow(cx, cy, r, fill, highlight)
    draw = ImageDraw.Draw(img)
    offsets = [-18, -6, 6, 18] if heavy else [-13, 0, 13]
    drop_color = (55, 120, 210, 255)
    drop_glow  = (55, 120, 210, 80)
    for dx in offsets:
        bx = cx + dx
        # Glow halo around each teardrop
        draw.ellipse([bx - 7, cy + 18, bx + 7, cy + 39], fill=drop_glow)
        # Slender ellipse body (tall teardrop shape)
        draw.ellipse([bx - 4, cy + 20, bx + 4, cy + 37], fill=drop_color)
        # Rounded bottom cap for classic raindrop silhouette
        draw.ellipse([bx - 5, cy + 32, bx + 5, cy + 40], fill=drop_color)
    return img


def _icon_snow():
    cx, cy = NOTION_ICON_SIZE // 2, NOTION_ICON_SIZE // 2 - 5
    r = 20
    img = _icon_cloud_with_shadow(cx, cy, r, (255, 255, 255, 255), (240, 246, 252, 255))
    draw = ImageDraw.Draw(img)
    flake_color = (145, 190, 225, 255)
    for fx, fy in [(-14, 26), (0, 33), (14, 26)]:
        fcx, fcy = cx + fx, cy + fy
        arm = 7
        for i in range(3):
            a = i * math.pi / 3
            for sign in (1, -1):
                ex = fcx + math.cos(a) * arm * sign
                ey = fcy + math.sin(a) * arm * sign
                draw.line([(fcx, fcy), (ex, ey)], fill=flake_color, width=2)
                # Two secondary ticks per arm segment
                for frac in (0.42, 0.78):
                    mx = fcx + math.cos(a) * arm * frac * sign
                    my = fcy + math.sin(a) * arm * frac * sign
                    for side in (1, -1):
                        bx = mx + math.cos(a + side * math.pi / 3) * 3
                        by = my + math.sin(a + side * math.pi / 3) * 3
                        draw.line([(mx, my), (bx, by)], fill=flake_color, width=1)
    return img


def _icon_fog():
    img = _icon_new_canvas()
    draw = ImageDraw.Draw(img)
    cx, cy = NOTION_ICON_SIZE // 2, NOTION_ICON_SIZE // 2
    for i, (dy, w) in enumerate([(-20, 30), (-6, 36), (8, 32), (22, 26)]):
        alpha = max(110, 210 - i * 30)
        c = (125 + i * 8, 140 + i * 5, 162, alpha)
        r = 5  # end-cap radius
        # Filled rectangle for the body
        draw.rectangle([cx - w, cy + dy - r, cx + w, cy + dy + r], fill=c)
        # Round caps at each end
        draw.ellipse([cx - w - r, cy + dy - r, cx - w + r, cy + dy + r], fill=c)
        draw.ellipse([cx + w - r, cy + dy - r, cx + w + r, cy + dy + r], fill=c)
    return img


def _icon_thunder():
    cx, cy = NOTION_ICON_SIZE // 2, NOTION_ICON_SIZE // 2 - 10
    r = 20
    img = _icon_cloud_with_shadow(cx, cy, r, (125, 135, 150, 255), (155, 163, 175, 255))
    draw = ImageDraw.Draw(img)
    pts = [(cx - 4, cy + 15), (cx + 9, cy + 15), (cx, cy + 30), (cx + 11, cy + 30), (cx - 5, cy + 48)]
    # Wide amber glow
    draw.line(pts, fill=(255, 215, 40, 75), width=16, joint="curve")
    # Mid glow
    draw.line(pts, fill=(255, 220, 60, 150), width=10, joint="curve")
    # Sharp bright core
    draw.line(pts, fill=(255, 240, 100, 255), width=5, joint="curve")
    return img


def _icon_wind_arrow(direction_from_deg, speed_kmh):
    """
    Draws a wind direction arrow on a compass-rose target background.
    The target (concentric rings + N/S/E/W labels) gives immediate
    directional context. Arrow points toward wind destination (180° from
    meteorological "from" convention). Color follows the plasma colormap
    at 0–40 km/h, consistent with the 30-day wind rose.
    """
    from PIL import ImageFilter

    size = NOTION_ICON_SIZE
    cx, cy = size // 2, size // 2
    r_outer = size // 2 - 6

    img = _icon_new_canvas()  # transparent background
    draw = ImageDraw.Draw(img)

    # ---- Compass target background ----
    # Match Notion text-gray (#787774) used for chart axes in the same block
    _g = (120, 119, 116)
    ring_color  = _g + (110,)
    tick_color  = _g + (140,)
    label_color = _g + (220,)

    # Outer circle fills the canvas
    draw.ellipse(
        [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
        outline=ring_color, width=1,
    )
    # Middle ring
    r_mid = r_outer * 2 // 3
    draw.ellipse(
        [cx - r_mid, cy - r_mid, cx + r_mid, cy + r_mid],
        outline=ring_color, width=1,
    )
    # Inner ring
    r_inner = r_outer // 3
    draw.ellipse(
        [cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner],
        outline=ring_color, width=1,
    )

    # Cardinal and intercardinal tick marks on the outer ring
    for deg in range(0, 360, 22):
        is_cardinal = deg % 90 == 0
        tick_len = 8 if is_cardinal else 4
        rad = math.radians(deg)
        x0 = cx + math.sin(rad) * (r_outer - tick_len)
        y0 = cy - math.cos(rad) * (r_outer - tick_len)
        x1 = cx + math.sin(rad) * r_outer
        y1 = cy - math.cos(rad) * r_outer
        draw.line([(x0, y0), (x1, y1)], fill=tick_color, width=2 if is_cardinal else 1)

    # N / S / E / W labels
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    label_r = r_outer - 18
    for label, deg in [("N", 0), ("E", 90), ("S", 180), ("W", 270)]:
        rad = math.radians(deg)
        lx = cx + math.sin(rad) * label_r
        ly = cy - math.cos(rad) * label_r
        bbox = draw.textbbox((0, 0), label, font=font)
        lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((lx - lw / 2, ly - lh / 2), label, font=font, fill=label_color)

    # ---- Arrow ----
    import matplotlib
    _norm_speed = max(0, min(speed_kmh, 40)) / 40
    _plasma_rgba = matplotlib.colormaps["plasma"](_norm_speed)
    color = tuple(round(c * 255) for c in _plasma_rgba[:3]) + (255,)

    arrow_len = int((r_inner + 4) * 1.25)  # 25% longer than base
    angle_rad = math.radians(direction_from_deg + 180)
    dx = math.sin(angle_rad) * arrow_len
    dy = -math.cos(angle_rad) * arrow_len
    tail = (cx - dx, cy - dy)
    tip  = (cx + dx, cy + dy)

    shadow = _icon_new_canvas()
    sd = ImageDraw.Draw(shadow)
    sd.line([tail, tip], fill=(0, 0, 0, 60), width=20)
    shadow = shadow.filter(ImageFilter.GaussianBlur(4))
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)

    head_len = 24
    head_angle = math.radians(28)
    back_angle = angle_rad + math.pi
    shaft_end = (tip[0] + head_len * 0.6 * math.sin(back_angle),
                 tip[1] - head_len * 0.6 * math.cos(back_angle))
    draw.line([tail, shaft_end], fill=color, width=16)

    left  = (tip[0] + head_len * math.sin(back_angle + head_angle),
             tip[1] - head_len * math.cos(back_angle + head_angle))
    right = (tip[0] + head_len * math.sin(back_angle - head_angle),
             tip[1] - head_len * math.cos(back_angle - head_angle))
    draw.polygon([tip, left, right], fill=color)

    return img


def render_wind_icon(direction_from_deg, speed_kmh):
    """
    Renders the wind direction/strength arrow as PNG bytes, or None if
    rendering fails for any reason (so a drawing bug never blocks the
    rest of the dashboard from updating).
    """
    try:
        import io as _io
        img = _icon_wind_arrow(direction_from_deg, speed_kmh)
        out_buf = _io.BytesIO()
        img.save(out_buf, format="PNG")
        return out_buf.getvalue()
    except Exception as e:
        print("WIND ICON RENDER FAILED:", e)
        return None


def render_icon_with_big_number(icon_bytes, number_text, unit_text, icon_size=90, number_color=(40, 40, 40)):
    """
    Combines a small icon on the left with a large number + unit on the
    right, rendered as a single image, cropped TIGHTLY to actual content
    rather than a fixed oversized canvas.

    Why tight cropping matters: Notion has no per-block image width
    control — it always scales the whole image to fill the available
    column width. A canvas with a lot of empty transparent margin around
    the real content (icon + number) meant that margin got scaled along
    with everything else, so the actual number ended up much smaller on
    screen than its own font size would suggest, especially in a
    half-width column. Cropping to content means nearly every pixel of
    the final image is meaningful, so the same display width shows
    dramatically larger text.

    number_color: RGB tuple for the big number specifically, letting
    callers color-code it (e.g. by temperature or wind force) while the
    unit text stays a neutral gray.

    Returns PNG bytes, or None on failure.
    """
    try:
        import io as _io

        icon = Image.open(_io.BytesIO(icon_bytes)).convert("RGBA") if icon_bytes else None

        try:
            font_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
            font_unit = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
        except Exception:
            font_big = font_unit = ImageFont.load_default()

        # Measure actual text size first, on a throwaway canvas, so the
        # real canvas can be sized exactly to fit (plus small margins).
        _tmp = Image.new("RGBA", (10, 10))
        _tmp_draw = ImageDraw.Draw(_tmp)
        num_bbox = _tmp_draw.textbbox((0, 0), number_text, font=font_big)
        num_w, num_h = num_bbox[2] - num_bbox[0], num_bbox[3] - num_bbox[1]
        unit_bbox = _tmp_draw.textbbox((0, 0), unit_text, font=font_unit)
        unit_w, unit_h = unit_bbox[2] - unit_bbox[0], unit_bbox[3] - unit_bbox[1]

        margin = 8
        gap_icon_text = 14
        gap_num_unit = 6

        canvas_h = max(icon_size, num_h) + margin * 2
        canvas_w = margin + icon_size + gap_icon_text + num_w + gap_num_unit + unit_w + margin
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

        if icon:
            icon_resized = icon.resize((icon_size, icon_size), Image.LANCZOS)
            paste_y = (canvas_h - icon_size) // 2
            canvas.paste(icon_resized, (margin, paste_y), icon_resized)

        draw = ImageDraw.Draw(canvas)
        text_x = margin + icon_size + gap_icon_text
        text_y = (canvas_h - num_h) // 2 - num_bbox[1]
        draw.text((text_x, text_y), number_text, font=font_big, fill=number_color + (255,))

        unit_x = text_x + num_w + gap_num_unit
        unit_y = text_y + num_h - unit_h + num_bbox[1] - unit_bbox[1] - 2
        draw.text((unit_x, unit_y), unit_text, font=font_unit, fill=(90, 90, 90, 255))

        out_buf = _io.BytesIO()
        canvas.save(out_buf, format="PNG")
        return out_buf.getvalue()

    except Exception as e:
        print("ICON WITH BIG NUMBER RENDER FAILED:", e)
        return None


def temperature_to_color(temp_c):
    """
    Maps a temperature in Celsius to a cold-to-hot color gradient. There
    is no single official WMO color standard for temperature display
    (confirmed — meteorological organizations each use their own
    convention), so this follows the common, widely-used blue-to-red
    convention rather than claiming a specific authoritative standard.
    """
    if temp_c is None:
        return (40, 40, 40)
    stops = [
        (-30, (84, 130, 217)),
        (-15, (107, 174, 230)),
        (0, (140, 200, 230)),
        (10, (90, 160, 110)),
        (20, (220, 170, 50)),
        (30, (220, 100, 50)),
        (40, (180, 40, 40)),
    ]
    if temp_c <= stops[0][0]:
        return stops[0][1]
    if temp_c >= stops[-1][0]:
        return stops[-1][1]
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t0 <= temp_c <= t1:
            frac = (temp_c - t0) / (t1 - t0)
            return tuple(round(c0[j] + (c1[j] - c0[j]) * frac) for j in range(3))
    return (40, 40, 40)


def windspeed_to_beaufort_color(speed_kmh):
    """
    Maps a wind speed in km/h to a color based on the Beaufort wind force
    scale — a real, internationally standardized scale (defined by the
    WMO, with identical speed-range definitions worldwide; only the
    preferred display unit varies by country). Color progresses from
    pale (calm) through yellow/orange (gale) to dark red (storm+).
    Returns (color_rgb_tuple, beaufort_description).
    """
    if speed_kmh is None:
        return (40, 40, 40), "—"
    scale = [
        (1, (180, 200, 215), "Calm"),
        (5, (150, 190, 210), "Light air"),
        (11, (120, 180, 190), "Light breeze"),
        (19, (100, 170, 140), "Gentle breeze"),
        (28, (140, 170, 80), "Moderate breeze"),
        (38, (200, 170, 50), "Fresh breeze"),
        (49, (220, 140, 40), "Strong breeze"),
        (61, (220, 100, 40), "Moderate gale"),
        (74, (200, 60, 40), "Gale"),
        (88, (170, 40, 40), "Strong gale"),
        (102, (140, 20, 60), "Storm"),
        (117, (110, 10, 80), "Violent storm"),
        (9999, (80, 0, 80), "Hurricane force"),
    ]
    for max_kmh, color, label in scale:
        if speed_kmh <= max_kmh:
            return color, label
    return scale[-1][1], scale[-1][2]


def render_weather_icon(weathercode):
    """
    Renders a small PNG icon matching the given WMO weathercode.
    Returns PNG bytes, or None if rendering fails for any reason (so a
    drawing bug never blocks the rest of the dashboard from updating).
    """
    try:
        import io as _io

        code = weathercode if weathercode is not None else -1

        if code in (0, 1):
            img = _icon_sun()
        elif code == 2:
            img = _icon_partly_cloudy()
        elif code == 3:
            img = _icon_cloud()
        elif code in (45, 48):
            img = _icon_fog()
        elif code in (51, 53, 55, 56, 57):
            img = _icon_rain(heavy=False)
        elif code in (61, 63, 65, 66, 67):
            img = _icon_rain(heavy=(code in (65, 67)))
        elif code in (71, 73, 75, 77):
            img = _icon_snow()
        elif code in (80, 81, 82):
            img = _icon_rain(heavy=(code == 82))
        elif code in (85, 86):
            img = _icon_snow()
        elif code in (95, 96, 99):
            img = _icon_thunder()
        else:
            # Unrecognized code: fall back to a plain cloud rather than
            # guessing, since an unknown code shouldn't be shown as sunny.
            img = _icon_cloud()

        out_buf = _io.BytesIO()
        img.save(out_buf, format="PNG")
        return out_buf.getvalue()

    except Exception as e:
        print("WEATHER ICON RENDER FAILED:", e)
        return None


def weathercode_to_emoji(code):
    """
    Maps a WMO weathercode to a representative emoji, using the same code
    groupings as render_weather_icon, for use in contexts where an actual
    image can't be embedded — e.g. Notion table cells, which only support
    rich text, not nested image blocks.
    """
    if code is None:
        return "—"
    if code in (0, 1):
        return "â˜€ï¸"
    elif code == 2:
        return "ðŸŒ¤ï¸"
    elif code == 3:
        return "â˜ï¸"
    elif code in (45, 48):
        return "ðŸŒ«ï¸"
    elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "ðŸŒ§ï¸"
    elif code in (71, 73, 75, 77, 85, 86):
        return "â„ï¸"
    elif code in (95, 96, 99):
        return "â›ˆï¸"
    else:
        return "â˜ï¸"


def build_mini_forecast_strip(days_data):
    """
    Renders a compact horizontal strip: one small weather icon per day,
    with a day label above and a temperature range below — a scannable
    visual summary for the Weather card, reusing the same icon family as
    the rest of the dashboard rather than introducing a new visual style.

    days_data: list of dicts with 'day_label', 'weathercode', 'temp_min',
    'temp_max' keys. Returns PNG bytes, or None on failure.
    """
    try:
        import io as _io

        n = len(days_data)
        cell_w, cell_h = 110, 175
        canvas = Image.new("RGBA", (cell_w * n, cell_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        try:
            font_day = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26)
            font_temp = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        except Exception:
            font_day = font_temp = ImageFont.load_default()

        for i, d in enumerate(days_data):
            x0 = i * cell_w
            icon_bytes = render_weather_icon(d["weathercode"])
            if icon_bytes:
                icon_img = Image.open(_io.BytesIO(icon_bytes)).convert("RGBA")
                icon_img = icon_img.resize((76, 76), Image.LANCZOS)
                canvas.paste(icon_img, (x0 + (cell_w - 76) // 2, 42), icon_img)

            day_label = d["day_label"]
            temp_label = f"{fmt_temp(d['temp_min'])}–{fmt_temp(d['temp_max'])}°"

            day_bbox = draw.textbbox((0, 0), day_label, font=font_day)
            draw.text((x0 + (cell_w - (day_bbox[2] - day_bbox[0])) // 2, 4), day_label, font=font_day, fill=(40, 40, 40))

            temp_bbox = draw.textbbox((0, 0), temp_label, font=font_temp)
            draw.text((x0 + (cell_w - (temp_bbox[2] - temp_bbox[0])) // 2, 128), temp_label, font=font_temp, fill=(60, 60, 60))

        out_buf = _io.BytesIO()
        canvas.save(out_buf, format="PNG")
        return out_buf.getvalue()

    except Exception as e:
        print("MINI FORECAST STRIP RENDER FAILED:", e)
        return None


def build_large_forecast_strip(days_data):
    """
    A larger, more detailed version of build_mini_forecast_strip, sized
    for the full-width 5-day forecast detail section rather than a
    half-width card. Notion table cells have no font-size control, which
    is why a previous emoji-in-table approach couldn't be made bigger no
    matter how the table itself was sized — rendering real icon images
    at a larger fixed size sidesteps that limitation entirely.

    days_data: list of dicts with 'day_label', 'weathercode', 'temp_min',
    'temp_max', 'wind_label', 'precip_label' keys.
    Returns PNG bytes, or None on failure.
    """
    try:
        import io as _io

        n = len(days_data)
        cell_w, cell_h = 170, 230
        canvas = Image.new("RGBA", (cell_w * n, cell_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)

        try:
            font_day = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            font_temp = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
            font_detail = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        except Exception:
            font_day = font_temp = font_detail = ImageFont.load_default()

        icon_size = 110

        for i, d in enumerate(days_data):
            x0 = i * cell_w
            icon_bytes = render_weather_icon(d["weathercode"])
            if icon_bytes:
                icon_img = Image.open(_io.BytesIO(icon_bytes)).convert("RGBA")
                icon_img = icon_img.resize((icon_size, icon_size), Image.LANCZOS)
                canvas.paste(icon_img, (x0 + (cell_w - icon_size) // 2, 36), icon_img)

            day_label = d["day_label"]
            day_bbox = draw.textbbox((0, 0), day_label, font=font_day)
            draw.text((x0 + (cell_w - (day_bbox[2] - day_bbox[0])) // 2, 4), day_label, font=font_day, fill=(50, 50, 50))

            temp_label = f"{fmt_temp(d['temp_min'])}–{fmt_temp(d['temp_max'])}°"
            temp_bbox = draw.textbbox((0, 0), temp_label, font=font_temp)
            draw.text((x0 + (cell_w - (temp_bbox[2] - temp_bbox[0])) // 2, 152), temp_label, font=font_temp, fill=(40, 40, 40))

            for j, detail_label in enumerate([d.get("wind_label", ""), d.get("precip_label", "")]):
                detail_bbox = draw.textbbox((0, 0), detail_label, font=font_detail)
                draw.text((x0 + (cell_w - (detail_bbox[2] - detail_bbox[0])) // 2, 184 + j * 22), detail_label, font=font_detail, fill=(90, 90, 90))

        out_buf = _io.BytesIO()
        canvas.save(out_buf, format="PNG")
        return out_buf.getvalue()

    except Exception as e:
        print("LARGE FORECAST STRIP RENDER FAILED:", e)
        return None


def build_wind_forecast_mini_chart(hourly_wind_forecast):
    """
    Builds a compact 48h wind speed forecast chart for the Wind card,
    sized for a half-width column rather than the full-width 30-day
    historical vector chart elsewhere on the page. Uses data already
    fetched as part of get_weather's existing hourly request — no
    separate API call. Returns (png_bytes, caption).
    """
    if not hourly_wind_forecast or not hourly_wind_forecast.get("time"):
        return None, "Wind forecast unavailable."

    try:
        times = hourly_wind_forecast["time"]
        speeds = hourly_wind_forecast["windspeed_10m"]
        hours = list(range(len(times)))

        NOTION_BLUE = "#337EA9"
        NOTION_TEXT_GRAY = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(4.2, 2.4), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        ax.fill_between(hours, speeds, 0, color=NOTION_BLUE, alpha=0.15, linewidth=0)
        ax.plot(hours, speeds, color=NOTION_BLUE, linewidth=3)

        NOTION_RED = "#E16259"
        ax.plot([0], [speeds[0]], marker="o", markersize=10,
                color=NOTION_RED, markeredgecolor="white", markeredgewidth=1.5, zorder=5)
        x_offset = (max(hours) if hours else 48) * 0.04
        ax.annotate("now", xy=(0, speeds[0]), xytext=(x_offset, speeds[0]),
                    color=NOTION_RED, fontsize=16, fontweight="bold", ha="left", va="center",
                    bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8))

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        max_h = max(hours) if hours else 48
        tick_positions = [h for h in [0, 12, 24, 36, 48] if h <= max_h]
        tick_labels = ["now" if h == 0 else f"+{h}h" for h in tick_positions]
        ax.set_xticks(tick_positions)
        ax.set_xticklabels(tick_labels, fontsize=16, color=NOTION_TEXT_GRAY)
        ax.tick_params(axis="y", labelsize=16, colors=NOTION_TEXT_GRAY, length=0)
        ax.tick_params(axis="x", length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1)
        ax.set_axisbelow(True)
        ax.set_ylabel("km/h", fontsize=17, color=NOTION_TEXT_GRAY)
        ax.set_ylim(0, max(speeds) * 1.2 if speeds else 10)

        fig.tight_layout()
        png_bytes = fig_to_png_bytes(fig)
        caption = "Wind speed, next 48h. Source: Open-Meteo."
        return png_bytes, caption

    except Exception as e:
        print("WIND FORECAST MINI CHART FAILED:", e)
        return None, "Wind forecast chart could not be generated."


# =========================================================
# MODULE — LAND WEATHER FORECAST (next N days)
# =========================================================
def get_land_forecast(lat, lon, days=5):
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max,winddirection_10m_dominant,precipitation_sum,precipitation_probability_max,weathercode",
            "forecast_days": days,
            "timezone": "UTC",
        }
        r = get_with_retry(url, params=params, timeout=20, retries=1, backoff_seconds=5)
        data = r.json()
        daily = data.get("daily", {})

        days_list = []
        for i, day_str in enumerate(daily.get("time", [])):
            days_list.append({
                "date": day_str,
                "temp_max": daily["temperature_2m_max"][i],
                "temp_min": daily["temperature_2m_min"][i],
                "wind_max_kmh": daily["windspeed_10m_max"][i],
                "wind_dir_deg": daily["winddirection_10m_dominant"][i],
                "precip_mm": daily["precipitation_sum"][i],
                "precip_prob_pct": daily.get("precipitation_probability_max", [None]*len(daily.get("time", [])))[i],
                "weathercode": daily.get("weathercode", [None]*len(daily.get("time", [])))[i],
            })
        return days_list
    except Exception as e:
        print("LAND FORECAST FETCH FAILED:", e)
        return []


# =========================================================
# MODULE — GEM/GDPS FORECAST (ECCC via Open-Meteo)
# Primary source for forecast strips and wind chart. Uses Open-Meteo's
# gem_seamless model (HRDPS blended → GDPS), which gives 10-day GDPS
# coverage with higher-resolution HRDPS data for the first ~48h
# automatically — without any visual distinction needed in the output.
# Falls back to Open-Meteo best_match (ECMWF) if gem_seamless fails.
# =========================================================

def fetch_gem_forecast(lat, lon, now_utc, tz_name="UTC"):
    """
    Returns dict with 'hourly', 'daily', 'source' keys, or None if both
    GEM and the ECMWF fallback fail.
    tz_name must be the site's IANA timezone (e.g. "America/Inuvik") so that
    daily min/max aggregation in the API matches local calendar days.
    """
    hourly_vars = "temperature_2m,windspeed_10m,winddirection_10m,pressure_msl,precipitation,rain"
    daily_vars  = "weathercode,temperature_2m_max,temperature_2m_min,windspeed_10m_max,winddirection_10m_dominant,precipitation_sum"

    for model in ("gem_seamless", "best_match"):
        try:
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&hourly={hourly_vars}"
                f"&daily={daily_vars}"
                f"&models={model}&forecast_days=10&timezone={tz_name}&windspeed_unit=kmh"
            )
            resp = get_with_retry(url, timeout=20, retries=1, backoff_seconds=5)
            data = resp.json()
            h = data.get("hourly", {})
            d = data.get("daily",  {})
            if not h.get("time") or not d.get("time"):
                print(f"GEM FORECAST [{model}]: empty response, trying fallback")
                continue
            print(f"GEM FORECAST: {len(h['time'])} hourly steps via {model}")
            return {
                "source": model,
                "hourly": {
                    "time":          h["time"],
                    "temperature":   h.get("temperature_2m", []),
                    "windspeed":     h.get("windspeed_10m", []),
                    "winddirection": h.get("winddirection_10m", []),
                    "pressure":      h.get("pressure_msl", []),
                    "precipitation": h.get("precipitation", []),
                    "rain":          h.get("rain", []),
                },
                "daily": {
                    "dates":       d["time"],
                    "weathercode": d.get("weathercode", []),
                    "temp_max":    d.get("temperature_2m_max", []),
                    "temp_min":    d.get("temperature_2m_min", []),
                    "windspeed":   d.get("windspeed_10m_max", []),
                    "winddir":     d.get("winddirection_10m_dominant", []),
                    "precip":      d.get("precipitation_sum", []),
                },
            }
        except Exception as e:
            print(f"GEM FORECAST [{model}] FAILED: {e}")
    return None


def gem_daily_to_land_forecast_days(daily):
    """
    Converts GEM daily dict → the list-of-dicts format expected by
    build_large_forecast_strip / build_mini_forecast_strip.
    precipitation_probability_max is not available from GDPS (deterministic
    model), so precip_prob_pct is always None.
    """
    result = []
    wc  = daily.get("weathercode") or []
    tmax = daily.get("temp_max")    or []
    tmin = daily.get("temp_min")    or []
    wspd = daily.get("windspeed")   or []
    wdir = daily.get("winddir")     or []
    prcp = daily.get("precip")      or []
    for i, date_str in enumerate(daily.get("dates", [])):
        try:
            from datetime import date as _date
            dt = _date.fromisoformat(date_str)
            day_label = "Today" if i == 0 else dt.strftime("%a %-d")
        except Exception:
            day_label = date_str
        result.append({
            "date":           date_str,
            "day_label":      day_label,
            "weathercode":    wc[i]   if i < len(wc)   else None,
            "temp_max":       tmax[i] if i < len(tmax) else 0,
            "temp_min":       tmin[i] if i < len(tmin) else 0,
            "wind_max_kmh":   wspd[i] if i < len(wspd) else 0,
            "wind_dir_deg":   wdir[i] if i < len(wdir) else 0,
            "precip_mm":      prcp[i] if i < len(prcp) else 0,
            "precip_prob_pct": None,
        })
    return result


def gem_hourly_wind_forecast(hourly, now_utc, tz_name="UTC"):
    """
    Slices the next 48h of GEM hourly wind data starting from now,
    returning the same dict format that build_wind_forecast_mini_chart expects.
    Times in 'hourly' are in local time (tz_name), so we compare against
    the local representation of now_utc.
    """
    times = hourly.get("time", [])
    if not times:
        return None
    try:
        now_local = now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
        now_str = now_local.strftime("%Y-%m-%dT%H:00")
    except Exception:
        now_str = now_utc.strftime("%Y-%m-%dT%H:00")
    try:
        idx = next(i for i, t in enumerate(times) if t >= now_str)
    except StopIteration:
        idx = 0
    end = idx + 49
    return {
        "time":             times[idx:end],
        "windspeed_10m":    hourly.get("windspeed", [])[idx:end],
        "winddirection_10m": hourly.get("winddirection", [])[idx:end],
    }


# â”€â”€ Thin-outline SVG weather icons (Tabler style) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# cairosvg renders these to PNG for use in PIL strip images.

_GEM_SVG = {
    "sun": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round'>"
        "<circle cx='12' cy='12' r='4'/>"
        "<line x1='12' y1='2' x2='12' y2='5'/>"
        "<line x1='12' y1='19' x2='12' y2='22'/>"
        "<line x1='2' y1='12' x2='5' y2='12'/>"
        "<line x1='19' y1='12' x2='22' y2='12'/>"
        "<line x1='4.93' y1='4.93' x2='7.05' y2='7.05'/>"
        "<line x1='16.95' y1='16.95' x2='19.07' y2='19.07'/>"
        "<line x1='19.07' y1='4.93' x2='16.95' y2='7.05'/>"
        "<line x1='7.05' y1='16.95' x2='4.93' y2='19.07'/>"
        "</svg>"
    ),
    "partly_cloudy": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'>"
        "<circle cx='9' cy='9' r='3'/>"
        "<line x1='9' y1='2.5' x2='9' y2='4.5'/>"
        "<line x1='9' y1='13.5' x2='9' y2='14.5'/>"
        "<line x1='2.5' y1='9' x2='4.5' y2='9'/>"
        "<line x1='13.5' y1='9' x2='15' y2='9'/>"
        "<line x1='4.64' y1='4.64' x2='6.05' y2='6.05'/>"
        "<line x1='12.95' y1='12.95' x2='14.36' y2='14.36'/>"
        "<line x1='14.36' y1='4.64' x2='12.95' y2='6.05'/>"
        "<path d='M11 19a4 4 0 0 1-.5-7.9A5 5 0 1 1 19 16a3 3 0 0 1 .5 5H11z'/>"
        "</svg>"
    ),
    "cloud": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'>"
        "<path d='M6.657 18C4.085 18 2 15.993 2 13.517"
        " c0-2.475 1.982-4.482 4.573-4.482"
        " c.405-1.506 1.316-2.832 2.57-3.774"
        " A7.374 7.374 0 0 1 12 4"
        " a7.5 7.5 0 0 1 7.5 7.5"
        " c0 .05 0 .1-.003.15"
        " A4.5 4.5 0 0 1 19.5 20H7z'/>"
        "</svg>"
    ),
    "fog": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round'>"
        "<line x1='3' y1='8' x2='21' y2='8'/>"
        "<line x1='3' y1='12' x2='21' y2='12'/>"
        "<line x1='3' y1='16' x2='21' y2='16'/>"
        "</svg>"
    ),
    "drizzle": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'>"
        "<path d='M6.657 14C4.085 14 2 11.993 2 9.517"
        " c0-2.475 1.982-4.482 4.573-4.482"
        " c.405-1.506 1.316-2.832 2.57-3.774"
        " A7.374 7.374 0 0 1 12 0"
        " a7.5 7.5 0 0 1 7.5 7.5"
        " c0 .05 0 .1-.003.15"
        " A4.5 4.5 0 0 1 19.5 16H7z'/>"
        "<line x1='9' y1='20' x2='8' y2='22'/>"
        "<line x1='14' y1='20' x2='13' y2='22'/>"
        "</svg>"
    ),
    "rain": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'>"
        "<path d='M6.657 14C4.085 14 2 11.993 2 9.517"
        " c0-2.475 1.982-4.482 4.573-4.482"
        " c.405-1.506 1.316-2.832 2.57-3.774"
        " A7.374 7.374 0 0 1 12 0"
        " a7.5 7.5 0 0 1 7.5 7.5"
        " c0 .05 0 .1-.003.15"
        " A4.5 4.5 0 0 1 19.5 16H7z'/>"
        "<line x1='8' y1='19' x2='7' y2='22'/>"
        "<line x1='12' y1='19' x2='11' y2='22'/>"
        "<line x1='16' y1='19' x2='15' y2='22'/>"
        "</svg>"
    ),
    "snow": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'>"
        "<path d='M6.657 14C4.085 14 2 11.993 2 9.517"
        " c0-2.475 1.982-4.482 4.573-4.482"
        " c.405-1.506 1.316-2.832 2.57-3.774"
        " A7.374 7.374 0 0 1 12 0"
        " a7.5 7.5 0 0 1 7.5 7.5"
        " c0 .05 0 .1-.003.15"
        " A4.5 4.5 0 0 1 19.5 16H7z'/>"
        "<line x1='8' y1='19' x2='8' y2='22'/>"
        "<line x1='12' y1='19' x2='12' y2='22'/>"
        "<line x1='16' y1='19' x2='16' y2='22'/>"
        "<line x1='7' y1='20.5' x2='9' y2='20.5'/>"
        "<line x1='11' y1='20.5' x2='13' y2='20.5'/>"
        "<line x1='15' y1='20.5' x2='17' y2='20.5'/>"
        "</svg>"
    ),
    "thunder": (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none'"
        " stroke='{c}' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'>"
        "<path d='M6.657 14C4.085 14 2 11.993 2 9.517"
        " c0-2.475 1.982-4.482 4.573-4.482"
        " c.405-1.506 1.316-2.832 2.57-3.774"
        " A7.374 7.374 0 0 1 12 0"
        " a7.5 7.5 0 0 1 7.5 7.5"
        " c0 .05 0 .1-.003.15"
        " A4.5 4.5 0 0 1 19.5 16H7z'/>"
        "<polyline points='13 17 11 21 13 21 11 24'/>"
        "</svg>"
    ),
}


def _wmo_to_gem_svg(code, color="#555555"):
    """Map a WMO weather code to a GEM (Tabler-style) SVG icon string."""
    if code is None:
        key = "cloud"
    elif code == 0:
        key = "sun"
    elif code in (1, 2):
        key = "partly_cloudy"
    elif code == 3:
        key = "cloud"
    elif code in (45, 48):
        key = "fog"
    elif code in (51, 53, 55, 56, 57):
        key = "drizzle"
    elif code in (61, 63, 65, 66, 67, 80, 81, 82):
        key = "rain"
    elif code in (71, 73, 75, 77, 85, 86):
        key = "snow"
    elif code in (95, 96, 99):
        key = "thunder"
    else:
        key = "cloud"
    return _GEM_SVG[key].replace("{c}", color)


def _svg_to_pil(svg_str, size_px=72):
    """Render an SVG string to a PIL RGBA image via cairosvg."""
    try:
        import cairosvg
        import io as _io
        png = cairosvg.svg2png(bytestring=svg_str.encode(), output_width=size_px, output_height=size_px)
        return Image.open(_io.BytesIO(png)).convert("RGBA")
    except Exception as e:
        print(f"SVG RENDER FAILED: {e}")
        return Image.new("RGBA", (size_px, size_px), (0, 0, 0, 0))


def build_gem_day_strip(daily, tz_name, n_days=10):
    """
    Renders a 7-day forecast strip using the same PIL weather icons as the
    weather block. Returns PNG bytes, or None on failure.
    """
    try:
        import io as _io
        n = min(n_days, len(daily.get("dates", [])))
        if n == 0:
            return None

        icon_px  = 96          # render at 96 px (icons are 140 px internally, scaled down)
        cell_w   = 155
        cell_h   = 240         # taller canvas — plenty of room above and below icon
        icon_y   = 48          # top edge of icon within cell
        temp_y   = icon_y + icon_px + 12
        wind_y   = temp_y + 30

        canvas = Image.new("RGBA", (cell_w * n, cell_h), (0, 0, 0, 0))
        draw   = ImageDraw.Draw(canvas)

        try:
            font_day    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            font_temp   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            font_detail = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     17)
        except Exception:
            font_day = font_temp = font_detail = ImageFont.load_default()

        wc   = daily.get("weathercode") or []
        tmax = daily.get("temp_max")    or []
        tmin = daily.get("temp_min")    or []
        wspd = daily.get("windspeed")   or []
        wdir = daily.get("winddir")     or []

        TEXT_DARK = "#37352F"
        TEXT_GRAY = "#787774"
        SEP_COLOR = (220, 218, 215, 120)

        for i in range(n):
            x0      = i * cell_w
            code    = int(wc[i])   if i < len(wc)   and wc[i]   is not None else None
            t_max   = tmax[i]      if i < len(tmax) and tmax[i]  is not None else None
            t_min   = tmin[i]      if i < len(tmin) and tmin[i]  is not None else None
            wind_spd = wspd[i]     if i < len(wspd) and wspd[i]  is not None else None
            wind_dir = wdir[i]     if i < len(wdir) and wdir[i]  is not None else None

            date_str  = daily["dates"][i]
            dt        = datetime.strptime(date_str, "%Y-%m-%d")
            day_label = "Today" if i == 0 else dt.strftime("%a %-d")

            # Day name
            bb = draw.textbbox((0, 0), day_label, font=font_day)
            tw = bb[2] - bb[0]
            draw.text((x0 + (cell_w - tw) // 2, 12), day_label,
                      font=font_day, fill=TEXT_DARK if i == 0 else TEXT_GRAY)

            # Icon — same PIL renderer as the weather block
            icon_png = render_weather_icon(code)
            if icon_png:
                icon_img = Image.open(_io.BytesIO(icon_png)).convert("RGBA")
                icon_img = icon_img.resize((icon_px, icon_px), Image.LANCZOS)
            else:
                icon_img = Image.new("RGBA", (icon_px, icon_px), (0, 0, 0, 0))
            canvas.paste(icon_img, (x0 + (cell_w - icon_px) // 2, icon_y), icon_img)

            # Temp range
            if t_max is not None and t_min is not None:
                temp_label = f"{fmt_temp(t_min)}–{fmt_temp(t_max)}°"
                bb = draw.textbbox((0, 0), temp_label, font=font_temp)
                tw = bb[2] - bb[0]
                draw.text((x0 + (cell_w - tw) // 2, temp_y), temp_label,
                          font=font_temp, fill=TEXT_DARK)

            # Wind
            if wind_spd is not None:
                compass    = degrees_to_compass(wind_dir) or ""
                wind_label = f"{wind_spd:.0f} km/h {compass}".strip()
                bb = draw.textbbox((0, 0), wind_label, font=font_detail)
                tw = bb[2] - bb[0]
                draw.text((x0 + (cell_w - tw) // 2, wind_y), wind_label,
                          font=font_detail, fill=TEXT_GRAY)

            # Cell separator
            if i > 0:
                draw.line([(x0, 12), (x0, cell_h - 12)], fill=SEP_COLOR, width=1)

        out = _io.BytesIO()
        canvas.save(out, format="PNG")
        return png_on_white(out.getvalue())

    except Exception as e:
        print("GEM DAY STRIP FAILED:", e)
        return None


def _gem_chart(hours, values, color, ylabel, t0, bar=False, ymin=None, ymax=None):
    """
    Render one GEM forecast curve (or bar chart for precip) as PNG bytes.
    hours[0] == 0 corresponds to "now". t0 is a naive local datetime (the
    API returns times in the site's local timezone, not UTC).
    """
    try:
        # Strip trailing None/NaN/0.0 from line charts — GEM pads the end of the
        # 10-day window with 0.0, causing a cliff-drop. For bar charts (precipitation)
        # 0.0 is a valid "no rain" value so we leave those alone.
        import math as _math
        def _bad_tail(v):
            return v is None or (isinstance(v, float) and (_math.isnan(v) or v == 0.0))
        if not bar:
            while values and _bad_tail(values[-1]):
                values = values[:-1]
                hours  = hours[:-1]
        else:
            # Still strip trailing None/NaN for bar charts
            while values and (values[-1] is None or (isinstance(values[-1], float) and _math.isnan(values[-1]))):
                values = values[:-1]
                hours  = hours[:-1]

        NOTION_TEXT_GRAY  = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(8, 3.0), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        if bar:
            ax.bar(hours, values, color=color + "99", width=1.0, linewidth=0)
        else:
            ax.fill_between(hours, values, min(v for v in values if v is not None),
                            color=color, alpha=0.12, linewidth=0, zorder=1)
            ax.plot(hours, values, color=color, linewidth=2.5, zorder=2)

            # "now" dot at hour 0
            NOTION_RED = "#E16259"
            ax.plot([0], [values[0]], marker="o", markersize=9,
                    color=NOTION_RED, markeredgecolor="white", markeredgewidth=1.5, zorder=5)
            x_range = max(hours) if hours else 240
            ax.annotate("now", xy=(0, values[0]), xytext=(x_range * 0.02, values[0]),
                        color=NOTION_RED, fontsize=13, fontweight="bold", ha="left", va="center",
                        bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8))

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        ax.set_xlim(0, max(hours) if hours else 240)
        if ymin is not None:
            ax.set_ylim(bottom=ymin)
        if ymax is not None:
            ax.set_ylim(top=ymax)

        # x-ticks: one per local day. t0 is already a naive local datetime,
        # so t.hour == 0 correctly finds local midnight without any tz conversion.
        tick_hours, tick_labels, minor_tick_hours = [], [], []
        for h in hours:
            t = t0 + timedelta(hours=h)
            if t.hour == 0 or h == 0:
                tick_hours.append(h)
                tick_labels.append("now" if h == 0 else t.strftime("%b %d"))
            elif t.hour == 12:
                minor_tick_hours.append(h)
        ax.set_xticks(tick_hours)
        ax.set_xticklabels(tick_labels, fontsize=13, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
        ax.set_xticks(minor_tick_hours, minor=True)
        ax.tick_params(axis="x", which="minor", length=4, color="#555555", width=1.0, bottom=True, direction="out")
        ax.tick_params(axis="x", which="major", length=8, color="#555555", width=1.2, bottom=True, direction="out")
        ax.tick_params(axis="y", labelsize=13, colors=NOTION_TEXT_GRAY, length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
        ax.set_ylabel(ylabel, fontsize=13, color=NOTION_TEXT_GRAY)

        # Subtle midnight dividers — skip h=0 ("now"), draw one per day boundary.
        for h in tick_hours:
            if h > 0:
                ax.axvline(h, color=NOTION_TEXT_GRAY, linewidth=0.6, alpha=0.18, zorder=0.5)

        fig.tight_layout()
        png = fig_to_png_bytes(fig, white_bg=True)
        return png
    except Exception as e:
        print(f"GEM CHART FAILED ({ylabel}):", e)
        return None


def _gem_precip_chart(hours, rain_vals, snow_vals, t0):
    """
    Stacked bar chart for precipitation: snow (violet) on the bottom,
    rain (blue) on top. Legend appears only when any snow is present.
    Matches the styling of _gem_chart exactly.
    """
    try:
        import math as _math
        # Trim trailing all-zero hours from the tail (GEM pads the 10-day window)
        while hours and rain_vals[-1] == 0.0 and snow_vals[-1] == 0.0:
            hours = hours[:-1]; rain_vals = rain_vals[:-1]; snow_vals = snow_vals[:-1]
        if not hours:
            return None

        NOTION_BLUE       = "#337EA9"
        SNOW_COLOR        = "#A855F7"
        NOTION_TEXT_GRAY  = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        has_snow = any(v > 0.005 for v in snow_vals)

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(8, 3.0), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        # Snow bars — bottom segment
        ax.bar(hours, snow_vals, color=SNOW_COLOR + "99", width=1.0, linewidth=0, label="Snow / sleet")
        # Rain bars — stacked on top of snow
        ax.bar(hours, rain_vals, bottom=snow_vals, color=NOTION_BLUE + "99", width=1.0, linewidth=0, label="Rain")

        if has_snow:
            # Reorder so Rain appears first in legend
            handles, labels = ax.get_legend_handles_labels()
            ax.legend(handles[::-1], labels[::-1],
                      fontsize=11, frameon=False, loc="upper right",
                      handlelength=1.2, handleheight=0.8, handletextpad=0.4,
                      labelcolor=NOTION_TEXT_GRAY)

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        ax.set_xlim(0, max(hours))
        ax.set_ylim(bottom=0)

        tick_hours, tick_labels = [], []
        minor_tick_hours = []
        for h in hours:
            t = t0 + timedelta(hours=h)
            if t.hour == 0 or h == 0:
                tick_hours.append(h)
                tick_labels.append("now" if h == 0 else t.strftime("%b %d"))
            elif t.hour == 12:
                minor_tick_hours.append(h)
        ax.set_xticks(tick_hours)
        ax.set_xticklabels(tick_labels, fontsize=13, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
        ax.set_xticks(minor_tick_hours, minor=True)
        ax.tick_params(axis="x", which="minor", length=4, color="#555555", width=1.0, bottom=True, direction="out")
        ax.tick_params(axis="x", which="major", length=8, color="#555555", width=1.2, bottom=True, direction="out")
        ax.tick_params(axis="y", labelsize=13, colors=NOTION_TEXT_GRAY, length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
        ax.set_ylabel("Precipitation (mm/h)", fontsize=13, color=NOTION_TEXT_GRAY)

        for h in tick_hours:
            if h > 0:
                ax.axvline(h, color=NOTION_TEXT_GRAY, linewidth=0.6, alpha=0.18, zorder=0.5)

        fig.tight_layout()
        return fig_to_png_bytes(fig, white_bg=True)
    except Exception as e:
        print("GEM PRECIP CHART FAILED:", e)
        return None


def build_gem_forecast_charts(hourly, tz_name, now_utc=None):
    """
    Build the four GEM forecast curves (wind, temperature, pressure, precip).
    Slices all series to start from now_utc so the "now" dot aligns with
    the same moment as the wind mini-chart in the upper block.
    Returns (temp_bytes, wind_bytes, press_bytes, precip_bytes) — any may be None.
    """
    try:
        times = list(hourly.get("time", []))
        if not times:
            return None, None, None, None

        # Slice to start from the current hour (times are in local tz)
        idx = 0
        if now_utc:
            try:
                now_local = now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
                now_str = now_local.strftime("%Y-%m-%dT%H:00")
                idx = next((i for i, t in enumerate(times) if t >= now_str), 0)
            except Exception:
                pass
        times = times[idx:]

        def _safe(key):
            vals = (hourly.get(key) or [])[idx:]
            return [v if v is not None else 0.0 for v in vals]

        t0    = datetime.fromisoformat(times[0])      # naive local datetime
        hours = list(range(len(times)))               # 0, 1, 2, â€¦ hours from now

        temp_b  = _gem_chart(hours, _safe("temperature"), "#E8A838", "Temperature (°C)",  t0)
        wind_b  = _gem_chart(hours, _safe("windspeed"),   "#4F9768", "Wind speed (km/h)", t0, ymin=0)
        press_b = _gem_chart(hours, _safe("pressure"),    "#C07038", "Pressure (hPa)",    t0, ymin=990)

        # Separate rain from snow: rain = API "rain" field (liquid only);
        # snow = total precip minus rain (liquid equivalent of snow/sleet).
        rain_vals  = _safe("rain")
        total_vals = _safe("precipitation")
        snow_vals  = [max(0.0, round(t - r, 4)) for t, r in zip(total_vals, rain_vals)]
        precip_b   = _gem_precip_chart(hours[:], rain_vals[:], snow_vals[:], t0)
        return temp_b, wind_b, press_b, precip_b

    except Exception as e:
        print("GEM FORECAST CHARTS FAILED:", e)
        return None, None, None, None


# =========================================================
# MODULE — MARINE FORECAST (Environment Canada)
# Source: Environment Canada's Atom feed for a given marine zone. The
# feed returns natural-language forecast text per period (e.g. "Wind
# light becoming southeast 15 knots"), not structured numeric fields, so
# we display the text as published rather than trying to parse specific
# values out of free-form wording.
#
# zone_id and zone_name are site-specific (each site's config.py
# specifies its own marine zone — e.g. 16000/"Yukon Coast" for Herschel
# and Shingle Point; a future site like Tuktoyaktuk would use whichever
# zone covers it, found at weather.gc.ca/marine).
# =========================================================
def _strip_html_to_text(html_str):
    """
    Converts Environment Canada's HTML-formatted summary text into plain
    text suitable for a Notion paragraph: <br/> tags become newlines, and
    any other HTML tags are stripped using Python's built-in HTML parser
    rather than naive string replacement (more robust to whatever markup
    variations the feed actually contains).
    """
    if not html_str:
        return ""

    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

        def handle_starttag(self, tag, attrs):
            if tag.lower() == "br":
                self.parts.append("\n")

    parser = _TextExtractor()
    parser.feed(html_str)
    text = "".join(parser.parts)

    # Collapse repeated whitespace within lines, but preserve the
    # intentional newlines from <br/> tags.
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def get_marine_forecast(zone_id):
    try:
        import xml.etree.ElementTree as ET

        url = f"https://weather.gc.ca/rss/marine/{zone_id}_e.xml"
        # weather.gc.ca's RSS endpoints have shown occasional transient
        # connection timeouts (seen in practice on a real run), so this
        # uses the retry helper rather than a single unprotected attempt.
        r = get_with_retry(url, timeout=15, retries=2, backoff_seconds=5)

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.content)

        entries = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            title = _strip_html_to_text(title_el.text) if title_el is not None else ""
            summary = _strip_html_to_text(summary_el.text) if summary_el is not None else ""
            entries.append({"title": title, "summary": summary})
        return entries
    except Exception as e:
        print("MARINE FORECAST FETCH FAILED:", e)
        return []


def format_marine_forecast_text(marine_entries, zone_name, exclude_title_patterns=None):
    """
    Turns the raw entries from get_marine_forecast into display-ready
    lines (see build_bolded_lines) plus a source-attribution string.

    zone_name is used to strip the feed's own redundant "- <Zone Name>"
    suffix from entry titles (e.g. "Forecast for Today ... - Yukon
    Coast"), since the section heading already states the zone name —
    this must match whatever zone_name the site's section heading uses,
    or the suffix won't be recognized and stripped.
    """
    if not marine_entries:
        return "Marine forecast unavailable — fetch failed. Check Action logs.", ""

    # The feed mixes forecast periods with warnings/synopsis entries; show
    # the first several as-is, since titles already summarize each one
    # (e.g. "Wind", "Waves", "Extended Forecast", "Ice Forecast"). We bold
    # the section title (a clean label) but leave the forecaster's
    # free-form prose unbolded.
    import re

    def _strip_zone_suffix(text):
        return re.sub(rf"\s*[-–—]\s*{re.escape(zone_name)}\s*$", "", text, flags=re.IGNORECASE).strip()

    # The feed repeats an "Issued HH:MM AM/PM <timezone> <date>" line inside
    # every entry's summary. Extract it once and strip it from each
    # individual entry, rather than show the same timestamp repeatedly.
    issued_pattern = re.compile(r"Issued\s+\d{1,2}:\d{2}\s*[AP]M\s+\w+\s+\d{1,2}\s+\w+\s+\d{4}\.?", re.IGNORECASE)

    def _extract_and_strip_issued(text):
        match = issued_pattern.search(text)
        issued_text = match.group(0).strip() if match else None
        cleaned = issued_pattern.sub("", text).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned, issued_text

    # Filter entries whose titles contain any excluded substring (case-insensitive).
    # Used e.g. for Tuktoyaktuk zone 14600 which returns both "northern half"
    # and "southern half" entries — caller can exclude one subzone.
    if exclude_title_patterns:
        marine_entries = [
            e for e in marine_entries
            if not any(p.lower() in e["title"].lower() for p in exclude_title_patterns)
        ]

    lines = []
    issued_line = None
    for e in marine_entries[:6]:
        title = _strip_zone_suffix(e["title"].strip())
        summary = e["summary"].strip() if e["summary"] else ""
        summary, found_issued = _extract_and_strip_issued(summary)
        if found_issued and not issued_line:
            issued_line = found_issued
        if summary and summary != title:
            lines.append([("", title), ": ", summary])
        else:
            lines.append(title)

    if issued_line:
        lines.append(issued_line)

    source_text = f"Source: Environment Canada ({zone_name} marine zone)"
    return lines, source_text


# =========================================================
# MODULE — WEATHER & COASTAL FLOOD ALERTS (only shown if active)
# Environment Canada publishes a per-location Atom feed covering
# watches, warnings, and special statements — including coastal
# flooding alerts — for a specific lat/lon point. When nothing is
# active, the feed contains a single boilerplate entry with the
# well-documented wording "No watches or warnings in effect" — we treat
# that phrase as the reliable signal to show nothing, rather than guess
# from absence of entries alone.
# =========================================================
def _get_weather_alerts_nws(lat, lon):
    """NWS (NOAA) alerts for US locations via api.weather.gov."""
    try:
        url = f"https://api.weather.gov/alerts/active?point={lat},{lon}"
        nws_headers = {
            "User-Agent": "arctic-dashboard/1.0 (hugues.lantuit@awi.de)",
            "Accept": "application/geo+json",
        }
        r = requests.get(url, headers=nws_headers, timeout=15)
        r.raise_for_status()
        entries = []
        for feature in r.json().get("features", []):
            props = feature.get("properties", {})
            event = props.get("event", "")
            headline = props.get("headline") or props.get("description", "")[:300]
            link = props.get("id", "")
            entries.append({"title": event, "summary": headline, "link": link})
        return entries
    except Exception as e:
        print("NWS WEATHER ALERTS FETCH FAILED:", e)
        return None


def get_weather_alerts(lat, lon):
    # Auto-route to NWS for Alaska (lat > 54°N, lon west of 130°W).
    if lat > 54 and lon < -130:
        return _get_weather_alerts_nws(lat, lon)
    try:
        import xml.etree.ElementTree as ET

        url = f"https://weather.gc.ca/rss/alerts/{lat}_{lon}_e.xml"
        # Same retry treatment as the marine forecast fetch — weather.gc.ca
        # has shown transient connection timeouts in practice.
        r = get_with_retry(url, timeout=15, retries=2, backoff_seconds=5)

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(r.content)

        entries = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            link_el = entry.find("atom:link", ns)
            title = (title_el.text or "").strip() if title_el is not None else ""
            summary = _strip_html_to_text(summary_el.text) if summary_el is not None else ""
            link = link_el.get("href") if link_el is not None else None
            entries.append({"title": title, "summary": summary, "link": link})
        return entries
    except Exception as e:
        print("WEATHER ALERTS FETCH FAILED:", e)
        return None  # None (fetch failed) is distinct from [] (fetched OK, no entries)


def filter_active_alerts(weather_alert_entries):
    """Filters out the documented 'nothing active' boilerplate entry."""
    active_alerts = []
    if weather_alert_entries is not None:
        for e in weather_alert_entries:
            title_lower = e["title"].lower()
            if "no watches or warnings" in title_lower or "no alerts" in title_lower:
                continue
            active_alerts.append(e)
    return active_alerts


# =========================================================
# MODULE — SUN: sunrise, sunset, day length, elevation curve
# Source: sunrise-sunset.org for sunrise/sunset (free, no key); solar
# elevation computed directly from standard astronomical formulas, since
# this project's sites are typically above or near 69°N, where polar
# day/polar night are expected and need explicit handling.
# =========================================================
def get_sun_info(lat, lon):
    try:
        url = "https://api.sunrise-sunset.org/json"
        params = {"lat": lat, "lng": lon, "formatted": 0}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        print("SUN: raw response status:", data.get("status"))

        if data.get("status") != "OK":
            return {"status": "no_data", "raw_status": data.get("status")}

        results = data["results"]
        sunrise = datetime.fromisoformat(results["sunrise"].replace("Z", "+00:00"))
        sunset = datetime.fromisoformat(results["sunset"].replace("Z", "+00:00"))
        day_length_s = results.get("day_length")

        return {
            "status": "ok",
            "sunrise": sunrise,
            "sunset": sunset,
            "day_length_s": day_length_s,
        }
    except Exception as e:
        print("SUN FETCH FAILED:", e)
        return {"status": "error"}


def solar_elevation_deg(lat_deg, lon_deg, dt_utc):
    """
    Standard solar elevation angle formula (declination + hour angle).
    Returns elevation in degrees above the horizon (negative = below).
    Verified against known physical cases: ~90° at the equator/equinox
    noon, positive at high-latitude summer midnight (midnight sun),
    negative at high-latitude winter noon (polar night boundary).
    """
    lat = math.radians(lat_deg)
    day_of_year = dt_utc.timetuple().tm_yday
    hour_utc = dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600

    decl = math.radians(23.45 * math.sin(math.radians(360 / 365 * (day_of_year - 81))))
    b = math.radians(360 / 365 * (day_of_year - 81))
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)

    time_correction = 4 * lon_deg + eot  # minutes
    solar_time = hour_utc + time_correction / 60
    hour_angle = math.radians(15 * (solar_time - 12))

    elevation = math.asin(
        math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    )
    return math.degrees(elevation)


def classify_sun_text(sun_info, lat, lon, now, tz_name):
    """
    Builds the display text for sunrise/sunset/day-length, including
    polar day/polar night detection.

    At high latitudes, sunrise-sunset.org's reported day_length can
    behave unexpectedly during polar day (seen in practice returning ~0
    instead of ~24h, likely because its internal sunrise/sunset
    timestamps become degenerate when the sun never sets). Rather than
    guess at that API's internal edge-case behavior, polar day/night is
    classified directly using solar_elevation_deg (verified correct
    against known physical cases), which doesn't depend on day_length.
    """
    if sun_info["status"] == "ok":
        # Scan the full day at 15-minute resolution to find the true
        # minimum and maximum elevation — fixed clock-hour samples
        # aren't reliable, since the actual highest/lowest points of the
        # day are offset from UTC by this location's longitude.
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_elevations = [solar_elevation_deg(lat, lon, day_start + timedelta(minutes=15 * i)) for i in range(96)]
        min_elevation_today = min(day_elevations)
        max_elevation_today = max(day_elevations)

        tz = ZoneInfo(tz_name)
        if min_elevation_today > 0:
            return "Sun stays above the horizon all day (midnight sun) at this latitude."
        elif max_elevation_today < 0:
            return "Sun stays below the horizon all day (polar night) at this latitude."
        else:
            # sunrise-sunset.org returns degenerate values (sunrise == sunset,
            # day_length == 0) for near-polar-day/night conditions. Detect this
            # by checking if day_length is 0 or sunrise == sunset, and fall back
            # to estimates computed from our own elevation scan.
            day_length_s = sun_info["day_length_s"]
            api_degenerate = (not day_length_s) or (sun_info["sunrise"] == sun_info["sunset"])
            if api_degenerate:
                # Estimate sunrise/sunset from elevation scan zero-crossings.
                # Scan runs from midnight UTC; find last below→above (sunrise)
                # and last above→below (sunset) transition.
                above_minutes = sum(1 for e in day_elevations if e > 0) * 15
                dl_h = above_minutes // 60
                dl_m = above_minutes % 60
                sunrise_idx = next(
                    (i for i in range(1, len(day_elevations)) if day_elevations[i - 1] <= 0 and day_elevations[i] > 0),
                    None,
                )
                sunset_idx = next(
                    (i for i in range(len(day_elevations) - 1, 0, -1) if day_elevations[i - 1] > 0 and day_elevations[i] <= 0),
                    None,
                )
                if sunrise_idx is not None and sunset_idx is not None:
                    sr_utc = day_start + timedelta(minutes=15 * sunrise_idx)
                    ss_utc = day_start + timedelta(minutes=15 * sunset_idx)
                    sr_local = sr_utc.replace(tzinfo=timezone.utc).astimezone(tz)
                    ss_local = ss_utc.replace(tzinfo=timezone.utc).astimezone(tz)
                    tz_abbr = sr_local.strftime("%Z")
                    return [
                        ("Sunrise: ", f"~{sr_local.strftime('%H:%M')} {tz_abbr}"),
                        ("Sunset: ", f"~{ss_local.strftime('%H:%M')} {tz_abbr}"),
                        ("Day length: ", f"~{dl_h}h {dl_m}min"),
                    ]
                else:
                    return f"Near polar day — sun briefly crosses the horizon (≈{dl_h}h {dl_m}min above horizon)."
            hours = int(day_length_s // 3600)
            minutes = int((day_length_s % 3600) // 60)
            return [
                ("Sunrise: ", sun_info['sunrise'].astimezone(tz).strftime('%H:%M %Z')),
                ("Sunset: ", sun_info['sunset'].astimezone(tz).strftime('%H:%M %Z')),
                ("Day length: ", f"{hours}h {minutes}min"),
            ]
    elif sun_info["status"] == "no_data":
        return f"Sun data unavailable ({sun_info.get('raw_status')}) — may be a polar-day/polar-night edge case the API can't resolve at this latitude."
    else:
        return "Sun data fetch failed — check Action logs."


def build_sun_curve_chart(lat, lon, now_utc, now_local, tz_name):
    """
    Renders a 24-hour solar elevation curve for today (site-local time),
    with the current moment marked. The data window spans one local
    calendar day (midnight to midnight local time); the underlying solar
    position formula still operates on UTC internally (as it must, since
    it's tied to real longitude/UTC physics), but the window boundaries
    and all displayed labels are local so the axis and the data
    genuinely correspond to the same local day.
    Returns (png_bytes, caption).
    """
    try:
        local_day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        times_local = [local_day_start + timedelta(minutes=15 * i) for i in range(96)]
        times_utc = [t.astimezone(timezone.utc).replace(tzinfo=None) for t in times_local]

        elevations = [solar_elevation_deg(lat, lon, t) for t in times_utc]
        hour_floats = [t.hour + t.minute / 60 for t in times_local]

        current_elevation = solar_elevation_deg(lat, lon, now_utc)
        current_hour_float = now_local.hour + now_local.minute / 60

        NOTION_YELLOW = "#E7B347"
        NOTION_RED = "#E16259"
        NOTION_TEXT_GRAY = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"
        NOTION_HORIZON = "#D4A72C"
        NIGHT_BLUE = "#4A90D9"
        NIGHT_BLUE_EDGE = "#2563A8"

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(4.5, 2.8), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        above = [e > 0 for e in elevations]
        below = [e <= 0 for e in elevations]

        # Fill and curve above horizon — yellow
        ax.fill_between(hour_floats, elevations, 0, where=above,
                        color=NOTION_YELLOW, alpha=0.18, linewidth=0, zorder=1)
        # Fill and curve below horizon — blue
        ax.fill_between(hour_floats, elevations, 0, where=below,
                        color=NIGHT_BLUE, alpha=0.13, linewidth=0, zorder=1)

        # Draw curve in two colour segments so the transition at the horizon is sharp
        import numpy as np
        elev_arr = np.array(elevations)
        hour_arr = np.array(hour_floats)
        for color, mask in ((NOTION_YELLOW, elev_arr > 0), (NIGHT_BLUE, elev_arr <= 0)):
            if not mask.any():
                continue
            # Draw each contiguous run as a separate line so gaps don't bleed colour
            indices = np.where(mask)[0]
            breaks = np.where(np.diff(indices) > 1)[0] + 1
            runs = np.split(indices, breaks)
            for run in runs:
                # Extend by one point on each side to meet the horizon cleanly
                start = max(run[0] - 1, 0)
                end = min(run[-1] + 2, len(hour_arr))
                ax.plot(hour_arr[start:end], elev_arr[start:end],
                        linewidth=3, color=color, zorder=2, solid_capstyle="round")

        ax.axhline(0, color=NOTION_HORIZON, linewidth=1.2, alpha=0.6, zorder=1)

        dot_color = NOTION_YELLOW if current_elevation > 0 else NIGHT_BLUE
        dot_edge  = NOTION_RED    if current_elevation > 0 else NIGHT_BLUE_EDGE
        ax.plot([current_hour_float], [current_elevation], marker="o", markersize=18,
                color=dot_color, markeredgecolor=dot_edge, markeredgewidth=2.5, zorder=3)

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        ax.set_xlim(0, 24)
        ax.set_xticks(range(0, 25, 6))
        ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 6)], fontsize=16, color=NOTION_TEXT_GRAY)
        ax.tick_params(axis="y", labelsize=16, colors=NOTION_TEXT_GRAY, length=0)
        ax.tick_params(axis="x", length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
        ax.set_ylabel("Elevation (°)", fontsize=17, color=NOTION_TEXT_GRAY)

        fig.tight_layout()
        png_bytes = fig_to_png_bytes(fig)

        caption = (
            f"Solar elevation today, {local_day_start.strftime('%b %d')} (local time). "
            f"Computed from standard solar position formulas, not measured."
        )
        return png_bytes, caption

    except Exception as e:
        print("SUN CURVE CHART FAILED:", e)
        return None, "Sun position chart could not be generated — see Action logs."


# =========================================================
# SHARED HELPER — Open-Meteo historical archive
# Used by both the temperature chart and thawing degree days. The
# temp_cache dict is passed in by the caller (each site keeps its own
# cache file on disk — see load_temp_cache/save_temp_cache above), so
# this stays a pure function rather than depending on a module-level
# global, which matters now that multiple sites could in principle share
# this same library process space (e.g. a future batch-runner script).
# =========================================================
def fetch_daily_temps(lat, lon, start_date, end_date):
    """
    Fetches daily mean temperature for [start_date, end_date] (inclusive)
    from Open-Meteo's historical archive (ERA5 reanalysis).
    Returns a dict {date_str: temp_c} or {} on failure.
    """
    try:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "daily": "temperature_2m_mean",
            "timezone": "UTC",
        }
        r = get_with_retry(url, params=params, timeout=20, retries=1)
        data = r.json()
        daily = data.get("daily", {})
        times = daily.get("time", [])
        temps = daily.get("temperature_2m_mean", [])
        return dict(zip(times, temps))
    except Exception as e:
        print(f"HISTORICAL FETCH FAILED for {start_date} to {end_date}:", e)
        return {}


def fetch_full_year_cached(lat, lon, year, temp_cache):
    """
    Returns the complete Jan 1 - Dec 31 daily temperature dict for the
    given (necessarily past, complete) year — from temp_cache if already
    present, otherwise fetched fresh and added to temp_cache (mutated in
    place) for the caller to persist via save_temp_cache. Never used for
    the current year, which is always in-progress and must be fetched
    fresh every time.

    Returns (temps_dict, was_newly_cached: bool) so the caller can track
    whether anything changed and needs saving.
    """
    cache_key = str(year)
    if cache_key in temp_cache:
        return temp_cache[cache_key], False

    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    temps = fetch_daily_temps(lat, lon, year_start, year_end)

    days_in_year = (year_end - year_start).days + 1
    if len(temps) >= days_in_year * 0.95:
        # Only cache genuinely (near-)complete years — caching a partial
        # year from a bad fetch would permanently "freeze in" an
        # incomplete result, defeating the point of retrying later.
        temp_cache[cache_key] = temps
        print(f"CACHE: fetched and cached {year} ({len(temps)}/{days_in_year} days)")
        return temps, True
    else:
        print(f"CACHE: {year} fetch incomplete ({len(temps)}/{days_in_year} days), not caching")
        return temps, False


def prefetch_years_concurrently(lat, lon, years, temp_cache, max_workers=3):
    """
    Pre-fetches any of the given years not already in temp_cache, running
    up to max_workers requests concurrently rather than one at a time —
    the main lever for cutting run time, since Open-Meteo's documented
    rate limit (600/min, 5000/hour) comfortably allows this. temp_cache
    is mutated in place; returns True if anything new was added (so the
    caller knows whether save_temp_cache is needed).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    years_to_fetch = [y for y in years if str(y) not in temp_cache]
    if not years_to_fetch:
        return False

    print(f"PREFETCH: fetching {len(years_to_fetch)} uncached years concurrently (max {max_workers} at once)")
    any_cached = False
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_full_year_cached, lat, lon, year, temp_cache): year for year in years_to_fetch}
        for future in as_completed(futures):
            year = futures[future]
            try:
                _, was_cached = future.result()
                any_cached = any_cached or was_cached
            except Exception as e:
                print(f"PREFETCH FAILED for {year}:", e)
    return any_cached


# =========================================================
# MODULE — TEMPERATURE CHART: last 30 days vs N-year daily normal
# =========================================================
def build_temperature_chart(lat, lon, now_utc, temp_cache, normal_years=30):
    """
    Builds a chart of the last 30 days of mean daily temperature against
    the normal_years-year average for the same calendar days.

    The normal is computed here by pulling the same 30-day calendar
    window from each of the past normal_years years and averaging —
    Open-Meteo has no pre-computed "climate normal" endpoint, so this is
    done as separate historical queries (one per year, each covering the
    full 30-day window in a single request).

    temp_cache is the site's loaded cache dict (mutated in place by any
    new fetches) — the caller is responsible for loading it before this
    call and saving it after, via load_temp_cache/save_temp_cache.
    """
    end = (now_utc - timedelta(days=1)).date()  # yesterday, since today's mean isn't final yet
    start = end - timedelta(days=29)

    recent = fetch_daily_temps(lat, lon, start, end)
    if not recent:
        return None, "No recent historical temperature data returned."

    day_labels = sorted(recent.keys())
    recent_values = [recent[d] for d in day_labels]

    normals_by_day = {d: [] for d in day_labels}
    current_year = now_utc.year

    prefetch_years_concurrently(lat, lon, [end.year - yb for yb in range(1, normal_years + 1)], temp_cache)

    years_with_data = []

    for years_back in range(1, normal_years + 1):
        hist_year = end.year - years_back
        hist_start = start.replace(year=hist_year)
        hist_end = end.replace(year=hist_year)
        full_year_data, _ = fetch_full_year_cached(lat, lon, hist_year, temp_cache)

        if not full_year_data:
            continue

        hist_data = {
            d: t for d, t in full_year_data.items()
            if hist_start.strftime("%Y-%m-%d") <= d <= hist_end.strftime("%Y-%m-%d")
        }
        if not hist_data:
            continue

        years_with_data.append(hist_year)

        for hist_date_str, temp in hist_data.items():
            hist_date = datetime.strptime(hist_date_str, "%Y-%m-%d").date()
            matching_label = next(
                (d for d in day_labels if datetime.strptime(d, "%Y-%m-%d").date().strftime("%m-%d") == hist_date.strftime("%m-%d")),
                None,
            )
            if matching_label and temp is not None:
                normals_by_day[matching_label].append(temp)

    normal_values = []
    years_used_counts = []
    for d in day_labels:
        vals = normals_by_day[d]
        years_used_counts.append(len(vals))
        normal_values.append(sum(vals) / len(vals) if vals else None)

    min_years_used = min(years_used_counts) if years_used_counts else 0
    max_years_used = max(years_used_counts) if years_used_counts else 0
    print(f"TEMP CHART: normal built from {min_years_used}-{max_years_used} years of data per day")
    print(f"TEMP CHART: years with at least some data: {sorted(years_with_data)}")

    if min_years_used < 15:
        print("TEMP CHART: WARNING — fewer than 15 years of data available for the normal, treat with caution")

    if years_with_data:
        years_sorted = sorted(years_with_data)
        is_contiguous = years_sorted == list(range(years_sorted[0], years_sorted[-1] + 1))
        if len(years_sorted) == normal_years and is_contiguous:
            normal_label = f"{years_sorted[0]}–{years_sorted[-1]} average"
        elif is_contiguous:
            normal_label = f"{years_sorted[0]}–{years_sorted[-1]} average ({len(years_sorted)} years)"
        else:
            normal_label = f"{len(years_sorted)}-year average ({years_sorted[0]}–{years_sorted[-1]}, with gaps)"
    else:
        normal_label = "historical average (no data)"

    NOTION_TEXT_GRAY  = "#787774"
    NOTION_HIST_GRAY  = "#9CA3AF"   # neutral reference — not water-blue
    NOTION_TEMP_AMBER = "#E8A838"   # same amber as GEM temperature chart
    NOTION_LIGHT_GRID = "#EDECEC"

    plt.rcParams["font.family"] = "DejaVu Sans"

    fig, ax = plt.subplots(figsize=(8, 3.5), dpi=150)
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")

    x_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%b %d") for d in day_labels]
    x = range(len(day_labels))

    ax.fill_between(x, [v - 1.5 if v is not None else math.nan for v in normal_values],
                     [v + 1.5 if v is not None else math.nan for v in normal_values],
                     color=NOTION_HIST_GRAY, alpha=0.25, linewidth=0, zorder=1)
    ax.plot(x, normal_values, linewidth=1.5, color=NOTION_HIST_GRAY, alpha=0.80,
             label=normal_label, zorder=2)

    ax.plot(x, recent_values, marker="o", markersize=4, linewidth=2,
             color=NOTION_TEMP_AMBER, label=f"{current_year} observed",
             markerfacecolor="white", markeredgewidth=1.2, markeredgecolor=NOTION_TEMP_AMBER, zorder=3)

    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

    tick_positions = list(x)[::5]   # one tick every 5 days — avoids crowding
    tick_labels_out = x_labels[::5]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels_out, fontsize=13, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
    ax.tick_params(axis="y", labelsize=13, colors=NOTION_TEXT_GRAY, length=0)
    ax.tick_params(axis="x", length=0)

    ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)

    ax.set_ylabel("°C", fontsize=13, color=NOTION_TEXT_GRAY)
    ax.legend(loc="upper left", frameon=False, fontsize=11, labelcolor=NOTION_TEXT_GRAY)

    fig.tight_layout()
    png_bytes = fig_to_png_bytes(fig, white_bg=True)

    if min_years_used == max_years_used:
        years_phrase = f"{min_years_used} years" if min_years_used != normal_years else f"the full {normal_years} years"
    else:
        years_phrase = f"{min_years_used} to {max_years_used} years (varies by day)"

    caption = (
        f"Daily mean temperature, last 30 days vs. {normal_label.replace(' average', '')} "
        f"(shaded band ±1.5°C). Normal computed from {years_phrase} of ERA5 data per calendar day."
    )
    return png_bytes, caption


# =========================================================
# THAWING DEGREE DAYS HISTOGRAM (one bar per year, full year totals)
# Thawing degree days = cumulative sum of mean daily temperatures above
# 0°C, from Jan 1 through Dec 31 for past years (a real annual total), or
# Jan 1 through yesterday for the current year (necessarily partial,
# highlighted in a different color so it's not mistaken for a complete
# year). Uses the same fetch_daily_temps function already verified for
# the temperature chart, so failures/retries behave identically.
# =========================================================
def compute_tdd_from_temps(daily_temps, start_date, end_date):
    """
    Sums mean daily temps above 0°C from start_date through end_date
    (inclusive). Missing days are skipped, not treated as 0 — using real
    date arithmetic (not hand-rolled year shifting) so leap years are
    handled correctly automatically.
    """
    total = 0.0
    days_counted = 0
    d = start_date
    while d <= end_date:
        temp = daily_temps.get(d.strftime("%Y-%m-%d"))
        if temp is not None:
            days_counted += 1
            if temp > 0:
                total += temp
        d += timedelta(days=1)
    return total, days_counted


def build_tdd_histogram(lat, lon, now_utc, temp_cache, num_years=25):
    """
    Builds a bar chart of annual thawing degree days for the past
    num_years complete years, plus the current (partial) year in a
    different color. Returns (png_bytes, caption).

    Past complete years are fetched via fetch_full_year_cached, the same
    on-disk cache used by the temperature chart's normal — so a year
    already fetched for one chart doesn't need to be fetched again for
    the other.
    """
    today = now_utc.date()
    current_year = today.year

    tdd_by_year = {}

    prefetch_years_concurrently(lat, lon, [current_year - yb for yb in range(1, num_years + 1)], temp_cache)

    for years_back in range(1, num_years + 1):
        year = current_year - years_back
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        temps, _ = fetch_full_year_cached(lat, lon, year, temp_cache)
        if not temps:
            print(f"TDD HISTOGRAM: no data for {year}, skipping (will retry next run)")
            continue
        tdd, days_counted = compute_tdd_from_temps(temps, year_start, year_end)
        days_in_year = (year_end - year_start).days + 1
        if days_counted < days_in_year * 0.8:
            print(f"TDD HISTOGRAM: {year} only has {days_counted}/{days_in_year} days, skipping (too incomplete, will retry next run)")
            continue
        tdd_by_year[year] = tdd

    current_start = date(current_year, 1, 1)
    current_end = today - timedelta(days=1)
    current_temps = fetch_daily_temps(lat, lon, current_start, current_end)
    if not current_temps:
        # ERA5 archive has a 5-7 day lag; fall back to the forecast API for the
        # current partial year, which has the most recent days.
        print(f"TDD HISTOGRAM: ERA5 returned nothing for {current_year}, trying forecast API")
        try:
            r = get_with_retry(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_mean",
                    "timezone": "UTC",
                    "start_date": current_start.strftime("%Y-%m-%d"),
                    "end_date": current_end.strftime("%Y-%m-%d"),
                    # past_days conflicts with start_date/end_date — omit it.
                },
                timeout=20, retries=1,
            )
            daily = r.json().get("daily", {})
            current_temps = dict(zip(daily.get("time", []), daily.get("temperature_2m_mean", [])))
        except Exception as _e:
            print(f"TDD HISTOGRAM: forecast API fallback also failed: {_e}")
    if current_temps:
        current_tdd, current_days = compute_tdd_from_temps(current_temps, current_start, current_end)
        tdd_by_year[current_year] = current_tdd

    if not tdd_by_year:
        return None, "Thawing degree days data unavailable — all fetches failed. Check Action logs."

    print(f"TDD HISTOGRAM: years with data: {sorted(tdd_by_year.keys())}")

    try:
        NOTION_TEXT_GRAY  = "#787774"
        NOTION_TEMP_AMBER = "#E8A838"
        NOTION_RED        = "#E16259"
        NOTION_LIGHT_GRID = "#EDECEC"

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(8, 3.5), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        full_year_range = list(range(current_year - num_years, current_year + 1))
        plotted_values = [tdd_by_year.get(y, 0) for y in full_year_range]
        colors = [
            NOTION_RED if y == current_year
            else NOTION_LIGHT_GRID if y not in tdd_by_year
            else NOTION_TEMP_AMBER
            for y in full_year_range
        ]

        x = range(len(full_year_range))
        ax.bar(x, plotted_values, color=colors, width=0.7)

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        ax.set_xticks(list(x))
        ax.set_xticklabels([str(y) for y in full_year_range], fontsize=13, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
        ax.tick_params(axis="y", labelsize=13, colors=NOTION_TEXT_GRAY, length=0)
        ax.tick_params(axis="x", length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
        ax.set_axisbelow(True)
        ax.set_ylabel("Thawing degree days (°C·days)", fontsize=13, color=NOTION_TEXT_GRAY)

        fig.tight_layout()
        png_bytes = fig_to_png_bytes(fig, white_bg=True)

        gap_years = [y for y in full_year_range if y not in tdd_by_year and y != current_year]
        gap_note = f" Years with incomplete or missing data ({', '.join(str(y) for y in gap_years)}) are shown empty." if gap_years else ""
        caption = (
            f"Annual thawing degree days (sum of mean daily temperatures above 0°C, Jan 1–Dec 31), "
            f"{full_year_range[0]}–{full_year_range[-2]}. "
            f"Current year ({current_year}, in red) is partial: Jan 1 through {current_end.strftime('%b %d')} only, "
            f"not directly comparable to complete-year totals.{gap_note} Source: Open-Meteo (ERA5)."
        )
        return png_bytes, caption

    except Exception as e:
        print("TDD HISTOGRAM RENDER FAILED:", e)
        return None, "Thawing degree days chart could not be generated — see Action logs."


# =========================================================
# MODULE — WIND VECTOR CHART (last 30 days)
# Fetches hourly wind speed/direction from the same Open-Meteo historical
# archive used for temperature, aggregates to one vector per day (using
# proper vector averaging — not naive angle averaging, which is wrong
# near the 0/360 boundary), and renders as color-graded direction arrows.
# =========================================================
def wind_to_uv(speed, direction_deg):
    """
    Converts meteorological wind speed/direction (direction = where wind
    comes FROM, standard convention) to u (eastward) / v (northward)
    vector components, for correct vector-based averaging.
    """
    direction_rad = math.radians(direction_deg)
    u = -speed * math.sin(direction_rad)
    v = -speed * math.cos(direction_rad)
    return u, v


def uv_to_wind(u, v):
    speed = math.hypot(u, v)
    direction_rad = math.atan2(-u, -v)
    direction_deg = math.degrees(direction_rad) % 360
    return speed, direction_deg


def fetch_hourly_wind_chunk(lat, lon, start_date, end_date):
    """
    Fetches hourly wind speed and direction for a single [start_date,
    end_date] window from Open-Meteo's historical archive. Returns a dict
    {date_str: [(speed, direction), ...]} grouped by calendar day, or {}
    on failure for just this chunk.
    """
    try:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "hourly": "windspeed_10m,winddirection_10m",
            "timezone": "UTC",
        }
        r = get_with_retry(url, params=params, timeout=30, retries=2)
        data = r.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        speeds = hourly.get("windspeed_10m", [])
        directions = hourly.get("winddirection_10m", [])

        by_day = {}
        for t, s, d in zip(times, speeds, directions):
            if s is None or d is None:
                continue
            day = t[:10]
            by_day.setdefault(day, []).append((s, d))
        return by_day
    except Exception as e:
        print(f"WIND VECTOR CHUNK FETCH FAILED for {start_date} to {end_date}:", e)
        return {}


def fetch_hourly_wind(lat, lon, start_date, end_date, chunk_days=10):
    """
    Fetches hourly wind speed/direction across [start_date, end_date] by
    splitting the request into smaller chunks (default 10 days each),
    fetched concurrently rather than one after another. This is more
    resilient than one large request: a failure in one chunk only loses
    that chunk's days rather than the entire requested window.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    chunk_ranges = []
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end_date)
        chunk_ranges.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)

    combined = {}
    with ThreadPoolExecutor(max_workers=len(chunk_ranges) or 1) as executor:
        futures = {executor.submit(fetch_hourly_wind_chunk, lat, lon, s, e): (s, e) for s, e in chunk_ranges}
        for future in as_completed(futures):
            s, e = futures[future]
            try:
                chunk_data = future.result()
                combined.update(chunk_data)
            except Exception as ex:
                print(f"WIND VECTOR CHUNK FAILED for {s} to {e}:", ex)

    if not combined:
        print("WIND VECTOR FETCH FAILED: all chunks returned no data")
    elif len(combined) < (end_date - start_date).days:
        print(f"WIND VECTOR FETCH: partial data only — got {len(combined)} of {(end_date - start_date).days + 1} expected days")

    return combined


def _compute_daily_wind(by_day):
    """
    Takes the raw {date_str: [(speed, direction), ...]} dict from
    fetch_hourly_wind and returns (day_labels, daily_speed, daily_dir)
    using proper vector averaging.
    """
    day_labels = sorted(by_day.keys())
    daily_speed, daily_dir = [], []
    for day in day_labels:
        readings = by_day[day]
        us, vs = [], []
        for s, d in readings:
            u, v = wind_to_uv(s, d)
            us.append(u)
            vs.append(v)
        avg_u, avg_v = sum(us) / len(us), sum(vs) / len(vs)
        speed, direction = uv_to_wind(avg_u, avg_v)
        daily_speed.append(speed)
        daily_dir.append(direction)
    return day_labels, daily_speed, daily_dir


def _render_wind_combined_figure(day_labels, daily_speed, daily_dir, by_day):
    """
    Renders the wind rose (left, 1/4 width) and the daily vector chart
    (right, 3/4 width) as a single figure using gridspec — so the size
    ratio is fixed in the image itself, independent of any Notion column
    layout. Returns PNG bytes.
    """
    NOTION_TEXT_GRAY = "#787774"
    NOTION_LIGHT_GRID = "#EDECEC"

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig = plt.figure(figsize=(10, 4.2), dpi=150)
    fig.patch.set_alpha(0)

    gs = fig.add_gridspec(1, 4, wspace=0.5, left=0.04, right=0.97, top=0.93, bottom=0.22)
    ax_rose = fig.add_subplot(gs[0, 0], projection="polar")
    ax_vec = fig.add_subplot(gs[0, 1:])

    ax_rose.set_facecolor("none")
    ax_vec.set_facecolor("none")

    # ---- Wind rose (left, compact) ----
    all_speeds, all_dirs = [], []
    for readings in by_day.values():
        for s, d in readings:
            all_speeds.append(s)
            all_dirs.append(d)

    n_obs = len(all_speeds)
    if n_obs > 0:
        n_dirs = 16
        dir_width_deg = 360 / n_dirs
        speed_bins = [0, 10, 20, 30, 40, 9999]
        cmap = matplotlib.colormaps["plasma"]
        colors = [cmap(v) for v in [0.05, 0.28, 0.52, 0.76, 0.97]]

        counts = np.zeros((n_dirs, len(speed_bins) - 1))
        for spd, drn in zip(all_speeds, all_dirs):
            dir_idx = int((drn + dir_width_deg / 2) % 360 / dir_width_deg) % n_dirs
            for si in range(len(speed_bins) - 1):
                if speed_bins[si] <= spd < speed_bins[si + 1]:
                    counts[dir_idx, si] += 1
                    break
        freqs = counts / n_obs * 100

        ax_rose.set_theta_zero_location("N")
        ax_rose.set_theta_direction(-1)

        dir_centers_deg = np.arange(0, 360, dir_width_deg)
        theta = np.radians(dir_centers_deg)
        bar_width = np.radians(dir_width_deg * 0.82)

        bottoms = np.zeros(n_dirs)
        for si, color in enumerate(colors):
            ax_rose.bar(theta, freqs[:, si], width=bar_width, bottom=bottoms,
                        color=color, alpha=0.92, linewidth=0)
            bottoms += freqs[:, si]

        ax_rose.set_xticks(np.radians([0, 45, 90, 135, 180, 225, 270, 315]))
        ax_rose.set_xticklabels(["N", "NE", "E", "SE", "S", "SW", "W", "NW"],
                                 fontsize=14, color=NOTION_TEXT_GRAY)
        ax_rose.tick_params(axis="y", labelsize=11, colors=NOTION_TEXT_GRAY)
        ax_rose.grid(color=NOTION_TEXT_GRAY, alpha=0.25, linewidth=0.5)
        ax_rose.spines["polar"].set_visible(False)

        max_freq = bottoms.max()
        ax_rose.set_yticks([])
        ax_rose.set_ylim(0, max_freq * 1.18)

    # ---- Daily vector chart (right, dominant) ----
    x = list(range(len(day_labels)))
    u_arrows = [-math.sin(math.radians(d)) * s for d, s in zip(daily_dir, daily_speed)]
    v_arrows = [-math.cos(math.radians(d)) * s for d, s in zip(daily_dir, daily_speed)]

    ax_vec.axhline(0, color=NOTION_LIGHT_GRID, linewidth=1.2, zorder=1)

    quiv = ax_vec.quiver(
        x, [0] * len(x), u_arrows, v_arrows,
        daily_speed, cmap="plasma", scale=220, width=0.005,
        pivot="tail", clim=(0, 40), zorder=2,
    )

    cbar = fig.colorbar(quiv, ax=ax_vec, orientation="vertical", pad=0.02, fraction=0.03)
    cbar.set_label("km/h", fontsize=14, color=NOTION_TEXT_GRAY)
    cbar.ax.tick_params(labelsize=13, colors=NOTION_TEXT_GRAY)
    cbar.outline.set_visible(False)

    for spine in ["top", "right", "left"]:
        ax_vec.spines[spine].set_visible(False)
    ax_vec.spines["bottom"].set_color(NOTION_LIGHT_GRID)

    tick_positions = x[::3]
    tick_labels = [datetime.strptime(day_labels[i], "%Y-%m-%d").strftime("%b %d") for i in tick_positions]
    ax_vec.set_xticks(tick_positions)
    ax_vec.set_xticklabels(tick_labels, fontsize=15, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
    ax_vec.set_yticks([])
    ax_vec.tick_params(axis="x", length=0)
    ax_vec.set_ylim(-1.5, 1.5)
    ax_vec.set_xlim(-2, len(x) + 1)

    return fig_to_png_bytes(fig, white_bg=True)


def build_wind_charts_combined(lat, lon, now_utc):
    """
    Fetches 30-day hourly wind once and renders the wind rose + vector
    chart as a single combined image. Returns (combined_bytes, None, caption)
    — the None keeps the 3-tuple API so entrypoints don't need updating;
    build_wind_chart_section treats rose_bytes=None as "show wind_chart_bytes
    directly" (which is now the combined image).
    """
    end = (now_utc - timedelta(days=1)).date()
    start = end - timedelta(days=29)

    by_day = fetch_hourly_wind(lat, lon, start, end)
    if not by_day:
        msg = "Wind data unavailable — fetch failed. Check Action logs."
        return None, None, msg

    day_labels, daily_speed, daily_dir = _compute_daily_wind(by_day)

    combined_bytes = None
    try:
        combined_bytes = _render_wind_combined_figure(day_labels, daily_speed, daily_dir, by_day)
    except Exception as e:
        print("WIND COMBINED CHART RENDER FAILED:", e)

    n_obs = sum(len(v) for v in by_day.values())
    caption = (
        f"Wind rose (left): {n_obs} hourly observations, frequency by direction and speed. "
        f"Wind vectors (right): daily averages, arrows point toward wind destination. "
        f"Plasma color scale: 0–40 km/h. Source: Open-Meteo (ERA5)."
    )
    return combined_bytes, None, caption


def build_wind_vector_chart(lat, lon, now_utc):
    """
    Builds a 30-day wind vector chart: one arrow per day, pointing in the
    direction the wind blows TOWARD (so arrows visually show flow
    direction), colored by speed. Returns (png_bytes, caption).
    Kept for backward compatibility — prefer build_wind_charts_combined
    when the wind rose is also needed.
    """
    end = (now_utc - timedelta(days=1)).date()
    start = end - timedelta(days=29)

    by_day = fetch_hourly_wind(lat, lon, start, end)
    if not by_day:
        return None, "Wind vector data unavailable — fetch failed. Check Action logs."

    day_labels, daily_speed, daily_dir = _compute_daily_wind(by_day)

    try:
        png_bytes = _render_wind_vector_chart(day_labels, daily_speed, daily_dir)
        caption = (
            "Daily-average wind vectors, last 30 days. Arrows point in the direction "
            "the wind blows toward; color shows speed. Source: Open-Meteo (ERA5)."
        )
        return png_bytes, caption
    except Exception as e:
        print("WIND VECTOR CHART RENDER FAILED:", e)
        return None, "Wind vector chart could not be generated — see Action logs."


# =========================================================
# MODULE — SATELLITE: MODIS true color via GIBS WMS
#
# Several values here are genuinely SITE-SPECIFIC CONSTANTS, computed
# once per site and stored in that site's config.py — not parameters
# recomputed live every run:
#
#   - The EPSG:3413 center point (call compute_3413_center(lat, lon)
#     once and paste the result into config.py).
#   - MODIS_ROTATION_DEG: the rotation needed to make true north point
#     up at this specific site's longitude. This requires EMPIRICAL
#     verification against a real fetched image, not pure geometry —
#     see the note in rotate_to_north_up below. Start from a geometric
#     estimate (roughly, the site's longitude minus -45°, the EPSG:3413
#     central meridian), then check a real image and adjust.
#   - SENTINEL1_UTM_ZONE: standard UTM zone math, but MUST be computed
#     per site, not assumed — Herschel Island (-139.1°) is zone 7, while
#     Shingle Point (-137.2°) is actually zone 8, despite being only
#     ~130km away (zone boundaries are simple longitude bands, so a site
#     can be close to a neighbor yet in a different zone). Compute via
#     compute_utm_zone(lon) for any new site rather than assuming it
#     matches a nearby existing one.
# =========================================================
MODIS_FINAL_SIZE_PX = 1024

# GIBS's polar stereographic image is only north-up exactly along its
# central meridian (-45°). A site far from that meridian gets a raw
# image rotated relative to true north — the further from -45° longitude,
# the more rotated. Fixed by fetching a larger image than needed,
# rotating it so true north points up at the site's location, then
# cropping back to the final size. The oversize factor covers the
# worst-case corner loss from rotating a square image by any angle, with
# extra margin for safety.
MODIS_OVERSIZE_FACTOR = 1.2
MODIS_FETCH_SIZE_PX = int(MODIS_FINAL_SIZE_PX * MODIS_OVERSIZE_FACTOR)


def compute_utm_zone(lon_deg):
    """
    Standard UTM zone number for a given longitude. Use this when adding
    a new site, rather than assuming it shares a neighboring site's
    zone — zone boundaries are simple 6°-wide longitude bands, so two
    nearby sites can fall in different zones (e.g. Herschel Island at
    -139.1° is zone 7, while Shingle Point at -137.2° — only ~130km away
    — is zone 8, since the zone 7/8 boundary falls at exactly -138°).
    """
    return int((lon_deg + 180) / 6) + 1


def latlon_to_3413(lat_deg, lon_deg):
    """
    Converts WGS84 lat/lon to EPSG:3413 (Arctic polar stereographic)
    projected meters, using the standard Snyder polar stereographic
    variant B forward formula. Verified against pyproj to sub-meter
    precision.
    """
    a = 6378137.0           # WGS84 semi-major axis (meters)
    f = 1 / 298.257223563   # WGS84 flattening
    e2 = 2 * f - f ** 2
    e = math.sqrt(e2)

    lat_ts = math.radians(70)    # EPSG:3413 standard parallel (latitude of true scale)
    lon0 = math.radians(-45)     # EPSG:3413 central meridian

    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)

    t_c = math.tan(math.pi / 4 - lat_ts / 2) / (
        ((1 - e * math.sin(lat_ts)) / (1 + e * math.sin(lat_ts))) ** (e / 2)
    )
    m_c = math.cos(lat_ts) / math.sqrt(1 - e2 * math.sin(lat_ts) ** 2)

    t = math.tan(math.pi / 4 - lat / 2) / (
        ((1 - e * math.sin(lat)) / (1 + e * math.sin(lat))) ** (e / 2)
    )
    rho = a * m_c * (t / t_c)

    x = rho * math.sin(lon - lon0)
    y = -rho * math.cos(lon - lon0)
    return x, y


def compute_3413_center(lat_deg, lon_deg):
    """
    Convenience wrapper: computes the EPSG:3413 center point for a new
    site. Call this once when setting up a new site's config.py, and
    paste the printed result in as a constant — it does not need (and
    should not) be recomputed every run.
    """
    return latlon_to_3413(lat_deg, lon_deg)


def latlon_to_topaz6_grid(lat_deg, lon_deg):
    """
    Converts WGS84 lat/lon to the TOPAZ6 THREDDS grid's own native
    projection (met.no's dataset-topaz6-arc-15min-3km-be.ncml) - NOT
    EPSG:3413. Confirmed 2026-07-17 via the dataset's own
    ds['stereographic'].attrs['proj4']:
    '+proj=stere +lon_0=-45 +lat_0=90 +k=1 +R=6378273 +no_defs' - a
    spherical (not WGS84-ellipsoid) polar stereographic projection with
    scale factor 1 at the pole, not EPSG:3413's standard-parallel-70N
    scaling. Using latlon_to_3413() here (as this code previously did)
    put the target point ~60km off at Herschel Island's latitude, enough
    to silently select the wrong 3km grid cell. Verified against pyproj
    (CRS.from_proj4 of the exact string above) to sub-millimeter
    precision at multiple Arctic test points before this fix landed.
    """
    R = 6378273.0
    lon0 = math.radians(-45)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    rho = 2 * R * math.tan(math.pi / 4 - lat / 2)
    x = rho * math.sin(lon - lon0)
    y = -rho * math.cos(lon - lon0)
    return x, y


def latlon_to_utm(lat_deg, lon_deg, zone):
    """
    Standard UTM forward projection (WGS84), verified against pyproj to
    sub-meter precision. zone is required (no default) — see
    compute_utm_zone, and the warning above about not assuming a new
    site shares a neighboring site's zone.
    """
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    e4 = e2 ** 2
    e6 = e2 ** 3
    k0 = 0.9996

    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)

    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    T = math.tan(lat) ** 2
    C = e2 / (1 - e2) * math.cos(lat) ** 2
    A = (lon - lon0) * math.cos(lat)

    M = a * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat)
        - (35 * e6 / 3072) * math.sin(6 * lat)
    )

    x = k0 * N * (A + (1 - T + C) * A ** 3 / 6 +
                  (5 - 18 * T + T ** 2 + 72 * C - 58 * (e2 / (1 - e2))) * A ** 5 / 120) + 500000.0
    y = k0 * (M + N * math.tan(lat) * (A ** 2 / 2 +
              (5 - T + 9 * C + 4 * C ** 2) * A ** 4 / 24 +
              (61 - 58 * T + T ** 2 + 600 * C - 330 * (e2 / (1 - e2))) * A ** 6 / 720))

    if lat_deg < 0:
        y += 10000000.0

    return x, y


def build_gibs_url(date_str, bbox_3413, fetch_size_px=MODIS_FETCH_SIZE_PX):
    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetMap",
        "VERSION": "1.1.1",
        "LAYERS": "MODIS_Terra_CorrectedReflectance_TrueColor,Coastlines",
        "STYLES": "",
        "FORMAT": "image/png",
        "TRANSPARENT": "false",
        "WIDTH": str(fetch_size_px),
        "HEIGHT": str(fetch_size_px),
        "SRS": "EPSG:3413",
        "BBOX": bbox_3413,
        "TIME": date_str,
    }
    base = "https://gibs.earthdata.nasa.gov/wms/epsg3413/best/wms.cgi"
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{base}?{query}"


def rotate_to_north_up(png_bytes, rotation_deg, final_size_px=MODIS_FINAL_SIZE_PX):
    """
    Rotates the fetched (oversized) polar stereographic image so true
    north points up at the site's location, then center-crops to the
    final display size. Returns the original bytes unchanged if this
    fails for any reason, so a rotation bug never blocks the image from
    displaying.

    rotation_deg sign convention: empirically verified (by placing a
    known due-north test point in a simulated raw image and checking
    both signs) — PIL's rotate() needs the POSITIVE angle to bring true
    north to the top, for the way this script's pixel rows map to
    projected y. A pure sign-algebra derivation led to the wrong overall
    result once combined with that mapping in practice, so for a NEW
    site, verify empirically against a real fetched image rather than
    trusting geometry alone — render once, check if the coastline's
    known real-world orientation matches, adjust if not.
    """
    try:
        from PIL import Image
        import io as _io

        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        rotated = img.rotate(rotation_deg, resample=Image.BICUBIC, expand=False)

        w, h = rotated.size
        left = (w - final_size_px) // 2
        top = (h - final_size_px) // 2
        cropped = rotated.crop((left, top, left + final_size_px, top + final_size_px))

        out_buf = _io.BytesIO()
        cropped.save(out_buf, format="PNG")
        return out_buf.getvalue()
    except Exception as e:
        print("MODIS ROTATION FAILED (showing unrotated image instead):", e)
        return png_bytes


def fetch_modis_image(bbox_3413, now_utc, max_days_back=10, fetch_size_px=MODIS_FETCH_SIZE_PX):
    for days_back in range(0, max_days_back + 1):
        date_str = (now_utc - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = build_gibs_url(date_str, bbox_3413, fetch_size_px)
        try:
            resp = requests.get(url, timeout=20)
        except Exception as e:
            print(f"MODIS request failed for {date_str}:", e)
            continue

        content_type = resp.headers.get("Content-Type", "")
        is_real_png = resp.content[:8] == b"\x89PNG\r\n\x1a\n"
        print(f"MODIS {date_str}: HTTP {resp.status_code}, type={content_type}, bytes={len(resp.content)}")

        if resp.status_code == 200 and "image/png" in content_type and is_real_png and len(resp.content) >= 5000:
            return resp.content, date_str
        print("  -> rejected (not a usable image for this date)")

    return None, None


def annotate_modis_image(png_bytes, points, center_x, center_y, rotation_deg,
                          half_width_m=150_000, scale_km=50, reference_lines=None,
                          final_size_px=MODIS_FINAL_SIZE_PX, coastline_geojson_path=None):
    """
    Draws label markers at the given coordinates and a scale bar on the
    MODIS image. The image itself has already been rotated to north-up
    and cropped to final_size_px by rotate_to_north_up() before this
    function runs. To place points consistently with that same rotation,
    each point's (x, y) offset from the site's center, in EPSG:3413
    projected meters, is rotated by the same angle used for the image,
    then mapped onto the final square frame (which is centered on the
    site by construction).

    points: REQUIRED list of (lat, lon, label) or (lat, lon, label,
    text_dy) or (lat, lon, label, text_dy, text_dx) tuples — the markers
    to draw (typically the site itself plus nearby reference
    settlements). No default: each site's config.py supplies its own
    list, since a sensible default for one site (e.g. "Shingle Point")
    would be wrong for every other site.

    reference_lines: optional list of (lat1, lon1, lat2, lon2, label)
    tuples for dashed reference lines (e.g. an international border) —
    omit entirely for sites where no such line is relevant.

    The scale bar uses a uniform meters-per-pixel value, valid since
    rotation preserves distances and the frame is centered consistently.

    Returns annotated PNG bytes, or the original bytes unchanged if
    annotation fails for any reason (so a drawing bug never blocks the
    underlying satellite image from being shown).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io

        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        width_px, height_px = img.size

        # Meters-per-pixel for the FINAL (post-crop) frame, not the
        # oversized fetch — this is the actual resolution of what's shown.
        meters_per_px = (half_width_m * 2) / final_size_px

        rotation_rad = math.radians(rotation_deg)

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        except Exception:
            font = ImageFont.load_default()

        def project_point(lat, lon):
            x_m, y_m = latlon_to_3413(lat, lon)
            dx_m = x_m - center_x
            dy_m = y_m - center_y
            cos_r, sin_r = math.cos(rotation_rad), math.sin(rotation_rad)
            dx_rot = dx_m * cos_r - dy_m * sin_r
            dy_rot = dx_m * sin_r + dy_m * cos_r
            x_px = width_px / 2 + dx_rot / meters_per_px
            y_px = height_px / 2 - dy_rot / meters_per_px
            return x_px, y_px

        # --- Coastline overlay (white lines, 2px) ---
        if coastline_geojson_path:
            try:
                import json as _json2
                with open(coastline_geojson_path) as _cf:
                    coast_geojson = _json2.load(_cf)
                segments_drawn = 0
                for feature in coast_geojson.get("features", []):
                    geom = feature.get("geometry", {})
                    if geom.get("type") != "LineString":
                        continue
                    coords = geom.get("coordinates", [])
                    prev_px = None
                    for coord_lon, coord_lat in coords:
                        px = project_point(coord_lat, coord_lon)
                        if prev_px is not None:
                            draw.line([prev_px, px], fill=(255, 255, 255), width=2)
                            segments_drawn += 1
                        prev_px = px
                print(f"MODIS COASTLINE OVERLAY: drew {segments_drawn} segments")
            except Exception as e:
                print(f"MODIS COASTLINE OVERLAY FAILED: {e}")

        # --- Label markers ---
        for point in points:
            if len(point) == 5:
                lat, lon, label_text, text_dy, text_dx = point
            elif len(point) == 4:
                lat, lon, label_text, text_dy = point
                text_dx = 12
            else:
                lat, lon, label_text = point
                text_dy = -10
                text_dx = 12

            x_px, y_px = project_point(lat, lon)

            marker_radius = 6
            draw.ellipse(
                [x_px - marker_radius, y_px - marker_radius, x_px + marker_radius, y_px + marker_radius],
                fill=(255, 60, 60), outline=(255, 255, 255), width=2,
            )

            text_x, text_y = x_px + text_dx, y_px + text_dy
            for tdx, tdy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                draw.text((text_x + tdx, text_y + tdy), label_text, font=font, fill=(0, 0, 0))
            draw.text((text_x, text_y), label_text, font=font, fill=(255, 255, 255))

        # --- Optional reference lines (e.g. an international border) ---
        for line in (reference_lines or []):
            lat1, lon1, lat2, lon2, _label = line
            p1 = project_point(lat1, lon1)
            p2 = project_point(lat2, lon2)
            num_dashes = 120
            for i in range(num_dashes):
                if i % 2 != 0:
                    continue
                t0, t1 = i / num_dashes, (i + 1) / num_dashes
                seg_p1 = (p1[0] + (p2[0] - p1[0]) * t0, p1[1] + (p2[1] - p1[1]) * t0)
                seg_p2 = (p1[0] + (p2[0] - p1[0]) * t1, p1[1] + (p2[1] - p1[1]) * t1)
                draw.line([seg_p1, seg_p2], fill=(180, 200, 210), width=1)

        # --- Scale bar (bottom-left corner) ---
        px_per_km = 1000 / meters_per_px
        bar_px = scale_km * px_per_km
        margin = 30
        bar_x0 = margin
        bar_y0 = height_px - margin - 10
        bar_x1 = bar_x0 + bar_px

        draw.line([(bar_x0, bar_y0), (bar_x1, bar_y0)], fill=(255, 255, 255), width=4)
        draw.line([(bar_x0, bar_y0 - 6), (bar_x0, bar_y0 + 6)], fill=(255, 255, 255), width=4)
        draw.line([(bar_x1, bar_y0 - 6), (bar_x1, bar_y0 + 6)], fill=(255, 255, 255), width=4)
        draw.text((bar_x0, bar_y0 + 8), f"{scale_km} km", font=font, fill=(255, 255, 255))

        out_buf = _io.BytesIO()
        img.save(out_buf, format="PNG")
        return out_buf.getvalue()

    except Exception as e:
        print("MODIS ANNOTATION FAILED (showing unannotated image instead):", e)
        return png_bytes


def annotate_plain_image(png_bytes, points, center_x, center_y, project_fn,
                          lat, lon, half_width_m=150_000, scale_km=50,
                          reference_lines=None, coastline_geojson_path=None,
                          arrow_annotations=None, water_bodies_geojson_path=None):
    """
    Draws label markers, an optional coastline overlay, and a scale bar
    on an already north-up, non-rotated image (e.g. Sentinel-1 in UTM) —
    unlike annotate_modis_image, which additionally rotates each point's
    offset to match MODIS's own rotate-then-crop workflow.

    points: REQUIRED, same format as annotate_modis_image — no
    site-specific default (see that function's docstring for why).

    project_fn: the forward projection function to use (e.g. latlon_to_utm
    with a specific site's zone already bound via functools.partial, or
    a plain lambda) — REQUIRED, since which projection is genuinely
    north-up depends on the site's longitude (see SENTINEL1_UTM_ZONE
    notes above the MODIS section).

    lat/lon: the site's own coordinates, needed here only for the
    coastline overlay's bounding-box filter (NOT used for projection —
    that's project_fn/center_x/center_y).

    coastline_geojson_path: path to a local, pre-filtered GeoJSON extract
    of OSM's natural=coastline data for this site (see
    build_coastline_extract.py in this repo for how to generate one for
    a new site). If None, no coastline overlay is drawn — appropriate
    for a site where this hasn't been set up yet, rather than crashing.

    reference_lines: optional list of (lat1, lon1, lat2, lon2, label)
    tuples for dashed reference lines — see annotate_modis_image.

    Returns annotated PNG bytes, or the original bytes unchanged if
    annotation fails for any reason.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io

        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        width_px, height_px = img.size

        meters_per_px = (half_width_m * 2) / width_px

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        except Exception:
            font = ImageFont.load_default()

        def project_point(plat, plon):
            x_m, y_m = project_fn(plat, plon)
            dx_m = x_m - center_x
            dy_m = y_m - center_y
            # NO rotation applied here — the image is already north-up.
            x_px = width_px / 2 + dx_m / meters_per_px
            y_px = height_px / 2 - dy_m / meters_per_px
            return x_px, y_px

        # --- Coastline overlay, in white ---
        # Uses a small, pre-filtered local extract of OpenStreetMap's
        # natural=coastline data, rather than fetching a global coastline
        # dataset fresh every run. OSM's natural=coastline tag
        # specifically marks the outer ocean coast, distinct from inland
        # water features (rivers/channels) — this matters in deltas and
        # other complex coastlines, where a generic "coastline" layer
        # (e.g. NASA GIBS's WMS Coastlines layer) can include confusing
        # internal channel/bar boundaries alongside the true outer coast.
        # Drawing it as vector line segments (rather than fetching a
        # rasterized image and resampling it into this frame) avoids
        # pixelation — a rasterize-then-resample pipeline is inherently
        # lossier than drawing vector lines directly at final resolution.
        if coastline_geojson_path:
            try:
                with open(coastline_geojson_path) as _f:
                    coast_geojson = json.load(_f)

                segments_drawn = 0
                for feature in coast_geojson.get("features", []):
                    geom = feature.get("geometry", {})
                    if geom.get("type") != "LineString":
                        continue

                    coords = geom.get("coordinates", [])
                    prev_px = None
                    for coord_lon, coord_lat in coords:
                        px = project_point(coord_lat, coord_lon)
                        if prev_px is not None:
                            draw.line([prev_px, px], fill=(255, 255, 255), width=2)
                            segments_drawn += 1
                        prev_px = px

                print(f"COASTLINE OVERLAY: drew {segments_drawn} line segments from local OSM extract")
            except Exception as e:
                print("COASTLINE OVERLAY FAILED (continuing without it):", e)

        # --- Water bodies overlay (inland sites: lakes, rivers) ---
        # Drawn on a separate RGBA overlay at 50% opacity then composited,
        # which gives the visual equivalent of a ~0.5 px line weight.
        if water_bodies_geojson_path:
            try:
                with open(water_bodies_geojson_path) as _wf:
                    wb_geojson = json.load(_wf)
                wb_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                wb_draw = ImageDraw.Draw(wb_overlay)
                wb_segments = 0
                for feature in wb_geojson.get("features", []):
                    geom = feature.get("geometry", {})
                    gt = geom.get("type")
                    coords = geom.get("coordinates", [])
                    rings = []
                    if gt == "Polygon":
                        rings = coords[:1]
                    elif gt == "MultiPolygon":
                        for poly in coords:
                            if poly:
                                rings.append(poly[0])
                    for ring in rings:
                        prev_px = None
                        for c in ring:
                            px = project_point(c[1], c[0])
                            if prev_px is not None:
                                # Only draw segments where at least one endpoint is
                                # inside (or near) the image frame — avoids
                                # rendering long off-screen river reaches that cross
                                # into the frame as a single overwhelming diagonal.
                                margin = width_px * 0.15
                                in_frame = lambda p: (
                                    -margin <= p[0] <= width_px + margin
                                    and -margin <= p[1] <= height_px + margin
                                )
                                if in_frame(px) or in_frame(prev_px):
                                    wb_draw.line([prev_px, px], fill=(160, 195, 215, 100), width=1)
                                    wb_segments += 1
                            prev_px = px
                img = Image.alpha_composite(img.convert("RGBA"), wb_overlay).convert("RGB")
                draw = ImageDraw.Draw(img)
                print(f"WATER BODIES OVERLAY: drew {wb_segments} segments from OSM extract")
            except Exception as e:
                print("WATER BODIES OVERLAY FAILED (continuing without it):", e)

        # --- Label markers ---
        for point in points:
            if len(point) == 5:
                plat, plon, label_text, text_dy, text_dx = point
            elif len(point) == 4:
                plat, plon, label_text, text_dy = point
                text_dx = 12
            else:
                plat, plon, label_text = point
                text_dy = -10
                text_dx = 12

            x_px, y_px = project_point(plat, plon)

            marker_radius = 6
            draw.ellipse(
                [x_px - marker_radius, y_px - marker_radius, x_px + marker_radius, y_px + marker_radius],
                fill=(255, 60, 60), outline=(255, 255, 255), width=2,
            )

            text_x, text_y = x_px + text_dx, y_px + text_dy
            for tdx, tdy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                draw.text((text_x + tdx, text_y + tdy), label_text, font=font, fill=(0, 0, 0))
            draw.text((text_x, text_y), label_text, font=font, fill=(255, 255, 255))

        # --- Optional reference lines (e.g. an international border) ---
        for line in (reference_lines or []):
            lat1, lon1, lat2, lon2, _label = line
            p1 = project_point(lat1, lon1)
            p2 = project_point(lat2, lon2)
            num_dashes = 120
            for i in range(num_dashes):
                if i % 2 != 0:
                    continue
                t0, t1 = i / num_dashes, (i + 1) / num_dashes
                seg_p1 = (p1[0] + (p2[0] - p1[0]) * t0, p1[1] + (p2[1] - p1[1]) * t0)
                seg_p2 = (p1[0] + (p2[0] - p1[0]) * t1, p1[1] + (p2[1] - p1[1]) * t1)
                draw.line([seg_p1, seg_p2], fill=(180, 200, 210), width=1)

        # --- Arrow annotations (white labelled arrows pointing to named spots) ---
        # Format: (lat, lon, label [, label_dx [, label_dy]])
        # label_dx/dy: pixel offset from target where the arrow tail and label appear
        for ann in (arrow_annotations or []):
            ann_lat, ann_lon, ann_label = ann[0], ann[1], ann[2]
            ann_dx = ann[3] if len(ann) > 3 else -55
            ann_dy = ann[4] if len(ann) > 4 else -55
            try:
                import math as _math
                tx, ty = project_point(ann_lat, ann_lon)
                lx, ly = tx + ann_dx, ty + ann_dy
                draw.line([(lx, ly), (tx, ty)], fill=(255, 255, 255), width=3)
                angle = _math.atan2(ty - ly, tx - lx)
                head_len, head_angle = 14, _math.radians(28)
                for side in (+head_angle, -head_angle):
                    bx = tx - head_len * _math.cos(angle - side)
                    by = ty - head_len * _math.sin(angle - side)
                    draw.line([(tx, ty), (bx, by)], fill=(255, 255, 255), width=3)
                bb = draw.textbbox((0, 0), ann_label, font=font)
                lw, lh = bb[2] - bb[0], bb[3] - bb[1]
                text_x = (lx - lw) if ann_dx < 0 else lx
                text_y = ly - lh // 2
                for tdx, tdy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                    draw.text((text_x + tdx, text_y + tdy), ann_label, font=font, fill=(0, 0, 0))
                draw.text((text_x, text_y), ann_label, font=font, fill=(255, 255, 255))
            except Exception as _ae:
                print(f"ARROW ANNOTATION '{ann_label}' FAILED: {_ae}")

        # --- Scale bar (bottom-left corner) ---
        px_per_km = 1000 / meters_per_px
        bar_px = scale_km * px_per_km
        margin = 30
        bar_x0 = margin
        bar_y0 = height_px - margin - 10
        bar_x1 = bar_x0 + bar_px

        draw.line([(bar_x0, bar_y0), (bar_x1, bar_y0)], fill=(255, 255, 255), width=4)
        draw.line([(bar_x0, bar_y0 - 6), (bar_x0, bar_y0 + 6)], fill=(255, 255, 255), width=4)
        draw.line([(bar_x1, bar_y0 - 6), (bar_x1, bar_y0 + 6)], fill=(255, 255, 255), width=4)
        draw.text((bar_x0, bar_y0 + 8), f"{scale_km} km", font=font, fill=(255, 255, 255))

        out_buf = _io.BytesIO()
        img.save(out_buf, format="PNG")
        return out_buf.getvalue()

    except Exception as e:
        print("PLAIN IMAGE ANNOTATION FAILED (showing unannotated image instead):", e)
        return png_bytes


def stamp_timestamp(png_bytes, dt_local, label="Acquired"):
    """
    Draws a timestamp in the upper-right corner of a satellite image, in
    the site's local time, so it's visually obvious the image is not
    real-time. Returns the stamped PNG bytes, or the original bytes
    unchanged if stamping fails for any reason.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io

        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        width_px, height_px = img.size

        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        except Exception:
            font = ImageFont.load_default()

        text = f"{label}: {dt_local.strftime('%Y-%m-%d %H:%M %Z')}"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        margin = 18
        text_x = width_px - text_w - margin
        text_y = margin

        for tdx, tdy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
            draw.text((text_x + tdx, text_y + tdy), text, font=font, fill=(0, 0, 0))
        draw.text((text_x, text_y), text, font=font, fill=(255, 255, 255))

        out_buf = _io.BytesIO()
        img.save(out_buf, format="PNG")
        return out_buf.getvalue()

    except Exception as e:
        print("TIMESTAMP STAMP FAILED (showing unstamped image instead):", e)
        return png_bytes


def fetch_and_process_modis(bbox_3413, center_x, center_y, rotation_deg, points,
                             now_utc, tz_name, half_width_m=150_000, reference_lines=None,
                             coastline_geojson_path=None):
    """
    Wraps the full MODIS fetch-rotate-annotate-stamp chain as a single
    function, so it can run concurrently with the other independent
    top-level data fetches (water level, Sentinel-1) via a thread pool.
    """
    modis_bytes, modis_date = fetch_modis_image(bbox_3413, now_utc)
    if modis_bytes:
        modis_bytes = rotate_to_north_up(modis_bytes, rotation_deg)
    if modis_bytes:
        modis_bytes = annotate_modis_image(
            modis_bytes, points=points, center_x=center_x, center_y=center_y,
            rotation_deg=rotation_deg, half_width_m=half_width_m, reference_lines=reference_lines,
            coastline_geojson_path=coastline_geojson_path,
        )
    if modis_bytes and modis_date:
        try:
            modis_dt_utc = datetime.strptime(modis_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            modis_bytes = stamp_timestamp(modis_bytes, to_local_time(modis_dt_utc.replace(tzinfo=None), tz_name), label="Acquired")
        except Exception as e:
            print("MODIS TIMESTAMP STAMP FAILED:", e)
    return modis_bytes, modis_date


# =========================================================
# MODULE — SATELLITE: Sentinel-1 SAR via Sentinel Hub / Copernicus
# Data Space Ecosystem
# =========================================================
def get_sentinel_hub_token():
    """
    Obtains an OAuth2 access token from Copernicus Data Space Ecosystem's
    identity service using client credentials (read from
    SENTINEL_HUB_CLIENT_ID/SECRET environment variables — the same two
    secret names every site repo provides, just with different values).
    Returns the token string, or None on failure.
    """
    client_id = os.environ.get("SENTINEL_HUB_CLIENT_ID")
    client_secret = os.environ.get("SENTINEL_HUB_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("SENTINEL-1: credentials not found in environment, skipping")
        return None

    try:
        token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
        resp = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as e:
        print("SENTINEL-1 TOKEN REQUEST FAILED:", e)
        return None


def _point_in_ring(lon, lat, ring):
    """
    Standard ray-casting point-in-polygon test against a single linear
    ring (list of [lon, lat] coordinate pairs). No new dependency (e.g.
    shapely) needed for this — it's a compact, well-known algorithm.
    Capped at 1000 vertices as a defensive bound, since this processes
    externally-controlled API data with no guaranteed upper bound on
    complexity.
    """
    if len(ring) > 1000:
        ring = ring[:1000]
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_covered_by_geometry(lon, lat, geometry, half_width_deg_lon, half_width_deg_lat, grid_n=5):
    """
    Checks coverage with two real, distinct requirements:
    1. The exact center point (where the site's marker and label
       actually render) must be covered, with a small real margin
       around it — this is the requirement that actually matters for
       "is the dot on real data."
    2. At least some reasonable minimum of the wider display frame must
       also be covered (so the image isn't almost entirely gray), but
       NOT a strict majority — a long narrow Sentinel-1 swath can
       legitimately leave large parts of a 300km square frame uncovered
       while still giving a perfectly usable image.
    """
    if not geometry:
        return False
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        rings = [coords[0]]
    elif gtype == "MultiPolygon":
        rings = [poly[0] for poly in coords]
    else:
        return False

    center_margin_frac = 0.15
    center_test_points = [
        (lon, lat),
        (lon - half_width_deg_lon * center_margin_frac, lat),
        (lon + half_width_deg_lon * center_margin_frac, lat),
        (lon, lat - half_width_deg_lat * center_margin_frac),
        (lon, lat + half_width_deg_lat * center_margin_frac),
    ]
    for tlon, tlat in center_test_points:
        if not any(_point_in_ring(tlon, tlat, ring) for ring in rings):
            return False

    offsets = [-1.0, -0.5, 0.0, 0.5, 1.0][:grid_n] if grid_n == 5 else \
        [i / (grid_n - 1) * 2 - 1 for i in range(grid_n)]

    test_points = [
        (lon + dx * half_width_deg_lon, lat + dy * half_width_deg_lat)
        for dx in offsets for dy in offsets
    ]

    covered_count = sum(
        1 for tlon, tlat in test_points
        if any(_point_in_ring(tlon, tlat, ring) for ring in rings)
    )
    return covered_count / len(test_points) >= 0.20


def find_latest_sentinel1_date(token, lat, lon, site_label, lookback_days=10, half_width_km=150, now_utc=None):
    """
    Searches the Catalog API for the most recent Sentinel-1 GRD scene
    that actually covers the given site within the lookback window.
    Accepts both IW and EW acquisition modes — Arctic sites often only
    have EW coverage, and restricting to IW silently produces blank images.

    Returns (date_str, full_datetime, acq_mode, band, pol_filter) where:
      acq_mode: "IW" or "EW"
      band:     "VV" (IW) or "HH" (EW) — HH gives better sea/ice contrast
      pol_filter: "DV" or "DH" — used in the Process API dataFilter
    Returns (None, None, None, None, None) on failure.
    """
    if now_utc is None:
        now_utc = datetime.utcnow()
    try:
        url = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"
        date_to = now_utc
        date_from = now_utc - timedelta(days=lookback_days)

        lat_buffer = half_width_km / 111
        lon_buffer = half_width_km / (111 * math.cos(math.radians(lat)))
        search_bbox = [lon - lon_buffer, lat - lat_buffer, lon + lon_buffer, lat + lat_buffer]

        body = {
            "bbox": search_bbox,
            "datetime": f"{date_from.strftime('%Y-%m-%dT%H:%M:%SZ')}/{date_to.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "collections": ["sentinel-1-grd"],
            "limit": 20,
        }
        resp = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            print(f"SENTINEL-1: no scenes found in catalog search window for {site_label}")
            return None, None, None, None, None

        half_width_deg_lat = half_width_km / 111
        half_width_deg_lon = half_width_km / (111 * math.cos(math.radians(lat)))

        covering_features = []
        for f in features:
            geometry = f.get("geometry")
            if geometry and _point_covered_by_geometry(lon, lat, geometry, half_width_deg_lon, half_width_deg_lat):
                covering_features.append(f)
                continue
            if not geometry:
                fbbox = f.get("bbox")
                if fbbox and len(fbbox) >= 4:
                    fminx, fminy, fmaxx, fmaxy = fbbox[0], fbbox[1], fbbox[2], fbbox[3]
                    bbox_margin_lon = half_width_deg_lon * 0.15
                    bbox_margin_lat = half_width_deg_lat * 0.15
                    if (fminx + bbox_margin_lon <= lon <= fmaxx - bbox_margin_lon and
                            fminy + bbox_margin_lat <= lat <= fmaxy - bbox_margin_lat):
                        covering_features.append(f)

        if not covering_features:
            print(f"SENTINEL-1: scenes found nearby, but none actually cover {site_label} with margin")
            return None, None, None, None, None

        covering_features.sort(key=lambda f: f["properties"]["datetime"], reverse=True)
        best = covering_features[0]
        latest_datetime = best["properties"]["datetime"]

        props = best.get("properties", {})
        acq_mode = props.get("sar:instrument_mode", "IW")
        polarizations = props.get("sar:polarizations", [])
        if "HH" in polarizations:
            band, pol_filter = "HH", "DH"
        else:
            band, pol_filter = "VV", "DV"

        print(f"SENTINEL-1: latest scene covering {site_label}: {latest_datetime} mode={acq_mode} band={band}")
        return latest_datetime[:10], latest_datetime, acq_mode, band, pol_filter

    except Exception as e:
        print("SENTINEL-1 CATALOG SEARCH FAILED:", e)
        return None, None, None, None, None


def fetch_sentinel1_image(token, date_str, center_x, center_y, utm_epsg,
                           half_width_m=150_000, output_size_px=MODIS_FINAL_SIZE_PX,
                           acq_mode="IW", band="VV", pol_filter="DV"):
    """
    Requests a gamma0 orthorectified Sentinel-1 GRD image for the given
    date, reprojected server-side to the site's UTM zone.

    acq_mode / band / pol_filter are passed through from
    find_latest_sentinel1_date so the image request matches the actual
    scene (IW+VV or EW+HH). Arctic sites typically only have EW coverage;
    hardcoding IW here used to silently produce blank images.

    The dB range is clipped to -22 to -10 dB, focused on water vs. ice:
    open water (~-20 to -25 dB) maps near black, sea ice (~-15 to -10 dB)
    maps to mid-grey, and land (above -10 dB) clips to white. This avoids
    land backscatter compressing the water/ice contrast range.

    Returns PNG bytes, or None on failure.
    """
    try:
        minx = center_x - half_width_m
        maxx = center_x + half_width_m
        miny = center_y - half_width_m
        maxy = center_y + half_width_m

        # dB range tuned for land surface backscatter (tundra, coastline):
        # -15 dB (dark rock/wet soil) to +3 dB (rough terrain / urban).
        # The previous [-22, -10] range was tuned for water/ice and clipped
        # all land to white; this range shows land texture naturally.
        evalscript = f"""
        //VERSION=3
        function setup() {{
          return {{
            input: ["{band}", "dataMask"],
            output: {{ bands: 2, sampleType: "UINT8" }}
          }};
        }}
        function evaluatePixel(samples) {{
          if (samples.dataMask == 0) {{
            return [0, 0];
          }}
          var db = 10 * Math.log(Math.max(samples.{band}, 1e-10)) / Math.LN10;
          var clipped = Math.max(-15, Math.min(0, db));
          var linear = (clipped + 15) / 15;
          // Gamma 0.5 (square-root) brightens dark tundra without clipping peaks.
          var gray = Math.round(Math.sqrt(linear) * 255);
          return [gray, 255];
        }}
        """

        request_body = {
            "input": {
                "bounds": {
                    "bbox": [minx, miny, maxx, maxy],
                    "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{utm_epsg}"},
                },
                "data": [
                    {
                        "type": "sentinel-1-grd",
                        "dataFilter": {
                            "timeRange": {
                                "from": f"{date_str}T00:00:00Z",
                                "to": f"{date_str}T23:59:59Z",
                            },
                            "acquisitionMode": acq_mode,
                            "polarization": pol_filter,
                        },
                        "processing": {
                            "backCoeff": "GAMMA0_ELLIPSOID",
                            "orthorectify": "true",
                        },
                    }
                ],
            },
            "output": {
                "width": output_size_px,
                "height": output_size_px,
                "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
            },
            "evalscript": evalscript,
        }

        resp = requests.post(
            "https://sh.dataspace.copernicus.eu/api/v1/process",
            json=request_body,
            headers={"Authorization": f"Bearer {token}", "Accept": "image/png"},
            timeout=60,
        )
        resp.raise_for_status()

        if resp.content[:8] != b"\x89PNG\r\n\x1a\n":
            print("SENTINEL-1: response was not a valid PNG")
            return None

        return resp.content

    except Exception as e:
        print("SENTINEL-1 IMAGE FETCH FAILED:", e)
        return None


def fetch_and_process_sentinel1(lat, lon, site_label, utm_zone, utm_epsg,
                                 center_x, center_y, points, tz_name,
                                 half_width_m=150_000, reference_lines=None,
                                 coastline_geojson_path=None, now_utc=None):
    """
    Wraps the full Sentinel-1 token-catalog-image-annotate-stamp chain as
    a single function, for the same concurrent-top-level-fetch reason as
    fetch_and_process_modis. The internal steps stay sequential (each
    needs the previous step's result), but the whole chain runs
    concurrently with MODIS and water level when called via a thread pool.

    Returns (sentinel1_bytes, sentinel1_caption).
    """
    import functools

    sentinel1_bytes = None
    sentinel1_caption = "Sentinel-1 SAR image unavailable — credentials missing or fetch failed. Check Action logs."

    sh_token = get_sentinel_hub_token()
    if sh_token:
        s1_date, s1_full_datetime, acq_mode, band, pol_filter = find_latest_sentinel1_date(
            sh_token, lat, lon, site_label, now_utc=now_utc
        )
        if s1_date:
            s1_raw = fetch_sentinel1_image(
                sh_token, s1_date, center_x, center_y, utm_epsg, half_width_m,
                acq_mode=acq_mode, band=band, pol_filter=pol_filter,
            )
            if s1_raw:
                from PIL import Image
                import io as _io
                rgba_img = Image.open(_io.BytesIO(s1_raw)).convert("RGBA")
                background = Image.new("RGBA", rgba_img.size, (50, 50, 50, 255))
                composited = Image.alpha_composite(background, rgba_img).convert("RGB")
                buf = _io.BytesIO()
                composited.save(buf, format="PNG")
                project_fn = functools.partial(latlon_to_utm, zone=utm_zone)
                sentinel1_bytes = annotate_plain_image(
                    buf.getvalue(), points=points, center_x=center_x, center_y=center_y,
                    project_fn=project_fn, lat=lat, lon=lon, half_width_m=half_width_m,
                    reference_lines=reference_lines, coastline_geojson_path=coastline_geojson_path,
                )
                s1_local_str = s1_date
                try:
                    s1_dt_utc = datetime.strptime(s1_full_datetime[:19], "%Y-%m-%dT%H:%M:%S")
                    s1_local = to_local_time(s1_dt_utc, tz_name)
                    sentinel1_bytes = stamp_timestamp(sentinel1_bytes, s1_local, label="Acquired")
                    tz_abbr = s1_local.strftime("%Z")
                    s1_local_str = s1_local.strftime(f"%b %d, %Y, %H:%M {tz_abbr}")
                except Exception as e:
                    print("SENTINEL-1 TIMESTAMP STAMP FAILED:", e)
                sentinel1_caption = (
                    f"Sentinel-1 SAR ({band} dB gamma0, {acq_mode} mode). "
                    f"Acquired: {s1_local_str}. "
                    f"Open water appears dark; land and sea ice appear bright; "
                    f"dark gray areas were outside the satellite swath. "
                    f"Source: Copernicus Sentinel-1 via Sentinel Hub."
                )
    return sentinel1_bytes, sentinel1_caption


def fetch_sentinel1_ice_image(token, date_str, center_x, center_y, utm_epsg,
                               half_width_m=150_000, output_size_px=MODIS_FINAL_SIZE_PX,
                               acq_mode="EW", band="HH", pol_filter="DH"):
    """
    Requests a sea-ice classification image from Sentinel-1 HH backscatter.
    Uses a three-segment colour ramp keyed to sigma-nought in dB:
      -22 dB → navy blue  (open water, low return)
      -15 dB → cyan       (marginal ice / wind-roughened water)
       -5 dB → white      (sea ice / bright surface)
    Returns PNG bytes or None on failure.
    """
    try:
        minx = center_x - half_width_m
        maxx = center_x + half_width_m
        miny = center_y - half_width_m
        maxy = center_y + half_width_m

        # HH-only classifier for summer Arctic sea ice.
        #
        # All polarimetric approaches (HH−HV ratio, HV absolute) fail in July:
        # summer melt-pond ice has HV at or below the EW NESZ (~−22 to −25 dB),
        # making it indistinguishable from rough open water in cross-pol.
        # HH backscatter is the only reliable signal available at C-band in
        # summer: open water is dark (specular or low Bragg), sea ice is bright
        # (surface roughness + volume scattering from ice matrix below ponds).
        #
        # 3-category scheme (turbid river-delta water may appear as marginal/ice):
        #   Open water   HH < −21 dB   dark navy
        #   Marginal     −21 to −14 dB cyan-blue ramp
        #   Sea ice      HH > −14 dB   white ramp
        evalscript = f"""
        //VERSION=3
        function setup() {{
          return {{
            input: ["{band}", "dataMask"],
            output: {{ bands: 4, sampleType: "UINT8" }}
          }};
        }}
        function evaluatePixel(samples) {{
          if (samples.dataMask == 0) {{
            return [40, 45, 50, 255];
          }}
          var hh_db = 10 * Math.log(Math.max(samples.{band}, 1e-10)) / Math.LN10;

          // Open water: low total backscatter (specular/calm or deep Bragg)
          if (hh_db < -21.0) {{
            var t = Math.max(0, Math.min(1, (hh_db + 30.0) / 9.0));
            return [
              Math.round(5  + t * 10),
              Math.round(15 + t * 55),
              Math.round(80 + t * 90),
              255
            ];
          }}

          // Marginal ice: intermediate backscatter — cyan-blue ramp
          if (hh_db < -14.0) {{
            var m = Math.max(0, Math.min(1, (hh_db + 21.0) / 7.0)); // 0=−21dB, 1=−14dB
            return [
              Math.round(30  + m * 60),
              Math.round(130 + m * 110),
              Math.round(215 + m * 25),
              255
            ];
          }}

          // Sea ice: bright surface — cyan to white ramp
          var p = Math.max(0, Math.min(1, (hh_db + 14.0) / 8.0)); // 0=−14dB, 1=−6dB
          return [
            Math.round(90  + p * 165),
            Math.round(220 + p * 35),
            Math.round(240 + p * 15),
            255
          ];
        }}
        """

        request_body = {
            "input": {
                "bounds": {
                    "bbox": [minx, miny, maxx, maxy],
                    "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{utm_epsg}"},
                },
                "data": [
                    {
                        "type": "sentinel-1-grd",
                        "dataFilter": {
                            "timeRange": {
                                "from": f"{date_str}T00:00:00Z",
                                "to": f"{date_str}T23:59:59Z",
                            },
                            "acquisitionMode": acq_mode,
                            "polarization": pol_filter,
                        },
                        "processing": {
                            "backCoeff": "GAMMA0_ELLIPSOID",
                            "orthorectify": "true",
                            # Lee 5×5 speckle filter: reduces salt-and-pepper noise
                            # before thresholding so single bright pixels (ships,
                            # specular returns) do not drive the classifier.
                            "speckleFilter": {
                                "type": "LEE",
                                "windowSizeX": 5,
                                "windowSizeY": 5,
                            },
                        },
                    }
                ],
            },
            "output": {
                "width": output_size_px,
                "height": output_size_px,
                "responses": [{"identifier": "default", "format": {"type": "image/png"}}],
            },
            "evalscript": evalscript,
        }

        resp = requests.post(
            "https://sh.dataspace.copernicus.eu/api/v1/process",
            json=request_body,
            headers={"Authorization": f"Bearer {token}", "Accept": "image/png"},
            timeout=60,
        )
        resp.raise_for_status()

        if resp.content[:8] != b"\x89PNG\r\n\x1a\n":
            print("SENTINEL-1 ICE: response was not a valid PNG")
            return None

        return resp.content

    except Exception as e:
        print("SENTINEL-1 ICE IMAGE FETCH FAILED:", e)
        return None


def add_ice_classification_legend(png_bytes, ice_label="Sea ice"):
    """
    Draws a horizontal colour-ramp legend in the upper-left corner of the
    ice-classification image, with labels consistent with the timestamp font
    (DejaVu Sans Bold 22 px, white text with black outline).

    Legend layout (top-left, inside a semi-transparent dark panel):
      â”Œâ”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
      â”‚  Sentinel-1 HH σ° – ice/water estimate  â”‚
      â”‚  [â–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆâ–ˆ] â”‚
      â”‚  Open water         Marginal       Ice  â”‚
      â””â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”˜
    Returns the annotated PNG bytes, or the original on failure.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io

        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        W, H = img.size

        try:
            font_title = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
            font_label = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
        except Exception:
            font_title = font_label = ImageFont.load_default()

        categories = [
            ((5,  25, 120),   "Open water"),
            ((55, 180, 225),  "Marginal ice"),
            ((230, 245, 255), ice_label),
        ]

        # Size each swatch to fit its label — measure first, then lay out.
        title = "Sentinel-1 HH σ° — ice / water estimate"
        _tmp_draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_w = _tmp_draw.textbbox((0, 0), title, font=font_title)[2]
        label_widths = [_tmp_draw.textbbox((0, 0), lbl, font=font_label)[2] for _, lbl in categories]
        swatch_min_w = 18   # minimum swatch width regardless of label
        gap = 10
        swatch_ws = [max(swatch_min_w, lw + 8) for lw in label_widths]
        total_swatches_w = sum(swatch_ws) + gap * (len(categories) - 1)

        margin = 18
        pad = 8
        bar_h = 18
        title_h = 26
        label_h = 20
        panel_w = min(max(total_swatches_w, title_w) + 2 * pad, W - 2 * margin)
        panel_h = pad + title_h + pad + bar_h + pad + label_h + pad

        # Semi-transparent dark panel
        panel = Image.new("RGBA", (panel_w, panel_h), (20, 20, 20, 170))
        img_rgba = img.convert("RGBA")
        img_rgba.paste(panel, (margin, margin), panel)
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img)

        px0 = margin + pad
        py0 = margin + pad

        # Title
        for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
            draw.text((px0 + dx, py0 + dy), title, font=font_title, fill=(0, 0, 0))
        draw.text((px0, py0), title, font=font_title, fill=(255, 255, 255))

        # 3-category discrete swatches, each wide enough for its own label.
        bar_y = py0 + title_h + pad
        label_y = bar_y + bar_h + pad
        sx = px0
        for ci, ((color, lbl), sw) in enumerate(zip(categories, swatch_ws)):
            draw.rectangle([sx, bar_y, sx + sw - 1, bar_y + bar_h - 1], fill=color)
            draw.rectangle([sx - 1, bar_y - 1, sx + sw, bar_y + bar_h],
                           outline=(180, 180, 180), width=1)
            lw = label_widths[ci]
            tx = sx + (sw - lw) // 2
            for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1)]:
                draw.text((tx + dx, label_y + dy), lbl, font=font_label, fill=(0, 0, 0))
            draw.text((tx, label_y), lbl, font=font_label, fill=(255, 255, 255))
            sx += sw + gap

        out = _io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    except Exception as e:
        print("ICE LEGEND FAILED:", e)
        return png_bytes


def _make_sea_mask(coastline_geojson_path, center_x, center_y, utm_zone, half_width_m, output_size_px):
    """
    Rasterises the coastline GeoJSON as thick lines on a binary mask, then
    flood-fills from the top-centre pixel (assumed to be Beaufort Sea / open
    ocean for all Arctic coastal sites where north = sea).
    Returns a boolean numpy array, True = sea pixel, False = land pixel.
    On any failure returns all-True (treat everything as sea).
    Uses latlon_to_utm so no extra dependencies are needed.
    """
    import json
    import numpy as np
    from PIL import Image as _PI, ImageDraw as _PID

    w = h = output_size_px
    mask = _PI.new("L", (w, h), 0)

    if coastline_geojson_path:
        try:
            def _to_px(lon_val, lat_val):
                x, y = latlon_to_utm(lat_val, lon_val, zone=utm_zone)
                px = int((x - (center_x - half_width_m)) / (2 * half_width_m) * w)
                py = int(h - (y - (center_y - half_width_m)) / (2 * half_width_m) * h)
                return (max(-10, min(w + 10, px)), max(-10, min(h + 10, py)))

            with open(coastline_geojson_path) as f:
                geojson = json.load(f)

            draw = _PID.Draw(mask)
            for feature in geojson.get("features", []):
                geom = feature.get("geometry", {})
                gt = geom.get("type")
                coords = geom.get("coordinates", [])
                lines = ([coords] if gt == "LineString"
                         else (coords if gt == "MultiLineString" else []))
                for line in lines:
                    pts = [_to_px(c[0], c[1]) for c in line]
                    if len(pts) >= 2:
                        draw.line(pts, fill=200, width=6)
        except Exception as e:
            print(f"SEA MASK COASTLINE RASTERISE FAILED: {e}")

    # Seal left, right, and bottom edges so flood-fill can't leak around the
    # coastline via the image border (top is left open — that's the sea seed).
    _draw2 = _PID.Draw(mask)
    _e = 4
    _draw2.rectangle([0, 0, _e, h - 1], fill=200)
    _draw2.rectangle([w - 1 - _e, 0, w - 1, h - 1], fill=200)
    _draw2.rectangle([0, h - 1 - _e, w - 1, h - 1], fill=200)

    try:
        _PID.floodfill(mask, (w // 2, 3), 128)
    except Exception as e:
        print(f"SEA MASK FLOOD FILL FAILED: {e}")
        return np.ones((h, w), dtype=bool)

    return np.array(mask) == 128


def _nice_scale_km(half_width_m):
    """Returns a 'nice' scale bar length in km for the given frame half-width."""
    target = half_width_m / 1000 / 3
    for n in reversed([1, 2, 5, 10, 20, 50, 100, 200, 500]):
        if n <= target:
            return n
    return 1


def _composite_sea_color_land_gray(color_bytes, gray_bytes, sea_mask):
    """
    Composites two Sentinel-1 images: sea pixels from the color ramp image,
    land pixels from the standard grayscale image.
    Returns PNG bytes; falls back to color_bytes on any error.
    """
    import io
    import numpy as np
    from PIL import Image as _PI

    try:
        color_img = _PI.open(io.BytesIO(color_bytes)).convert("RGBA")
        gray_img  = _PI.open(io.BytesIO(gray_bytes)).convert("RGBA")
        if gray_img.size != color_img.size:
            gray_img = gray_img.resize(color_img.size, _PI.LANCZOS)

        color_arr = np.array(color_img, dtype=np.uint8)
        gray_arr  = np.array(gray_img,  dtype=np.uint8)

        mask_4 = sea_mask[:, :, np.newaxis]           # broadcast across RGBA
        composite = np.where(mask_4, color_arr, gray_arr)

        out = _PI.fromarray(composite.astype(np.uint8), "RGBA")
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print(f"SEA/LAND COMPOSITE FAILED: {e}")
        return color_bytes


def fetch_and_process_sentinel1_ice(lat, lon, site_label, utm_zone, utm_epsg,
                                     center_x, center_y, points, tz_name,
                                     half_width_m=150_000, reference_lines=None,
                                     coastline_geojson_path=None, now_utc=None,
                                     arrow_annotations=None):
    """
    Fetches a Sentinel-1 sea-ice classification image for the same scene
    as fetch_and_process_sentinel1, using a colour-ramp evalscript instead
    of grayscale. Adds the same coastline overlay, timestamp stamp, and
    an ice/water legend. Returns (ice_bytes, ice_caption).
    """
    import functools

    sh_token = get_sentinel_hub_token()
    if not sh_token:
        return None, "Sea ice classification unavailable — Sentinel Hub credentials missing."

    s1_date, s1_full_datetime, acq_mode, band, pol_filter = find_latest_sentinel1_date(
        sh_token, lat, lon, site_label, now_utc=now_utc
    )
    if not s1_date:
        return None, "Sea ice classification unavailable — no recent Sentinel-1 scene found."

    if band != "HH":
        return None, (
            "Sea ice classification requires HH polarisation (EW mode). "
            f"Latest scene uses {band} — classification not available for this acquisition."
        )

    # Fetch colour-ramp and grayscale images in parallel (same scene)
    from concurrent.futures import ThreadPoolExecutor as _TPEX
    with _TPEX(max_workers=2) as _ex:
        _color_f = _ex.submit(
            fetch_sentinel1_ice_image,
            sh_token, s1_date, center_x, center_y, utm_epsg, half_width_m,
            acq_mode=acq_mode, band=band, pol_filter=pol_filter,
        )
        _gray_f = _ex.submit(
            fetch_sentinel1_image,
            sh_token, s1_date, center_x, center_y, utm_epsg, half_width_m,
            acq_mode=acq_mode, band=band, pol_filter=pol_filter,
        )
        raw_color = _color_f.result()
        raw_gray  = _gray_f.result()

    if not raw_color:
        return None, "Sea ice classification unavailable — image fetch failed."

    # Composite: sea pixels → colour ramp, land pixels → SAR grayscale
    sea_mask = _make_sea_mask(
        coastline_geojson_path, center_x, center_y, utm_zone, half_width_m,
        MODIS_FINAL_SIZE_PX,
    )
    if raw_gray is not None and sea_mask is not None:
        raw_color = _composite_sea_color_land_gray(raw_color, raw_gray, sea_mask)

    project_fn = functools.partial(latlon_to_utm, zone=utm_zone)
    ice_bytes = annotate_plain_image(
        raw_color, points=points, center_x=center_x, center_y=center_y,
        project_fn=project_fn, lat=lat, lon=lon, half_width_m=half_width_m,
        scale_km=_nice_scale_km(half_width_m),
        reference_lines=reference_lines, coastline_geojson_path=coastline_geojson_path,
        arrow_annotations=arrow_annotations,
    )
    ice_bytes = add_ice_classification_legend(ice_bytes)

    try:
        s1_dt_utc = datetime.strptime(s1_full_datetime[:19], "%Y-%m-%dT%H:%M:%S")
        s1_local = to_local_time(s1_dt_utc, tz_name)
        ice_bytes = stamp_timestamp(ice_bytes, s1_local, label="Acquired")
        tz_abbr = s1_local.strftime("%Z")
        s1_local_str = s1_local.strftime(f"%b %d, %Y, %H:%M {tz_abbr}")
    except Exception as e:
        print("ICE IMAGE TIMESTAMP STAMP FAILED:", e)
        s1_local_str = s1_date

    ice_caption = (
        f"Sentinel-1 HH+HV sea-ice estimate ({acq_mode} mode), acquired {s1_local_str}. "
        f"Sea classification uses the HH−HV polarization difference to separate rough open water "
        f"(high HH−HV, teal) from sea ice (low HH−HV, cyan→white). "
        f"Land areas shown in standard SAR grayscale (−15 to +3 dB). "
        f"Lee 5×5 speckle filter applied before classification. "
        f"Classification is most reliable in cold conditions; summer melt reduces the polarimetric "
        f"contrast between ice and wind-roughened water. "
        f"Source: Copernicus Sentinel-1 via Sentinel Hub."
    )

    return ice_bytes, ice_caption


def build_sea_ice_section(ice_bytes, ice_caption, site_label, title="🧊 Sea Ice — Sentinel-1 Classification", filename="sea_ice.png"):
    """Builds the Notion blocks for the sea-ice classification image."""
    heading_block = heading(title)
    if ice_bytes:
        try:
            uid = upload_image_to_notion(ice_bytes, filename)
            img_block = image_block_from_upload(uid)
        except Exception as e:
            print("SEA ICE NOTION UPLOAD FAILED:", e)
            img_block = paragraph(f"Sea ice image could not be uploaded: {e}")
    else:
        img_block = paragraph(
            f"Sea ice classification unavailable for {site_label}. "
            "This section requires a recent Sentinel-1 EW/HH acquisition."
        )
    return [heading_block, img_block, gray_caption(ice_caption)]


# =========================================================
# MODULE — TIDES & SEA LEVEL (DFO Canadian Hydrographic Service, IWLS API)
# Unlike the old SPINE API (which only covers the St. Lawrence and never
# had Arctic coverage), IWLS hosts real tide-table stations across
# Canada including the Arctic. Station IDs are internal UUIDs, not the
# public 5-digit code, so we resolve the code to an ID first, then
# request water level predictions (wlp) for that station.
# =========================================================
def find_iwls_station_id(code):
    try:
        resp = requests.get("https://api-iwls.dfo-mpo.gc.ca/api/v1/stations", timeout=30)
        resp.raise_for_status()
        stations = resp.json()
    except Exception as e:
        print("TIDES: failed to fetch IWLS station list:", e)
        return None

    for s in stations:
        if s.get("code") == code:
            return s.get("id")

    print(f"TIDES: station code {code} not found in IWLS station list")
    return None


def fetch_tide_predictions(station_id, now_utc, hours_ahead=24):
    from_dt = now_utc
    to_dt = now_utc + timedelta(hours=hours_ahead)
    url = f"https://api-iwls.dfo-mpo.gc.ca/api/v1/stations/{station_id}/data"
    params = {
        "time-series-code": "wlp",
        "from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("TIDES: failed to fetch predictions:", e)
        return None


def fetch_tide_predictions_noaa(station_id, now_utc, hours_ahead=168):
    """
    Fetches tide predictions from NOAA CO-OPS for a US station.
    Returns data in the same [{\"eventDate\": ISO, \"value\": float}] format
    as fetch_tide_predictions() so build_tide_chart() can be reused unchanged.
    station_id: NOAA numeric station ID string, e.g. \"9497645\".
    """
    begin_dt = now_utc
    end_dt = now_utc + timedelta(hours=hours_ahead)
    url = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    params = {
        "product": "predictions",
        "station": station_id,
        "datum": "MLLW",
        "time_zone": "GMT",
        "interval": "6",
        "units": "metric",
        "application": "arctic_dashboard",
        "format": "json",
        "begin_date": begin_dt.strftime("%Y%m%d"),
        "end_date": end_dt.strftime("%Y%m%d"),
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            print(f"NOAA TIDES: API error for {station_id}: {data['error']}")
            return None
        normalized = []
        for p in data.get("predictions", []):
            try:
                dt = datetime.strptime(p["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                normalized.append({
                    "eventDate": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "value": float(p["v"]),
                })
            except (ValueError, TypeError, KeyError):
                continue
        return normalized if normalized else None
    except Exception as e:
        print(f"NOAA TIDES: failed to fetch predictions for station {station_id}: {e}")
        return None


def format_tide_text(tide_points, now_utc, station_code, station_name):
    """
    Builds display-ready tide text (see build_bolded_lines) from raw
    IWLS prediction points. station_name is used in both the success and
    failure text, so a new site's tide section reads correctly without
    needing separate hand-written strings per site.
    """
    if not tide_points:
        return (
            f"Tide prediction data unavailable for {station_name} station ({station_code}).\n"
            "Check Action logs — this uses DFO's IWLS API, which requires resolving "
            "the station code to an internal station ID first; if DFO changes that "
            "station's status or the API shape, this lookup may need adjustment."
        )

    closest = min(
        tide_points,
        key=lambda p: abs(datetime.fromisoformat(p["eventDate"].replace("Z", "+00:00")) - now_utc.replace(tzinfo=timezone.utc)),
    )
    current_level = closest.get("value")

    sorted_points = sorted(tide_points, key=lambda p: p["eventDate"])
    levels = [p["value"] for p in sorted_points]
    next_max = max(levels) if levels else None
    next_min = min(levels) if levels else None

    return [
        ("Predicted water level (now): ", f"{current_level:.2f} m"),
        ["Next 24h range: ", ("", f"{next_min:.2f} m"), " to ", ("", f"{next_max:.2f} m")],
        f"Reference: chart datum, {station_name} station ({station_code})",
    ]


def build_tide_chart(tide_points, now_utc, tz_name):
    """
    Renders the 7-day water level predictions already fetched as a
    chart, styled consistently with the temperature and sun position
    charts.
    """
    if not tide_points:
        return None, "Tide chart unavailable — no prediction data."

    try:
        tz = ZoneInfo(tz_name)
        sorted_points = sorted(tide_points, key=lambda p: p["eventDate"])
        times = [datetime.fromisoformat(p["eventDate"].replace("Z", "+00:00")) for p in sorted_points]
        levels = [p["value"] for p in sorted_points]

        t0 = times[0]
        hours = [(t - t0).total_seconds() / 3600 for t in times]

        current_idx = min(range(len(times)), key=lambda i: abs((times[i] - now_utc.replace(tzinfo=timezone.utc)).total_seconds()))
        current_hour = hours[current_idx]
        current_level = levels[current_idx]

        NOTION_BLUE = "#337EA9"
        NOTION_RED = "#E16259"
        NOTION_TEXT_GRAY = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(4.8, 3.0), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        ax.fill_between(hours, levels, min(levels), color=NOTION_BLUE, alpha=0.12, linewidth=0, zorder=1)
        ax.plot(hours, levels, linewidth=3, color=NOTION_BLUE, zorder=2)
        ax.plot([current_hour], [current_level], marker="o", markersize=10,
                 color=NOTION_RED, markeredgecolor="white", markeredgewidth=1.5, zorder=3)

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        ax.set_xlim(0, max(hours))

        # Build tick positions at clean clock hours, not fixed offsets
        # from t0's exact minute. Steps daily (24h) rather than every 6h,
        # since the window is 7 days — 6h ticks would crowd 28 labels.
        first_tick_time = t0.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if first_tick_time < t0:
            first_tick_time += timedelta(hours=1)
        tick_times = []
        t = first_tick_time
        while (t - t0).total_seconds() / 3600 <= max(hours):
            tick_times.append(t)
            t += timedelta(hours=24)
        tick_hours = [(t - t0).total_seconds() / 3600 for t in tick_times]
        tick_labels = [t.astimezone(tz).strftime("%b %d") for t in tick_times]
        ax.set_xticks(tick_hours)
        ax.set_xticklabels(tick_labels, fontsize=15, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
        ax.tick_params(axis="y", labelsize=15, colors=NOTION_TEXT_GRAY, length=0)
        ax.tick_params(axis="x", length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
        ax.set_ylabel("Water level (m)", fontsize=16, color=NOTION_TEXT_GRAY)

        x_range = max(hours) if hours else 24
        x_offset = x_range * 0.035
        ax.annotate(
            "now", xy=(current_hour, current_level),
            xytext=(current_hour + x_offset, current_level),
            color=NOTION_RED, fontsize=16, fontweight="bold",
            ha="left", va="center",
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8),
        )

        fig.tight_layout()
        png_bytes = fig_to_png_bytes(fig)
        caption = f"Predicted water level, next 7 days, starting {t0.astimezone(tz).strftime('%b %d, %H:%M %Z')}. Source: DFO/CHS IWLS."
        return png_bytes, caption

    except Exception as e:
        print("TIDE CHART FAILED:", e)
        return None, "Tide chart could not be generated — see Action logs."


# =========================================================
# MODULE — TOTAL WATER LEVEL (TOPAZ6 Arctic model, tide + storm surge)
# Unlike DFO IWLS (pure astronomical tide prediction from a station),
# this product is a 3km HYCOM model that includes both tides AND storm
# surge — i.e. the actual "total" water level signal, not just the
# predictable tidal component. Dataset and variable verified directly
# from Copernicus's own Product User Manual: dataset
# "dataset-topaz6-arc-15min-3km-be", variable "zos" (meters). Fetched via
# plain xarray against the public THREDDS OPeNDAP endpoint, since
# copernicusmarine's own open_dataset() reported no subset-compatible
# service for this dataset.
#
# This product covers the whole pan-Arctic TOPAZ6 domain, so it should
# work for any new site within that domain without changes beyond
# passing that site's own lat/lon — no separate per-site setup needed
# here, unlike MODIS/Sentinel-1's rotation angle and UTM zone.
# =========================================================
def fetch_copernicus_water_level(lat, lon, now_utc, site_label, yearly_mean=None):
    """
    Fetches the total water level (sea surface height, tide + storm
    surge) forecast for the next ~10 days near the given site from the
    TOPAZ6 Arctic tide/surge model.

    yearly_mean: the site's pre-computed yearly average water level (see
    build_yearly_mean_helper_script below for how to compute one for a
    new site) — passed through unchanged so the chart can plot relative
    to it; not computed live here (confirmed via real test runs to
    sometimes take 30+ seconds against the remote THREDDS server for a
    value that barely changes year to year).

    site_label is used only for log messages.

    Returns (times, values_m, yearly_mean) as parallel lists, or
    (None, None, None) on failure, so a problem here never blocks the
    rest of the dashboard.
    """
    try:
        import xarray as xr

        thredds_url = "https://thredds.met.no/thredds/dodsC/cmems/topaz6/dataset-topaz6-arc-15min-3km-be.ncml"

        # The grid is polar stereographic (x/y in meters) but NOT EPSG:3413
        # - it's a spherical, scale-factor-1-at-pole projection (see
        # latlon_to_topaz6_grid's docstring for how this was confirmed and
        # why using latlon_to_3413 here was silently selecting a grid cell
        # ~60km off at Herschel Island's latitude, fixed 2026-07-17).
        target_x_m, target_y_m = latlon_to_topaz6_grid(lat, lon)

        ds = xr.open_dataset(thredds_url)

        # This .ncml file's x/y coordinate variables are in units of
        # 100km (meters / 100,000), confirmed by cross-checking against
        # an independent third-party source (OpenDrift's debug log for
        # this same file).
        UNIT_SCALE = 100_000
        target_x = target_x_m / UNIT_SCALE
        target_y = target_y_m / UNIT_SCALE

        # The TOPAZ6 grid is 3km resolution; near a coastline, the single
        # geometrically-nearest cell can land on a masked/land grid
        # point (NaN). Search a small neighborhood and use the nearest
        # cell that actually has valid data.
        search_radius_m = 50_000
        search_radius = search_radius_m / UNIT_SCALE
        x_coords = ds["x"].values
        y_coords = ds["y"].values
        x_ascending = x_coords[0] < x_coords[-1] if len(x_coords) > 1 else True
        y_ascending = y_coords[0] < y_coords[-1] if len(y_coords) > 1 else True

        x_slice = (
            slice(target_x - search_radius, target_x + search_radius) if x_ascending
            else slice(target_x + search_radius, target_x - search_radius)
        )
        y_slice = (
            slice(target_y - search_radius, target_y + search_radius) if y_ascending
            else slice(target_y + search_radius, target_y - search_radius)
        )

        print(f"COPERNICUS WATER LEVEL DEBUG [{site_label}]: target (meters): x={target_x_m:.0f}, y={target_y_m:.0f}")
        print(f"COPERNICUS WATER LEVEL DEBUG [{site_label}]: target (native 100km units): x={target_x:.3f}, y={target_y:.3f}, search_radius={search_radius:.3f}")
        print(f"COPERNICUS WATER LEVEL DEBUG [{site_label}]: dataset x range: {x_coords.min():.0f} to {x_coords.max():.0f} ({'ascending' if x_ascending else 'descending'})")
        print(f"COPERNICUS WATER LEVEL DEBUG [{site_label}]: dataset y range: {y_coords.min():.0f} to {y_coords.max():.0f} ({'ascending' if y_ascending else 'descending'})")

        nearby = ds["zos"].sel(x=x_slice, y=y_slice)
        print(f"COPERNICUS WATER LEVEL DEBUG [{site_label}]: after x/y selection, nearby size={nearby.size}, dims={dict(nearby.sizes)}")

        # Strip timezone for xarray sel — dataset time coords are tz-naive.
        start = now_utc.replace(tzinfo=None)
        end = (now_utc + timedelta(days=10)).replace(tzinfo=None)

        time_coords = nearby["time"].values
        time_ascending = time_coords[0] < time_coords[-1] if len(time_coords) > 1 else True
        time_slice = slice(start, end) if time_ascending else slice(end, start)
        nearby = nearby.sel(time=time_slice)
        print(f"COPERNICUS WATER LEVEL DEBUG [{site_label}]: after time selection, nearby size={nearby.size}, dims={dict(nearby.sizes)}")

        if nearby.size == 0:
            print(f"COPERNICUS WATER LEVEL: no grid cells found near {site_label} in this window")
            return None, None, None

        has_valid_data = nearby.notnull().any(dim="time").values
        xs = nearby["x"].values
        ys = nearby["y"].values

        best_point = None
        best_dist = None
        for yi_idx, yi in enumerate(ys):
            for xi_idx, xi in enumerate(xs):
                if has_valid_data[yi_idx, xi_idx]:
                    dist = math.hypot(xi - target_x, yi - target_y)
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        best_point = (xi, yi)

        if best_point is None:
            print(f"COPERNICUS WATER LEVEL: no valid (non-NaN) grid cells found near {site_label}")
            return None, None, None

        print(f"COPERNICUS WATER LEVEL: using grid cell at distance {best_dist:.0f}m from {site_label}")
        point = nearby.sel(x=best_point[0], y=best_point[1])

        times = [str(t) for t in point["time"].values]
        raw_values = [float(v) for v in point.values.flatten()]

        times_clean = []
        values_clean = []
        for t, v in zip(times, raw_values):
            if not math.isnan(v):
                times_clean.append(t)
                values_clean.append(v)

        if not values_clean:
            print(f"COPERNICUS WATER LEVEL: selected cell had no valid values in this time window for {site_label}")
            return None, None, None

        # Auto-compute yearly_mean from the past 30 days when not provided.
        # Each new site then works without a pre-computed constant — the
        # 30-day mean is a stable-enough proxy for the local TOPAZ6 geoid offset.
        if yearly_mean is None:
            hist_start = (now_utc - timedelta(days=30)).replace(tzinfo=None)
            hist_now = now_utc.replace(tzinfo=None)
            hist_time_slice = slice(hist_start, hist_now) if time_ascending else slice(hist_now, hist_start)
            hist_data = ds["zos"].sel(x=best_point[0], y=best_point[1], time=hist_time_slice)
            hist_values = [float(v) for v in hist_data.values.flatten() if not math.isnan(float(v))]
            if hist_values:
                yearly_mean = sum(hist_values) / len(hist_values)
                print(f"COPERNICUS WATER LEVEL: auto-computed mean from {len(hist_values)} historical points: {yearly_mean:.4f}m")
            else:
                print(f"COPERNICUS WATER LEVEL: could not auto-compute mean for {site_label}, chart will show raw values")

        return times_clean, values_clean, yearly_mean

    except Exception as e:
        print("COPERNICUS WATER LEVEL FETCH FAILED:", e)
        return None, None, None


def fetch_gdsps_water_level(lat, lon, now_utc, site_label, yearly_mean=None):
    """
    Fetches the GDSPS (Global Deterministic Storm Surge Prediction System)
    SSH (total water level = tide + surge) 10-day forecast at a point via
    WMS GetFeatureInfo. Samples every 3 hours (~80 requests run in parallel).

    yearly_mean: pre-computed site mean for bias correction. If None,
    the mean of the fetched forecast itself is used so the curve plots
    as an anomaly relative to its own average — comparable with the
    TOPAZ6 curve which is also mean-corrected.

    Returns (times_iso, values_m, yearly_mean_used) or (None, None, None).
    """
    try:
        import concurrent.futures as _cf
        import xml.etree.ElementTree as _ET

        # Step 1: find the latest model run and valid time range from GetCapabilities.
        caps_url = (
            "https://geo.weather.gc.ca/geomet"
            "?service=WMS&version=1.3.0&request=GetCapabilities"
            "&LAYERS=GDSPS_15km_SeaSfcHeight"
        )
        caps_resp = requests.get(caps_url, timeout=20)
        caps_resp.raise_for_status()

        # Parse time dimension via regex — avoids WMS namespace issues with ElementTree.
        # Format: <Dimension name="time" ...>start/end/PT1H</Dimension>
        import re as _re
        m = _re.search(
            r'<Dimension[^>]*name=["\']time["\'][^>]*>([^<]+)</Dimension>',
            caps_resp.text,
        )
        if not m:
            raise ValueError("Could not find time dimension in GDSPS GetCapabilities")
        time_dim = m.group(1).strip()

        parts = time_dim.split("/")
        t_model = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).replace(tzinfo=None)
        t_end   = datetime.fromisoformat(parts[1].replace("Z", "+00:00")).replace(tzinfo=None)
        now_naive = now_utc.replace(tzinfo=None)

        # Walk from the model run start in 3-hour steps, keeping only
        # future times. This ensures all timestamps land on valid hourly
        # grid points (12:00, 15:00, 18:00...) — clamping to now and then
        # stepping would produce off-grid times that GeoMet rejects.
        step_h = 1
        timestamps = []
        t = t_model
        while t <= t_end:
            if t >= now_naive:
                timestamps.append(t)
            t += timedelta(hours=step_h)

        # Step 2: parallel WMS GetFeatureInfo — one request per timestamp.
        bbox = f"{lat - 0.5},{lon - 0.5},{lat + 0.5},{lon + 0.5}"

        def _fetch_one(ts):
            try:
                url = (
                    "https://geo.weather.gc.ca/geomet"
                    "?service=WMS&version=1.3.0&request=GetFeatureInfo"
                    "&layers=GDSPS_15km_SeaSfcHeight"
                    "&query_layers=GDSPS_15km_SeaSfcHeight"
                    f"&bbox={bbox}&width=10&height=10&crs=EPSG:4326&i=5&j=5"
                    "&info_format=application/json"
                    f"&time={ts.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                )
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                features = r.json().get("features", [])
                if features:
                    val = features[0]["properties"].get("value")
                    if val is not None:
                        return ts.strftime("%Y-%m-%dT%H:%M:%SZ"), float(val)
            except Exception:
                pass
            return None

        with _cf.ThreadPoolExecutor(max_workers=20) as pool:
            raw = list(pool.map(_fetch_one, timestamps))

        pairs = [(t, v) for item in raw if item for t, v in [item]]
        if not pairs:
            raise ValueError(f"No valid GDSPS data returned for {site_label}")

        times_out  = [p[0] for p in pairs]
        values_out = [p[1] for p in pairs]

        if yearly_mean is None:
            yearly_mean = sum(values_out) / len(values_out)
            print(f"GDSPS WATER LEVEL: auto-computed mean from {len(values_out)} forecast points: {yearly_mean:.4f}m")

        print(f"GDSPS WATER LEVEL: {len(times_out)} steps fetched for {site_label}")
        return times_out, values_out, yearly_mean

    except Exception as e:
        print(f"GDSPS WATER LEVEL FETCH FAILED for {site_label}:", e)
        return None, None, None


def fetch_gdwps_wave_forecast(lat, lon, now_utc, site_label="site"):
    """
    Fetches the GDWPS (Global Deterministic Wave Prediction System) significant
    wave height, peak period, and mean direction at a point via MSC GeoMet WMS
    GetFeatureInfo. Same approach as fetch_gdsps_water_level.

    GDWPS covers the full Canadian Arctic domain (WAVEWATCH III), unlike the
    Open-Meteo marine API which fails over sea-ice. 3-hour time steps.

    Returns a wave_data dict (same structure as fetch_wave_forecast) or None.
    """
    try:
        import concurrent.futures as _cf

        import re as _re

        # ---- Step 1: discover layer name + get time range from GetCapabilities ----
        # Try the known layer name first; if the server returns ServiceException
        # (layer renamed/unavailable), scan the full capabilities for any GDWPS HTSGW layer.
        htsgw_layer = "GDWPS_10km_HTSGW"
        mtp_layer   = "GDWPS_10km_MTP"

        caps_resp = requests.get(
            "https://geo.weather.gc.ca/geomet"
            "?service=WMS&version=1.3.0&request=GetCapabilities"
            f"&LAYERS={htsgw_layer}",
            timeout=30,
        )
        caps_resp.raise_for_status()

        if "ServiceException" in caps_resp.text or "non disponible" in caps_resp.text.lower():
            print(f"GDWPS: layer {htsgw_layer!r} unavailable — scanning full GetCapabilities")
            full_caps = requests.get(
                "https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0&request=GetCapabilities",
                timeout=60,
            )
            full_caps.raise_for_status()
            name_m = _re.search(r'<Name>([^<]*(?:GDWPS|gdwps)[^<]*(?:HTSGW|htsgw)[^<]*)</Name>', full_caps.text)
            if not name_m:
                raise ValueError("Could not find any GDWPS HTSGW layer in full GetCapabilities")
            htsgw_layer = name_m.group(1).strip()
            mtp_layer   = htsgw_layer.replace("HTSGW", "MTP")
            print(f"GDWPS: discovered layers HTSGW={htsgw_layer!r} MTP={mtp_layer!r}")
            caps_resp = requests.get(
                "https://geo.weather.gc.ca/geomet"
                f"?service=WMS&version=1.3.0&request=GetCapabilities&LAYERS={htsgw_layer}",
                timeout=30,
            )
            caps_resp.raise_for_status()

        m = _re.search(r'<Dimension[^>]*name=["\']time["\'][^>]*>([^<]+)</Dimension>', caps_resp.text)
        if not m:
            m = _re.search(r'<Extent[^>]*name=["\']time["\'][^>]*>([^<]+)</Extent>', caps_resp.text)
        if not m:
            snippet = caps_resp.text[:400]
            print(f"GDWPS GetCapabilities snippet: {snippet}")
            raise ValueError("Could not find time dimension in GDWPS GetCapabilities")
        time_dim = m.group(1).strip()

        parts = time_dim.split("/")
        t_model = datetime.fromisoformat(parts[0].replace("Z", "+00:00")).replace(tzinfo=None)
        t_end   = datetime.fromisoformat(parts[1].replace("Z", "+00:00")).replace(tzinfo=None)
        now_naive = now_utc.replace(tzinfo=None)

        step_h = 3
        timestamps = []
        t = t_model
        while t <= t_end:
            if t >= now_naive:
                timestamps.append(t)
            t += timedelta(hours=step_h)

        # ---- Step 2: parallel GetFeatureInfo for HTSGW (wave height) ----
        bbox = f"{lat - 0.5},{lon - 0.5},{lat + 0.5},{lon + 0.5}"

        def _fetch_htsgw(ts):
            try:
                url = (
                    "https://geo.weather.gc.ca/geomet"
                    "?service=WMS&version=1.3.0&request=GetFeatureInfo"
                    f"&layers={htsgw_layer}&query_layers={htsgw_layer}"
                    f"&bbox={bbox}&width=10&height=10&crs=EPSG:4326&i=5&j=5"
                    "&info_format=application/json"
                    f"&time={ts.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                )
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                features = r.json().get("features", [])
                if features:
                    val = features[0]["properties"].get("value")
                    if val is not None:
                        return ts.strftime("%Y-%m-%dT%H:%M:%SZ"), float(val)
            except Exception:
                pass
            return None

        def _fetch_mtp(ts):
            try:
                url = (
                    "https://geo.weather.gc.ca/geomet"
                    "?service=WMS&version=1.3.0&request=GetFeatureInfo"
                    f"&layers={mtp_layer}&query_layers={mtp_layer}"
                    f"&bbox={bbox}&width=10&height=10&crs=EPSG:4326&i=5&j=5"
                    "&info_format=application/json"
                    f"&time={ts.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                )
                r = requests.get(url, timeout=15)
                r.raise_for_status()
                features = r.json().get("features", [])
                if features:
                    val = features[0]["properties"].get("value")
                    if val is not None:
                        return ts.strftime("%Y-%m-%dT%H:%M:%SZ"), float(val)
            except Exception:
                pass
            return None

        with _cf.ThreadPoolExecutor(max_workers=20) as pool:
            htsgw_raw = list(pool.map(_fetch_htsgw, timestamps))
            mtp_raw   = list(pool.map(_fetch_mtp,   timestamps))

        htsgw_pairs = [(t, v) for item in htsgw_raw if item for t, v in [item]]
        if len(htsgw_pairs) < 72:
            # Too few steps — the discovered layer has a short horizon (e.g. 25km PT1H product).
            # Return None so fetch_wave_forecast falls through to Open-Meteo (10-day forecast).
            raise ValueError(
                f"GDWPS returned only {len(htsgw_pairs)} steps for {site_label} "
                f"(layer {htsgw_layer!r}) — need ≥72 for a useful forecast"
            )

        mtp_dict = {t: v for item in mtp_raw if item for t, v in [item]}

        times_out = [p[0] for p in htsgw_pairs]
        heights   = [p[1] for p in htsgw_pairs]
        periods   = [mtp_dict.get(t) for t in times_out]

        now_str = now_utc.strftime("%Y-%m-%dT%H:00:00Z")
        idx0 = next((i for i, t in enumerate(times_out) if t >= now_str), 0)

        current = {
            "height_m":    heights[idx0],
            "period_s":    periods[idx0],
            "direction_deg": None,
        }

        result = {
            "times_raw":     times_out,
            "heights_m":     heights,
            "period_s":      periods,
            "direction_deg": [None] * len(times_out),
            "current":       current,
            "source":        "GDWPS",
        }
        print(f"GDWPS WAVE: {len(times_out)} steps fetched for {site_label}")
        return result

    except Exception as e:
        print(f"GDWPS WAVE FETCH FAILED for {site_label}:", e)
        return None


def build_water_level_chart(times, values, tz_name, yearly_mean=None,
                             gdsps_times=None, gdsps_values=None, gdsps_yearly_mean=None):
    if not times or not values:
        return None, "Total water level chart unavailable — no data."

    try:
        tz = ZoneInfo(tz_name)
        parsed_times = [datetime.fromisoformat(t.split(".")[0]) for t in times]
        t0 = parsed_times[0]
        hours = [(t - t0).total_seconds() / 3600 for t in parsed_times]

        DEEP_VIOLET  = "#5B21B6"
        PALE_VIOLET  = "#A78BFA"
        NOTION_RED   = "#E16259"
        NOTION_GRAY_LINE = "#9B9A97"
        NOTION_TEXT_GRAY = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        if yearly_mean is not None:
            plot_values = [v - yearly_mean for v in values]
            ylabel = "Water level (m)"
        else:
            plot_values = values
            ylabel = "Total water level (m)"

        has_gdsps = bool(gdsps_times and gdsps_values)
        if has_gdsps:
            gdsps_parsed = [datetime.fromisoformat(t.split(".")[0].rstrip("Z")) for t in gdsps_times]
            gdsps_hours  = [(t - t0).total_seconds() / 3600 for t in gdsps_parsed]
            # GDSPS SSH is already referenced to Mean Water Level by the model,
            # so plot it raw — no additional mean subtraction.
            gdsps_plot   = list(gdsps_values)

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(5.5, 3.2), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        # GDSPS drawn first (behind) as a lighter line, no fill.
        if has_gdsps:
            ax.plot(gdsps_hours, gdsps_plot, linewidth=1.8, color=PALE_VIOLET,
                    zorder=1, label="GDSPS (SSH, above MWL)")
            ax.plot([gdsps_hours[0]], [gdsps_plot[0]], marker="o", markersize=8,
                    color=PALE_VIOLET, markeredgecolor="white", markeredgewidth=1.5, zorder=4)

        # TOPAZ6 on top with fill.
        ax.fill_between(hours, plot_values, min(plot_values), color=DEEP_VIOLET, alpha=0.10, linewidth=0, zorder=2)
        ax.plot(hours, plot_values, linewidth=2.5, color=DEEP_VIOLET, zorder=3,
                label="TOPAZ6 (SSH, vs. yearly mean)")

        # "now" marker on the TOPAZ6 curve with label.
        ax.plot([hours[0]], [plot_values[0]], marker="o", markersize=10,
                color=NOTION_RED, markeredgecolor="white", markeredgewidth=1.5, zorder=5)
        x_offset = max(hours) * 0.025
        ax.annotate("now", xy=(hours[0], plot_values[0]),
                    xytext=(hours[0] + x_offset, plot_values[0]),
                    color=NOTION_RED, fontsize=10, fontweight="bold", ha="left", va="center",
                    bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8))

        if yearly_mean is not None:
            ax.axhline(0, color=NOTION_GRAY_LINE, linewidth=1.2, linestyle="--", zorder=1.5)
            ax.text(0.99, 0.97, f"mean ref ({yearly_mean:.2f}m)",
                    color=NOTION_GRAY_LINE, fontsize=8, va="top", ha="right",
                    transform=ax.transAxes,
                    bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="none", alpha=0.75))

        if has_gdsps:
            legend = ax.legend(fontsize=8, loc="lower right", framealpha=0.8, edgecolor="none")
            for text, color in zip(legend.get_texts(), [PALE_VIOLET, DEEP_VIOLET]):
                text.set_color(color)

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        ax.set_xlim(0, max(hours))

        # Find day boundaries in local time by tracking when the calendar day
        # changes as h increments.  This avoids any dependence on t0 falling at
        # a round minute, and works whether t0 is UTC or local.
        midnight_hours = []
        prev_day = (t0.replace(tzinfo=timezone.utc).astimezone(tz)).day
        for h in range(1, int(max(hours)) + 1):
            local_t = (t0 + timedelta(hours=h)).replace(tzinfo=timezone.utc).astimezone(tz)
            if local_t.day != prev_day:
                midnight_hours.append(h)
                prev_day = local_t.day

        tick_hours = [0] + midnight_hours
        tick_labels = [(t0 + timedelta(hours=h)).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%b %d") for h in tick_hours]
        ax.set_xticks(tick_hours)
        ax.set_xticklabels(tick_labels, fontsize=9, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
        ax.tick_params(axis="y", labelsize=9, colors=NOTION_TEXT_GRAY, length=0)
        ax.tick_params(axis="x", length=8, color="#555555", width=1.2, bottom=True, direction="out")
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
        ax.set_ylabel(ylabel, fontsize=10, color=NOTION_TEXT_GRAY)

        for h in midnight_hours:
            ax.axvline(h, color=NOTION_TEXT_GRAY, linewidth=0.6, alpha=0.18, zorder=0.5)

        fig.tight_layout()
        png_bytes = fig_to_png_bytes(fig, white_bg=True)
        forecast_days = round(max(hours) / 24)
        start_label = (t0.replace(tzinfo=timezone.utc).astimezone(tz)).strftime('%b %d, %H:%M %Z')
        if has_gdsps:
            caption = (
                f"Total water level (tide + storm surge), {forecast_days}-day forecast, "
                f"starting {start_label}. TOPAZ6 shown as anomaly relative to yearly mean "
                f"({yearly_mean:.2f}m); GDSPS plotted as-is (already referenced to Mean Water Level). "
                f"Tidal amplitude and surge signal are comparable; absolute levels are not. "
                f"Sources: TOPAZ6 (Copernicus Marine), GDSPS (MSC/ECCC)."
            )
        else:
            caption = (
                f"Total water level (tide + storm surge), {forecast_days}-day forecast, "
                f"starting {start_label}. Shown as anomaly relative to yearly mean "
                f"({yearly_mean:.2f}m). Source: TOPAZ6 (Copernicus Marine)."
            )
        return png_bytes, caption

    except Exception as e:
        print("WATER LEVEL CHART FAILED:", e)
        return None, "Water level chart could not be generated — see Action logs."


# =========================================================
# MODULE — HYDROMETRIC STATION WATER LEVEL (ECCC Water Survey of Canada)
# Generalized from a Napoiak-Channel-specific module into a reusable
# "any WSC hydrometric station" fetcher — a site can list as many of
# these as it has nearby gauges (e.g. a future Tuktoyaktuk dashboard
# might pull from a different station than Shingle Point's Napoiak
# Channel one), each with its own station_id/provterr/river_name in that
# site's config.py.
#
# Source format confirmed against ECCC's own documentation:
# https://eccc-msc.github.io/open-data/msc-data/obs_hydrometric/readme_hydrometric-datamart_en/
# Column layout: ID,Date,Water Level (m),Grade,Symbol,QA/QC,Discharge (cms),Grade,Symbol,QA/QC
#
# NOTE: not every WSC station measures both products — some report
# Water Level only, others Discharge only, confirmed in practice (a
# station's Discharge column can come back empty for every row). Check
# which this station provides before assuming.
# =========================================================
def fetch_hydrometric_water_level(station_id, provterr):
    """
    Fetches the last ~30 days of daily water level (m) for the given WSC
    hydrometric station from ECCC's public hydrometric CSV datamart.

    Returns (times, values_m) as parallel lists (naive local datetimes,
    UTC-offset suffix stripped since a once-per-day value doesn't need
    timezone math for a 30-day chart), or (None, None) on failure.
    """
    url = (
        f"https://dd.weather.gc.ca/today/hydrometric/csv/{provterr}/daily/"
        f"{provterr}_{station_id}_daily_hydrometric.csv"
    )
    try:
        # dd.weather.gc.ca and weather.gc.ca have both shown transient
        # connection timeouts together on the same run in practice,
        # suggesting shared underlying infrastructure.
        resp = get_with_retry(url, timeout=20, retries=2, backoff_seconds=5)

        import csv as _csv

        lines = resp.text.splitlines()
        print(f"HYDROMETRIC[{station_id}] DEBUG: fetched {len(resp.content)} bytes, {len(lines)} lines total")
        if lines:
            print(f"HYDROMETRIC[{station_id}] DEBUG: header line: {lines[0]!r}")
        for sample_row in lines[1:4]:
            print(f"HYDROMETRIC[{station_id}] DEBUG: sample data row: {sample_row!r}")

        reader = _csv.reader(lines[1:])

        times = []
        values_m = []
        rows_seen = 0
        rows_too_short = 0
        rows_empty_level = 0
        rows_parse_failed = 0
        for row in reader:
            rows_seen += 1
            if len(row) < 3:
                rows_too_short += 1
                continue
            date_str = row[1].strip()
            level_str = row[2].strip()
            if not level_str:
                rows_empty_level += 1
                continue
            try:
                t = datetime.fromisoformat(date_str[:19])
                v = float(level_str)
            except Exception:
                rows_parse_failed += 1
                continue
            times.append(t)
            values_m.append(v)

        print(f"HYDROMETRIC[{station_id}] DEBUG: rows_seen={rows_seen}, rows_too_short={rows_too_short}, "
              f"rows_empty_level={rows_empty_level}, rows_parse_failed={rows_parse_failed}, "
              f"rows_kept={len(values_m)}")

        if not values_m:
            print(f"HYDROMETRIC[{station_id}]: fetch succeeded but no usable water level values found "
                  f"— this station may report Discharge only, not Water Level; see DEBUG lines above")
            return None, None

        print(f"HYDROMETRIC[{station_id}]: parsed {len(values_m)} daily values from {url}")
        return times, values_m

    except Exception as e:
        print(f"HYDROMETRIC[{station_id}] FETCH FAILED:", e)
        return None, None


def build_hydrometric_chart(times, values_m, station_id, river_name, tz_name="America/Inuvik"):
    """
    river_name should describe the specific reach/gauge location, e.g.
    "Mackenzie River, Napoiak Channel above Shallow Bay" — used in the
    chart's caption.
    """
    if not times or not values_m:
        return None, f"{river_name} water level chart unavailable — no data."

    try:
        t0 = times[0]
        hours = [(t - t0).total_seconds() / 3600 for t in times]

        RIVER_TEAL = "#2A9D8F"   # freshwater teal — distinct from ocean blue and wind green
        NOTION_RED = "#E16259"
        NOTION_TEXT_GRAY = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(5.5, 3.2), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        ax.fill_between(hours, values_m, min(values_m), color=RIVER_TEAL, alpha=0.12, linewidth=0, zorder=1)
        ax.plot(hours, values_m, linewidth=2.5, color=RIVER_TEAL, zorder=2)
        ax.plot([hours[-1]], [values_m[-1]], marker="o", markersize=10,
                 color=NOTION_RED, markeredgecolor="white", markeredgewidth=1.5, zorder=3)
        x_offset = max(hours) * 0.025
        ax.annotate("now", xy=(hours[-1], values_m[-1]),
                    xytext=(hours[-1] - x_offset, values_m[-1]),
                    color=NOTION_RED, fontsize=10, fontweight="bold", ha="right", va="center",
                    bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8))

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        ax.set_xlim(0, max(hours))
        tick_hours = list(range(0, int(max(hours)) + 1, 5 * 24))  # every 5 days, to avoid crowding over a 30-day span
        tz = ZoneInfo(tz_name)
        tick_labels = [
            (t0 + timedelta(hours=h)).replace(tzinfo=timezone.utc).astimezone(tz).strftime("%b %d")
            for h in tick_hours
        ]
        ax.set_xticks(tick_hours)
        ax.set_xticklabels(tick_labels, fontsize=13, color=NOTION_TEXT_GRAY, rotation=45, ha="right")
        ax.tick_params(axis="y", labelsize=13, colors=NOTION_TEXT_GRAY, length=0)
        ax.tick_params(axis="x", length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1, zorder=0)
        ax.xaxis.grid(False)
        ax.set_axisbelow(True)
        ax.set_ylabel("Water level (m)", fontsize=13, color=NOTION_TEXT_GRAY)

        fig.tight_layout()
        png_bytes = fig_to_png_bytes(fig, white_bg=True)
        span_days = round(max(hours) / 24)
        end_label = times[-1].replace(tzinfo=timezone.utc).astimezone(tz).strftime("%b %d, %Y %Z")
        caption = (
            f"{river_name}, "
            f"past {span_days} days, ending {end_label}. "
            f"Source: ECCC Water Survey of Canada, station {station_id} (real-time, preliminary/unreviewed)."
        )
        return png_bytes, caption

    except Exception as e:
        print(f"HYDROMETRIC[{station_id}] CHART FAILED:", e)
        return None, f"{river_name} water level chart could not be generated — see Action logs."


# =========================================================
# SNOW DEPTH
# =========================================================
def fetch_snow_depth(lat, lon, past_days=30):
    """
    Fetches hourly snow depth (m) for the past `past_days` days via
    Open-Meteo ERA5-Land. Returns (times, depths_cm) as parallel lists,
    or (None, None) on failure.
    """
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&hourly=snow_depth&past_days={past_days}&forecast_days=1&timezone=UTC"
        )
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        times = data["hourly"]["time"]
        depths_m = data["hourly"]["snow_depth"]
        depths_cm = [d * 100 if d is not None else None for d in depths_m]
        return times, depths_cm
    except Exception as e:
        print("SNOW DEPTH FETCH FAILED:", e)
        return None, None


def build_snow_depth_chart(times, depths_cm, now_utc):
    """
    Renders a compact 30-day snow depth chart styled like the wind
    forecast mini chart — transparent background, fits inside a callout
    as a child block. Returns (png_bytes, caption).
    """
    try:
        # Treat None as 0 (Open-Meteo returns null when no snow on ground)
        depths_cm = [d if d is not None else 0.0 for d in depths_cm]
        if not times or not depths_cm:
            return None, "Snow depth chart unavailable — no data."

        NOTION_BLUE  = "#337EA9"
        NOTION_RED   = "#E16259"
        NOTION_TEXT_GRAY  = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        # Parse actual timestamps — data runs from 30 days ago to now
        import matplotlib.dates as mdates
        ts = [datetime.fromisoformat(t) for t in times]
        vals = depths_cm

        # Find "now" = last timestamp not in the future
        now_dt = datetime.utcnow()
        now_idx = next(
            (i for i in range(len(ts) - 1, -1, -1) if ts[i] <= now_dt),
            len(ts) - 1,
        )

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(4.2, 2.4), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        # Only plot up to "now" — don't show the 1-day forecast tail
        ax.fill_between(ts[:now_idx + 1], vals[:now_idx + 1], 0,
                        color=NOTION_BLUE, alpha=0.15, linewidth=0)
        ax.plot(ts[:now_idx + 1], vals[:now_idx + 1], color=NOTION_BLUE, linewidth=3)

        # "now" dot at the rightmost point
        ax.plot([ts[now_idx]], [vals[now_idx]], marker="o", markersize=10,
                color=NOTION_RED, markeredgecolor="white", markeredgewidth=1.5, zorder=5)
        x_span = (ts[now_idx] - ts[0]).total_seconds()
        x_offset = timedelta(seconds=x_span * 0.03)
        ax.annotate("now", xy=(ts[now_idx], vals[now_idx]),
                    xytext=(ts[now_idx] - x_offset * 4, vals[now_idx]),
                    color=NOTION_RED, fontsize=16, fontweight="bold", ha="right", va="center",
                    bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8))

        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)

        # X-axis: weekly tick marks showing past dates
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
        plt.setp(ax.get_xticklabels(), fontsize=13, color=NOTION_TEXT_GRAY)
        ax.tick_params(axis="y", labelsize=16, colors=NOTION_TEXT_GRAY, length=0)
        ax.tick_params(axis="x", length=0)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1)
        ax.set_axisbelow(True)
        ax.set_ylabel("cm", fontsize=17, color=NOTION_TEXT_GRAY)
        y_max = max(vals[:now_idx + 1]) if max(vals[:now_idx + 1]) > 0 else 10.0
        ax.set_ylim(0, y_max * 1.25)
        ax.set_xlim(ts[0], ts[now_idx])

        fig.tight_layout()
        png_bytes = fig_to_png_bytes(fig)
        caption = "Snow depth (ERA5-Land), past 30 days. Source: Open-Meteo."
        return png_bytes, caption
    except Exception as e:
        print("SNOW DEPTH CHART FAILED:", e)
        return None, "Snow depth chart could not be generated — see Action logs."


def build_snow_depth_card(lat, lon, now_utc):
    """
    Builds the snow depth Today's Conditions card (list of Notion blocks).
    Returns the card blocks (list). Designed to be passed as extra_card to
    build_todays_conditions_section.
    """
    times, depths_cm = fetch_snow_depth(lat, lon, past_days=30)

    current_cm = None
    fetch_ok = times is not None and depths_cm is not None
    if fetch_ok:
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:00")
        idx = next((i for i, t in enumerate(times) if t >= now_str), None)
        if idx is not None:
            current_cm = depths_cm[idx] if depths_cm[idx] is not None else 0.0

    if fetch_ok:
        snow_text = [("Snow depth: ", f"{current_cm:.0f} cm" if current_cm is not None else "0 cm")]
    else:
        snow_text = "Snow depth unavailable — fetch failed."

    chart_bytes, chart_caption = build_snow_depth_chart(times, depths_cm, now_utc) if fetch_ok else (None, "Snow depth chart unavailable.")
    chart_block, _ = _upload_chart_or_caption(chart_bytes, "snow_depth_chart.png", "")

    return [
        heading("❄️ Snow Depth", level=3),
        callout(
            snow_text,
            color="blue_background",
            children=[chart_block] if chart_block else None,
        ),
        gray_caption(chart_caption),
    ]


# =========================================================
# SECTION BUILDERS
# Each function below takes already-fetched data (plus a few small
# display parameters) and returns a list of ready Notion blocks for one
# section/card of the page. A site's dashboard_update.py calls whichever
# of these its config.py lists, in whatever order — this is the layer
# that makes "add or remove a block for one site without touching shared
# code" actually possible: the functions themselves never decide the
# page's overall structure, only how to render their own piece of it.
#
# Each builder uploads its own images to Notion as needed (rather than
# expecting pre-uploaded block objects passed in), so a site's
# entrypoint only needs to gather data and call these — not separately
# manage the upload step for every single chart.
# =========================================================
def _upload_chart_or_caption(chart_bytes, filename, fallback_caption):
    """
    Shared pattern used by nearly every section below: try to upload a
    chart image to Notion, returning (block_or_None, caption_to_show).
    If chart_bytes is None (the underlying fetch/render already failed),
    or the upload itself fails, returns None for the block and an
    appropriate fallback caption instead — so the page always shows
    SOMETHING for that slot rather than a missing/broken block.
    """
    if not chart_bytes:
        return None, fallback_caption
    try:
        uid = upload_image_to_notion(chart_bytes, filename)
        return image_block_from_upload(uid), None
    except Exception as e:
        print(f"{filename} NOTION UPLOAD FAILED:", e)
        return None, "Chart generated but upload to Notion failed — see Action logs."


def build_header_blocks(now_local, logo_url=None, logo_png_bytes=None, institution_text=None, tz_name=None):
    """
    Builds the page's top header: institution logo + attribution line,
    then the "last update" timestamp. logo_png_bytes (already converted
    via fetch_and_convert_logo_to_png) is preferred; falls back to an
    external SVG embed if that conversion failed, and to no logo at all
    if logo_url is also None (e.g. a site with no institutional logo).
    """
    blocks = []
    logo_block = None
    if logo_png_bytes:
        try:
            uid = upload_image_to_notion(logo_png_bytes, "logo.png")
            logo_block = image_block_from_upload(uid)
        except Exception as e:
            print("LOGO NOTION UPLOAD FAILED:", e)

    if logo_block or logo_url:
        logo_column = [logo_block] if logo_block else [external_image_block(logo_url)]
        attribution_column = [paragraph(institution_text)] if institution_text else [paragraph("")]
        blocks.append(columns(logo_column, attribution_column, width_ratios=[0.2, 0.8]))
        blocks.append(divider())
    elif institution_text:
        blocks.append(paragraph(institution_text))
        blocks.append(divider())

    tz_label = tz_name.split("/")[-1].replace("_", " ") if tz_name else "local"
    blocks.append(paragraph(f"Last update: {now_local.strftime('%Y-%m-%d %H:%M %Z')}"))
    blocks.append(paragraph(
        f"All times shown on this page are {tz_label} time "
        f"(automatically adjusts for daylight saving where applicable)."
    ))
    blocks.append(divider())
    return blocks


def build_todays_conditions_section(weather_text, weather_source_text, weather_icon_block,
                                      mini_forecast_strip_block, lat, lon,
                                      wind_now_text, wind_source_text, wind_icon_block,
                                      wind_forecast_chart_block,
                                      tide_text, tide_chart_bytes, tide_chart_caption, station_code,
                                      sun_text, sun_chart_bytes, sun_chart_caption,
                                      extra_card=None):
    """
    Builds the 2x2 "Today's Conditions" card grid (Weather, Wind, Tide,
    Sun) — the most important, fastest-scanning part of the page, with
    only current values, no full charts (those live further down via
    their own section builders). Omit the tide_* arguments (pass None)
    for a site with no nearby IWLS tide station. Pass extra_card (a list
    of Notion blocks) to fill the tide slot with a custom card instead.
    """
    blocks = [heading("📍 Today's Conditions")]

    weather_card = [
        heading("Weather", level=3),
        callout(
            weather_text,
            color="blue_background",
            children=[b for b in [weather_icon_block, mini_forecast_strip_block] if b] or None,
        ),
        link_paragraph(
            "Full weather data →",
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,pressure_msl&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=auto",
            prefix=f"{weather_source_text}  ", prefix_gray=True,
        ),
    ]

    wind_card = [
        heading("🧭 Wind", level=3),
        callout(
            wind_now_text,
            color="blue_background",
            children=[b for b in [wind_icon_block, wind_forecast_chart_block] if b] or None,
        ),
        link_paragraph(
            "Full wind data →",
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=windspeed_10m,winddirection_10m&timezone=auto",
            prefix=f"{wind_source_text}  ", prefix_gray=True,
        ),
    ]

    sun_chart_block, _ = _upload_chart_or_caption(sun_chart_bytes, "sun_chart.png", "")
    sun_card = [
        heading("☀️ Sun", level=3),
        callout(
            sun_text,
            color="blue_background",
            children=[sun_chart_block] if sun_chart_block else None,
        ),
        link_paragraph(
            "Full sun data →",
            f"https://api.sunrise-sunset.org/json?lat={lat}&lng={lon}&formatted=0",
            prefix=f"{sun_chart_caption if sun_chart_bytes else 'Sun position chart could not be generated — see Action logs.'}  ", prefix_gray=True,
        ),
    ]

    if tide_text is not None:
        tide_chart_block, _ = _upload_chart_or_caption(tide_chart_bytes, "tide_chart.png", "")
        tide_card = [
            heading("🌊 Tide", level=3),
            callout(
                tide_text,
                color="blue_background",
                children=[tide_chart_block] if tide_chart_block else None,
            ),
            link_paragraph(
                "Full station data →",
                f"https://www.tides.gc.ca/en/stations/{station_code}",
                prefix=f"{tide_chart_caption if tide_chart_bytes else 'Tide chart could not be generated — see Action logs.'}  ",
                prefix_gray=True,
            ),
        ]
        blocks.append(columns(weather_card, wind_card))
        blocks.append(columns(tide_card, sun_card))
    elif extra_card is not None:
        blocks.append(columns(weather_card, wind_card))
        blocks.append(columns(extra_card, sun_card))
    else:
        blocks.append(columns(weather_card, wind_card))
        blocks += sun_card

    blocks.append(divider())
    return blocks


def build_active_alerts_section(active_alerts):
    """
    Returns [] (nothing) if active_alerts is empty — this section
    genuinely disappears from the page entirely when nothing is active,
    rather than showing an empty placeholder.
    """
    if not active_alerts:
        return []

    alert_lines = []
    for a in active_alerts[:5]:
        if a["summary"] and a["summary"] != a["title"]:
            alert_lines.append([("", a["title"]), ": ", a["summary"]])
        else:
            alert_lines.append(a["title"])
    alert_lines.append("Source: Environment Canada")

    blocks = [
        heading("⚠️ Active Weather Alerts"),
        callout(alert_lines, color="yellow_background"),
    ]
    if active_alerts[0].get("link"):
        blocks.append(link_paragraph("See full alert details →", active_alerts[0]["link"]))
    else:
        blocks.append(paragraph(""))
    blocks.append(divider())
    return blocks


def build_land_forecast_section(large_forecast_strip_bytes, land_forecast_caption):
    block, fallback = _upload_chart_or_caption(large_forecast_strip_bytes, "large_forecast_strip.png", None)
    blocks = [
        heading("📅 Weather Forecast — next 5 days", level=3),
        callout("5-day outlook:", color="purple_background", children=[block] if block else None),
        gray_caption(fallback or land_forecast_caption),
        divider(),
    ]
    return blocks


def build_marine_forecast_section(marine_text, marine_source_text, zone_name, zone_id):
    return [
        heading(f"⚓ Marine Forecast — {zone_name}", level=3),
        callout(marine_text, color="purple_background"),
        link_paragraph(
            "Explore here →", f"https://weather.gc.ca/marine/forecast_e.html?siteID={zone_id}",
            prefix=f"{marine_source_text}  ", prefix_gray=True,
        ),
        divider(),
    ]


def fetch_wave_forecast(lat, lon, now_utc, site_label="site"):
    """
    Fetches 10-day significant wave height forecast. Tries ECCC GDWPS via
    MSC GeoMet first (covers Canadian Arctic / ice-covered waters), then
    falls back to Open-Meteo Marine API (fails in ice-covered areas).
    Returns a wave_data dict or None on complete failure.
    """
    # Primary: ECCC GDWPS (Canadian Arctic wave model, GeoMet WMS)
    result = fetch_gdwps_wave_forecast(lat, lon, now_utc, site_label)
    if result:
        return result

    # Fallback: Open-Meteo marine API (fails over sea-ice but useful for open water)
    print("WAVE FORECAST: GDWPS failed, falling back to Open-Meteo marine API")
    try:
        url = (
            "https://marine-api.open-meteo.com/v1/marine"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=wave_height,wave_direction,wave_period"
            "&forecast_days=10&timezone=UTC"
        )
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()

        times_raw  = data["hourly"]["time"]
        heights    = data["hourly"]["wave_height"]
        periods    = data["hourly"]["wave_period"]
        directions = data["hourly"]["wave_direction"]

        def _clean(v):
            """Replace Open-Meteo fill values (9999, None) with None."""
            if v is None:
                return None
            try:
                f = float(v)
                return None if f > 500 else f
            except (TypeError, ValueError):
                return None

        heights    = [_clean(v) for v in heights]
        periods    = [_clean(v) for v in periods]
        directions = [_clean(v) for v in directions]

        now_str = now_utc.strftime("%Y-%m-%dT%H:00")
        idx = next((i for i, t in enumerate(times_raw) if t >= now_str), 0)

        current = {
            "height_m":    heights[idx],
            "period_s":    periods[idx],
            "direction_deg": directions[idx],
        }

        return {
            "times_raw":     times_raw[idx:],
            "heights_m":     heights[idx:],
            "period_s":      periods[idx:],
            "direction_deg": directions[idx:],
            "current":       current,
            "source":        "Open-Meteo",
        }
    except Exception as e:
        print("WAVE FORECAST FETCH FAILED (both sources):", e)
        return None


def build_wave_forecast_chart(wave_data, tz_name, now_utc):
    """
    Renders a 10-day wave height forecast chart. Returns (png_bytes, caption).
    """
    try:
        times_raw = list(zip(wave_data["times_raw"], wave_data["heights_m"]))
        # Trim trailing None entries — forecast tail often has no data and
        # would otherwise plot as a false zero, creating a cliff in the chart.
        while times_raw and times_raw[-1][1] is None:
            times_raw.pop()
        if not times_raw:
            return None, "Wave forecast chart unavailable — no valid wave model data at this location (site may be outside the wave model grid)."
        tz = ZoneInfo(tz_name)
        times   = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc).astimezone(tz) for t, _ in times_raw]
        heights = [h for _, h in times_raw]

        NOTION_BLUE       = "#337EA9"
        NOTION_RED        = "#E16259"
        NOTION_TEXT_GRAY  = "#787774"
        NOTION_LIGHT_GRID = "#EDECEC"

        plt.rcParams["font.family"] = "DejaVu Sans"
        fig, ax = plt.subplots(figsize=(8, 3.0), dpi=150)
        fig.patch.set_alpha(0)
        ax.set_facecolor("none")

        ax.fill_between(times, heights, 0, color=NOTION_BLUE, alpha=0.15, linewidth=0)
        ax.plot(times, heights, color=NOTION_BLUE, linewidth=2.5)

        # Rough sea thresholds
        ax.axhline(1.5, color="#E8A838", linewidth=1, linestyle="--", alpha=0.6, zorder=1)
        ax.axhline(2.5, color="#E16259", linewidth=1, linestyle="--", alpha=0.6, zorder=1)
        ax.text(times[-1], 1.55, "rough", color="#E8A838", fontsize=9, ha="right", va="bottom")
        ax.text(times[-1], 2.55, "very rough", color="#E16259", fontsize=9, ha="right", va="bottom")

        # "now" marker — data starts from current hour so index 0 is now
        ax.plot([times[0]], [heights[0]], marker="o", markersize=10,
                color=NOTION_RED, markeredgecolor="white", markeredgewidth=1.5, zorder=5)
        valid_heights = [h for h in heights if h is not None]
        y_range = max(valid_heights) if valid_heights and max(valid_heights) > 0 else 1.0
        ax.annotate("now", xy=(times[0], heights[0]),
                    xytext=(times[0], heights[0] + y_range * 0.08 + 0.05),
                    color=NOTION_RED, fontsize=11, fontweight="bold", ha="left", va="bottom",
                    bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8))

        import matplotlib.dates as mdates
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=tz))
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=2, tz=tz))
        ax.tick_params(axis="x", labelsize=13, colors=NOTION_TEXT_GRAY, length=0, rotation=45)
        ax.tick_params(axis="y", labelsize=13, colors=NOTION_TEXT_GRAY, length=0)
        ax.set_ylabel("Wave height (m)", fontsize=13, color=NOTION_TEXT_GRAY)
        ax.yaxis.grid(True, color=NOTION_LIGHT_GRID, linewidth=1)
        ax.set_axisbelow(True)
        for spine in ["top", "right", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color(NOTION_LIGHT_GRID)
        ax.set_ylim(bottom=0)
        fig.tight_layout()

        png_bytes = fig_to_png_bytes(fig, white_bg=True)
        end_label = times[-1].strftime("%b %d, %Y %Z")
        source = wave_data.get("source", "GDWPS")
        if source == "GDWPS":
            source_text = "Environment Canada GDWPS (WAVEWATCH III, 10 km) via MSC GeoMet"
        else:
            source_text = "Open-Meteo Marine API (GFS Wave / ERA5-Ocean)"
        caption = (
            f"Significant wave height, forecast ending {end_label}. "
            f"Source: {source_text}."
        )
        location_note = wave_data.get("location_note")
        if location_note:
            caption += f" Note: {location_note}"
        return png_bytes, caption
    except Exception as e:
        print("WAVE FORECAST CHART FAILED:", e)
        return None, "Wave forecast chart could not be generated — see Action logs."


def build_wave_forecast_section(wave_data):
    """
    Builds the wave height forecast section. Insert between marine forecast
    and total water level sections in dashboard_update.py.
    """
    if not wave_data:
        return [
            heading("🌊 Wave Height — 10-Day Forecast", level=2),
            callout("Wave forecast unavailable — fetch failed. Check Action logs.", color="gray_background"),
            divider(),
        ]

    current = wave_data.get("current", {})
    h = current.get("height_m")
    p = current.get("period_s")
    d = current.get("direction_deg")

    compass = degrees_to_compass(d) if d is not None else None
    dir_text = f"{compass} ({d:.0f}°)" if compass else "—"

    current_text = [
        ("Current wave height: ", f"{h:.1f} m" if h is not None else "—"),
        ("Wave direction: ", dir_text),
        ("Wave period: ", f"{p:.0f} s" if p is not None else "—"),
    ]

    tz_name = "UTC"
    chart_bytes, chart_caption = build_wave_forecast_chart(wave_data, tz_name, datetime.utcnow())
    chart_block, fallback = _upload_chart_or_caption(chart_bytes, "wave_forecast.png",
                                                      "Wave forecast chart could not be generated — see Action logs.")
    blocks = [
        heading("🌊 Wave Height — 10-Day Forecast", level=2),
        callout(current_text, color="blue_background"),
    ]
    if chart_block:
        blocks.append(chart_block)
    blocks.append(gray_caption(fallback or chart_caption))
    blocks.append(divider())
    return blocks


def build_total_water_level_section(water_level_text, water_level_chart_bytes, water_level_chart_caption):
    block, fallback = _upload_chart_or_caption(water_level_chart_bytes, "water_level_chart.png",
                                                 "Water level chart could not be generated — see Action logs.")
    blocks = [
        heading("🌊 Total Water Level — 10-Day Forecast (tide + storm surge)"),
        callout(water_level_text, color="purple_background"),
    ]
    if block:
        blocks.append(block)
    blocks.append(gray_caption(fallback or water_level_chart_caption))
    blocks.append(divider())
    return blocks


def build_hydrometric_section(chart_bytes, chart_caption, heading_text):
    """
    heading_text: e.g. "ðŸ’§ Napoiak Channel Water Level — Mackenzie River
    above Shallow Bay" — written per-site in config.py, since it should
    name the specific gauge/reach, which varies per station.
    """
    block, fallback = _upload_chart_or_caption(chart_bytes, "hydrometric_chart.png",
                                                 "Water level chart could not be generated — see Action logs.")
    blocks = [heading(heading_text)]
    if block:
        blocks.append(block)
    blocks.append(paragraph(fallback or chart_caption))
    blocks.append(divider())
    return blocks


def build_modis_section(modis_block, modis_caption, modis_date, now_utc, bbox_3413, site_display_name):
    worldview_date = modis_date if modis_date else now_utc.strftime("%Y-%m-%d")
    worldview_url = (
        f"https://worldview.earthdata.nasa.gov/?p=arctic"
        f"&l=MODIS_Terra_CorrectedReflectance_TrueColor,Coastlines"
        f"&t={worldview_date}"
        f"&v={bbox_3413}"
    )
    blocks = [heading(f"🛰️ Satellite View of {site_display_name}")]
    if modis_block:
        blocks.append(modis_block)
    blocks.append(paragraph(f"A real satellite photo of {site_display_name}, taken on {modis_date if modis_date else 'a recent date'}."))
    blocks.append(link_paragraph("Explore here →", worldview_url, prefix=f"{modis_caption}  ", prefix_gray=True))
    blocks.append(divider())
    return blocks


def build_sentinel1_section(sentinel1_bytes, sentinel1_caption, sentinel1_explore_url, site_display_name):
    block, upload_fallback = _upload_chart_or_caption(sentinel1_bytes, "sentinel1.png", None)
    blocks = [heading(f"🛰️ Radar View of {site_display_name}")]
    if block:
        blocks.append(block)
    blocks.append(paragraph(
        f"A radar image of {site_display_name}. Sentinel-1 SAR sees through cloud and darkness, "
        f"making it useful when the optical satellite photo above is obscured by weather."
    ))
    caption_text = upload_fallback or sentinel1_caption
    if sentinel1_explore_url:
        blocks.append(link_paragraph("Explore here →", sentinel1_explore_url, prefix=f"{caption_text}  ", prefix_gray=True))
    else:
        blocks.append(gray_caption(caption_text))
    blocks.append(divider())
    return blocks


def build_temperature_chart_section(temp_chart_bytes, temp_chart_caption):
    block, fallback = _upload_chart_or_caption(temp_chart_bytes, "temp_chart.png", "Chart could not be generated — see Action logs.")
    blocks = [heading("📈 Temperature — last 30 days vs. historical average")]
    if block:
        blocks.append(block)
    blocks.append(paragraph(fallback or temp_chart_caption))
    blocks.append(divider())
    return blocks


def build_tdd_histogram_section(tdd_histogram_bytes, tdd_histogram_caption):
    block, fallback = _upload_chart_or_caption(tdd_histogram_bytes, "tdd_histogram.png", "Thawing degree days chart could not be generated — see Action logs.")
    blocks = [heading("🌡️ Thawing Degree Days — annual totals")]
    if block:
        blocks.append(block)
    blocks.append(paragraph(fallback or tdd_histogram_caption))
    blocks.append(divider())
    return blocks


def build_wind_chart_section(wind_chart_bytes, wind_chart_caption, rose_bytes=None):
    blocks = [heading("🧭 Wind — last 30 days")]
    # rose_bytes is intentionally ignored — the combined figure already
    # contains both the rose and the vector chart in a single image.
    block, fallback = _upload_chart_or_caption(wind_chart_bytes, "wind_chart.png", "Wind chart could not be generated — see Action logs.")
    if block:
        blocks.append(block)
    blocks.append(paragraph(fallback or wind_chart_caption))
    blocks.append(divider())
    return blocks


DISCLAIMER_SOURCES = {
    "gem":         "Environment and Climate Change Canada (ECCC) GEM/GDPS numerical weather prediction",
    "open_meteo":  "Open-Meteo (ERA5 reanalysis, ECMWF)",
    "modis":       "NASA MODIS Terra (true-colour satellite imagery)",
    "sentinel1":   "ESA Sentinel-1 SAR (Copernicus Sentinel-1 via Sentinel Hub)",
    "cmems":       "Copernicus Marine Service (CMEMS / TOPAZ6 total water level)",
    "waves":       "Open-Meteo Marine API (GFS Wave / ERA5-Ocean wave forecast)",
    "tides":       "Fisheries and Oceans Canada (DFO/IWLS tidal predictions)",
    "marine":      "Environment and Climate Change Canada (marine weather forecasts)",
    "alerts":      "Environment and Climate Change Canada (public weather alerts)",
    "hydrometric": "Water Survey of Canada (ECCC hydrometric gauges)",
    "snow":        "Open-Meteo (ERA5-Land snow depth reanalysis)",
    "wildfire":    "Canadian Wildland Fire Information System (CWFIS / Natural Resources Canada, satellite hotspots)",
}

DISCLAIMER_FOOTER = (
    "We hold no responsibility for the accuracy, completeness, or timeliness of this data, and this page "
    "is not a substitute for official sources. Do not use this information for navigation, "
    "safety-critical decisions, or any other purpose where inaccurate or delayed data could cause harm."
)


def build_disclaimer_section(sources):
    """
    sources: list of keys from DISCLAIMER_SOURCES (e.g. ["gem", "open_meteo", "modis", "sentinel1"]).
    Only the listed sources appear in the disclaimer — keeps it accurate per site.
    """
    source_lines = "; ".join(
        DISCLAIMER_SOURCES[k] for k in sources if k in DISCLAIMER_SOURCES
    )
    text = (
        "Disclaimer: All data and imagery on this page are collated from external third-party sources "
        f"and are displayed here for general informational purposes only. Data sources include: {source_lines}. "
        + DISCLAIMER_FOOTER
    )
    return [disclaimer_paragraph(text)]


def build_gem_forecast_section(gem_forecast, tz_name, now_utc=None):
    """
    Assembles all GEM-based forecast Notion blocks:
      - Day icon strip (10 days)
      - Temperature, wind speed, pressure, precipitation charts
    Returns a list of Notion blocks; empty on failure.
    """
    if not gem_forecast:
        return [paragraph("GEM/GDPS forecast unavailable.")]

    daily  = gem_forecast.get("daily",  {})
    hourly = gem_forecast.get("hourly", {})
    source = gem_forecast.get("source", "GEM")
    source_label = "GDPS (GEM-seamless) via Open-Meteo" if source == "gem_seamless" else "Open-Meteo (ECMWF fallback)"

    blocks = [heading("Weather forecast — next 10 days", level=2)]

    strip_bytes = build_gem_day_strip(daily, tz_name)
    img_block, caption = _upload_chart_or_caption(
        strip_bytes, "gem_day_strip.png", "10-day forecast strip could not be rendered."
    )
    blocks.append(img_block if img_block else paragraph(caption))
    blocks.append(gray_caption(f"Source: {source_label}"))

    blocks.append(divider())

    # build_gem_forecast_charts returns (temp, wind, press, precip)
    temp_b, wind_b, press_b, precip_b = build_gem_forecast_charts(hourly, tz_name, now_utc=now_utc)
    for chart_b, fname, caption_text in [
        (temp_b,   "gem_temp.png",   "GEM temperature forecast unavailable."),
        (wind_b,   "gem_wind.png",   "GEM wind forecast unavailable."),
        (press_b,  "gem_press.png",  "GEM pressure forecast unavailable."),
        (precip_b, "gem_precip.png", "GEM precipitation forecast unavailable."),
    ]:
        blk, cap = _upload_chart_or_caption(chart_b, fname, caption_text)
        blocks.append(blk if blk else paragraph(cap))

    return blocks


# =========================================================
# MODULE — LAKE/RIVER ICE (Sentinel-1, inland water bodies)
# Mirrors the sea-ice module but uses OSM water body polygons
# (lakes, rivers) instead of a coastline to define the water mask.
# The same HH-only 3-category classifier is used — the physics of
# backscatter from ice vs. open water is identical for fresh-water ice.
# =========================================================

def _make_water_mask(water_bodies_geojson_path, center_x, center_y, utm_zone, half_width_m, output_size_px):
    """
    Rasterises OSM water-body polygon features (natural=water, waterway=riverbank)
    as a binary mask. Polygon interiors are filled directly — no flood-fill needed.
    Returns True=water, False=land. Falls back to all-False on any error.
    """
    import numpy as np
    from PIL import Image as _PI, ImageDraw as _PID

    w = h = output_size_px
    mask = _PI.new("L", (w, h), 0)

    if not water_bodies_geojson_path:
        return np.zeros((h, w), dtype=bool)

    try:
        def _to_px(lon_val, lat_val):
            x, y = latlon_to_utm(lat_val, lon_val, zone=utm_zone)
            px = int((x - (center_x - half_width_m)) / (2 * half_width_m) * w)
            py = int(h - (y - (center_y - half_width_m)) / (2 * half_width_m) * h)
            return (px, py)

        with open(water_bodies_geojson_path) as f:
            geojson = json.load(f)

        draw = _PID.Draw(mask)
        for feature in geojson.get("features", []):
            geom = feature.get("geometry", {})
            gt = geom.get("type")
            coords = geom.get("coordinates", [])
            rings = []
            if gt == "Polygon":
                rings = coords  # list of rings; first is exterior, rest are holes
            elif gt == "MultiPolygon":
                for poly in coords:
                    rings.extend(poly)
            for ring in rings:
                pts = [_to_px(c[0], c[1]) for c in ring]
                if len(pts) >= 3:
                    draw.polygon(pts, fill=128)

    except Exception as e:
        print(f"WATER MASK RASTERISE FAILED: {e}")
        return np.zeros((h, w), dtype=bool)

    return np.array(mask) == 128


def fetch_and_process_sentinel1_lake_ice(lat, lon, site_label, utm_zone, utm_epsg,
                                          center_x, center_y, points, tz_name,
                                          half_width_m=50_000, reference_lines=None,
                                          water_bodies_geojson_path=None, now_utc=None):
    """
    Fetches a zoomed Sentinel-1 lake/river ice classification image.
    Uses a smaller half_width_m (default 50 km) for a closer view of local
    water bodies, and a water-body polygon mask instead of a coastline.
    Returns (ice_bytes, ice_caption).
    """
    import functools
    from concurrent.futures import ThreadPoolExecutor as _TPEX

    sh_token = get_sentinel_hub_token()
    if not sh_token:
        return None, "Lake ice classification unavailable — Sentinel Hub credentials missing."

    s1_date, s1_full_datetime, acq_mode, band, pol_filter = find_latest_sentinel1_date(
        sh_token, lat, lon, site_label, now_utc=now_utc
    )
    if not s1_date:
        return None, "Lake ice classification unavailable — no recent Sentinel-1 scene found."

    if band != "HH":
        return None, (
            "Lake ice classification requires HH polarisation (EW mode). "
            f"Latest scene uses {band} — classification not available for this acquisition."
        )

    # Compute a zoomed UTM center — same lat/lon, smaller frame
    with _TPEX(max_workers=2) as _ex:
        _color_f = _ex.submit(
            fetch_sentinel1_ice_image,
            sh_token, s1_date, center_x, center_y, utm_epsg, half_width_m,
            acq_mode=acq_mode, band=band, pol_filter=pol_filter,
        )
        _gray_f = _ex.submit(
            fetch_sentinel1_image,
            sh_token, s1_date, center_x, center_y, utm_epsg, half_width_m,
            acq_mode=acq_mode, band=band, pol_filter=pol_filter,
        )
        raw_color = _color_f.result()
        raw_gray  = _gray_f.result()

    if not raw_color:
        return None, "Lake ice classification unavailable — image fetch failed."

    water_mask = _make_water_mask(
        water_bodies_geojson_path, center_x, center_y, utm_zone, half_width_m,
        MODIS_FINAL_SIZE_PX,
    )
    if raw_gray is not None:
        raw_color = _composite_sea_color_land_gray(raw_color, raw_gray, water_mask)

    project_fn = functools.partial(latlon_to_utm, zone=utm_zone)
    ice_bytes = annotate_plain_image(
        raw_color, points=points, center_x=center_x, center_y=center_y,
        project_fn=project_fn, lat=lat, lon=lon, half_width_m=half_width_m,
        scale_km=_nice_scale_km(half_width_m),
        reference_lines=reference_lines,
        water_bodies_geojson_path=water_bodies_geojson_path,
    )
    ice_bytes = add_ice_classification_legend(ice_bytes, ice_label="Ice")

    try:
        s1_dt_utc = datetime.strptime(s1_full_datetime[:19], "%Y-%m-%dT%H:%M:%S")
        s1_local = to_local_time(s1_dt_utc, tz_name)
        ice_bytes = stamp_timestamp(ice_bytes, s1_local, label="Acquired")
        tz_abbr = s1_local.strftime("%Z")
        s1_local_str = s1_local.strftime(f"%b %d, %Y, %H:%M {tz_abbr}")
    except Exception as e:
        print("LAKE ICE TIMESTAMP STAMP FAILED:", e)
        s1_local_str = s1_date

    ice_caption = (
        f"Sentinel-1 HH lake/river ice estimate ({acq_mode} mode), acquired {s1_local_str}. "
        f"Water bodies coloured using HH σ° threshold: open water (dark navy, < −21 dB), "
        f"marginal/thin ice (cyan-blue, −21 to −14 dB), ice (cyan-white, > −14 dB). "
        f"Land shown in standard SAR grayscale. Water body outlines from OpenStreetMap. "
        f"Source: Copernicus Sentinel-1 via Sentinel Hub."
    )

    return ice_bytes, ice_caption


def build_lake_ice_section(ice_bytes, ice_caption, site_label):
    """Builds Notion blocks for the lake/river ice classification zoom image."""
    title = "🧊 Lake and River Ice — Sentinel-1 Classification — Zoom"
    heading_block = heading(title)
    if ice_bytes:
        try:
            uid = upload_image_to_notion(ice_bytes, "lake_ice.png")
            img_block = image_block_from_upload(uid)
        except Exception as e:
            print("LAKE ICE NOTION UPLOAD FAILED:", e)
            img_block = paragraph(f"Lake ice image could not be uploaded: {e}")
    else:
        img_block = paragraph(
            f"Lake/river ice classification unavailable for {site_label}. "
            "This section requires a recent Sentinel-1 EW/HH acquisition."
        )
    return [heading_block, img_block, gray_caption(ice_caption), divider()]


# =========================================================
# MODULE — WILDFIRE (Canadian Wildland Fire Information System)
# Uses CWFIS GeoServer WFS public endpoint — no authentication needed.
# Fetches satellite fire hotspots within a bounding box and renders
# them as a colour-coded map based on Fire Weather Index (FWI).
# =========================================================

def fetch_cwfis_wildfires(lat, lon, radius_km=600, now_utc=None):
    """
    Fetches satellite fire hotspots from the CWFIS GeoServer WFS endpoint
    (public:hotspots layer) within radius_km of the given lat/lon,
    filtered to the past 7 days.

    Returns a list of dicts with keys: lat, lon, fwi, frp, rep_date, agency.
    Returns empty list on failure or no data.
    """
    if now_utc is None:
        now_utc = datetime.utcnow()

    # Compute bbox in degrees (approximate)
    km_per_deg_lat = 111.1
    km_per_deg_lon = 111.1 * math.cos(math.radians(lat))
    dlat = radius_km / km_per_deg_lat
    dlon = radius_km / km_per_deg_lon
    lat_min = lat - dlat
    lat_max = lat + dlat
    lon_min = lon - dlon
    lon_max = lon + dlon

    cutoff = (now_utc - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        url = (
            "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wfs"
            "?service=WFS&version=2.0.0&request=GetFeature"
            "&typeNames=public:hotspots"
            "&outputFormat=application/json"
            f"&bbox={lon_min:.4f},{lat_min:.4f},{lon_max:.4f},{lat_max:.4f},EPSG:4326"
            f"&CQL_FILTER=rep_date>='{cutoff}'"
            "&count=5000"
        )
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
        fires = []
        for f in data.get("features", []):
            props = f.get("properties", {})
            geom = f.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            c = geom.get("coordinates", [None, None])
            fires.append({
                "lon": c[0],
                "lat": c[1],
                "fwi":      props.get("fwi"),
                "frp":      props.get("frp"),
                "rep_date": props.get("rep_date"),
                "agency":   props.get("agency", ""),
            })
        print(f"CWFIS WILDFIRES: {len(fires)} hotspots in {radius_km} km radius (last 7 days)")
        return fires
    except Exception as e:
        print(f"CWFIS WILDFIRE FETCH FAILED: {e}")
        return []


def _fetch_blue_marble(bbox_3413):
    """
    Fetches the Blue Marble Next Generation August composite from NASA GIBS
    (EPSG:3413) at the same resolution as the MODIS fetch so it can be
    passed through rotate_to_north_up() and used as a basemap.
    Returns PNG bytes (converted from JPEG).
    """
    from PIL import Image
    import io as _io
    params = {
        "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.1.1",
        "LAYERS": "BlueMarble_NextGeneration",
        "STYLES": "", "FORMAT": "image/jpeg", "TRANSPARENT": "false",
        "WIDTH": str(MODIS_FETCH_SIZE_PX), "HEIGHT": str(MODIS_FETCH_SIZE_PX),
        "SRS": "EPSG:3413", "BBOX": bbox_3413,
        "TIME": "2023-08-01",
    }
    url = "https://gibs.earthdata.nasa.gov/wms/epsg3413/best/wms.cgi?" + "&".join(
        f"{k}={v}" for k, v in params.items()
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    img = Image.open(_io.BytesIO(resp.content)).convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_wildfire_map(fires, site_lat, site_lon, now_utc, tz_name,
                       bbox_3413=None, center_x=None, center_y=None,
                       rotation_deg=0.0, half_width_m=150_000):
    """
    Renders a satellite-basemap fire-hotspot map coloured by FWI.

    When bbox_3413/center_x/center_y are supplied (all Canadian sites),
    fetches a cloud-free Blue Marble basemap from NASA GIBS, rotates it to
    north-up at the same extent as the MODIS image, and overlays the fire
    dots using the same EPSG:3413→pixel projection used by the MODIS and
    Sentinel-1 annotation pipeline.

    Returns (png_bytes, caption_str) or (None, error_str).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io as _io

        # FWI colour ramp matching CWFIS — RGB tuples for PIL
        def _fwi_color(fwi):
            if fwi is None or fwi < 5:  return (76,  175, 80)   # green
            if fwi < 12:                 return (255, 193,  7)   # yellow
            if fwi < 20:                 return (230, 100, 20)   # orange
            if fwi < 30:                 return (198,  40, 40)   # red
            return                              (74,    0, 20)   # dark maroon

        # --- Basemap ---
        basemap_bytes = None
        if bbox_3413 and center_x is not None and center_y is not None:
            try:
                basemap_bytes = _fetch_blue_marble(bbox_3413)
                basemap_bytes = rotate_to_north_up(basemap_bytes, rotation_deg)
            except Exception as e:
                print(f"WILDFIRE BASEMAP FETCH FAILED: {e}")

        if basemap_bytes:
            img = Image.open(_io.BytesIO(basemap_bytes)).convert("RGB")
        else:
            img = Image.new("RGB", (MODIS_FINAL_SIZE_PX, MODIS_FINAL_SIZE_PX), (240, 237, 232))

        draw = ImageDraw.Draw(img)
        width_px, height_px = img.size
        meters_per_px = (half_width_m * 2) / MODIS_FINAL_SIZE_PX
        rotation_rad = math.radians(rotation_deg)

        try:
            font    = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
        except Exception:
            font = font_sm = ImageFont.load_default()

        def _project_3413(x_m, y_m):
            dx_m, dy_m = x_m - center_x, y_m - center_y
            cos_r, sin_r = math.cos(rotation_rad), math.sin(rotation_rad)
            dx_rot = dx_m * cos_r - dy_m * sin_r
            dy_rot = dx_m * sin_r + dy_m * cos_r
            return width_px / 2 + dx_rot / meters_per_px, height_px / 2 - dy_rot / meters_per_px

        def _project_latlon(lat, lon):
            return _project_3413(*latlon_to_3413(lat, lon))

        # Distance rings at 200, 400, 600 km (dashed, white, labelled at top)
        for ring_km in [200, 400, 600]:
            ring_pts = []
            for a in range(0, 361, 3):
                ar = math.radians(a)
                rx = center_x + ring_km * 1000 * math.sin(ar)
                ry = center_y + ring_km * 1000 * math.cos(ar)
                ring_pts.append(_project_3413(rx, ry))
            for i in range(0, len(ring_pts) - 1, 2):
                draw.line([ring_pts[i], ring_pts[i + 1]], fill=(220, 220, 220), width=1)
            # Label at geographic north of ring (angle 0 = north in EPSG:3413)
            lx, ly = _project_3413(center_x, center_y + ring_km * 1000)
            draw.text((lx + 4, ly - 20), f"{ring_km} km", fill=(220, 220, 220), font=font_sm)

        # Fire dots — sorted low→high FWI so hottest renders on top
        sorted_fires = sorted(fires, key=lambda f: (f["fwi"] or 0))
        for fire in sorted_fires:
            if fire["lat"] is None or fire["lon"] is None:
                continue
            px, py = _project_latlon(fire["lat"], fire["lon"])
            fwi = fire["fwi"] or 0
            color = _fwi_color(fwi)
            r = max(5, min(4 + int(fwi / 4), 12))
            draw.ellipse([px - r - 2, py - r - 2, px + r + 2, py + r + 2], fill=(255, 255, 255))
            draw.ellipse([px - r,     py - r,     px + r,     py + r    ], fill=color)

        # Site marker: blue circle with white ring and white centre dot
        sx, sy = _project_latlon(site_lat, site_lon)
        draw.ellipse([sx - 10, sy - 10, sx + 10, sy + 10], fill=(255, 255, 255))
        draw.ellipse([sx -  8, sy -  8, sx +  8, sy +  8], fill=(32, 96, 192))
        draw.ellipse([sx -  3, sy -  3, sx +  3, sy +  3], fill=(255, 255, 255))

        # Legend (bottom-right)
        legend_items = [
            ((76,  175, 80),  "Low (FWI < 5)"),
            ((255, 193,  7),  "Moderate (5–12)"),
            ((230, 100, 20),  "High (12–20)"),
            ((198,  40, 40),  "Very high (20–30)"),
            ((74,   0,  20),  "Extreme (> 30)"),
        ]
        n_leg = len(legend_items)
        row_h = 22
        leg_w, leg_h = 178, n_leg * row_h + 10
        lx0 = width_px - leg_w - 6
        ly0 = height_px - leg_h - 6
        draw.rectangle([lx0, ly0, width_px - 6, height_px - 6], fill=(20, 20, 20))
        for i, (color, label) in enumerate(legend_items):
            cy = ly0 + 6 + i * row_h
            draw.ellipse([lx0 + 6, cy + 2, lx0 + 18, cy + 14], fill=color, outline=(255, 255, 255))
            draw.text((lx0 + 24, cy), label, fill=(235, 235, 235), font=font_sm)

        # Timestamp (bottom-left)
        tz = ZoneInfo(tz_name)
        date_str = now_utc.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%b %d, %Y %Z")
        draw.rectangle([4, height_px - 26, 260, height_px - 4], fill=(20, 20, 20))
        draw.text((8, height_px - 24), f"As of {date_str}", fill=(220, 220, 220), font=font_sm)

        out_buf = _io.BytesIO()
        img.save(out_buf, format="PNG")
        png_bytes = out_buf.getvalue()

        n = len(fires)
        caption = (
            f"{n} satellite fire hotspot{'s' if n != 1 else ''} within 600 km, "
            f"past 7 days, as of {date_str}. "
            f"Colour and size indicate Fire Weather Index (FWI). "
            f"Basemap: NASA Blue Marble Next Generation (August 2023 composite). "
            f"Source: Canadian Wildland Fire Information System (CWFIS) / Natural Resources Canada."
        )
        return png_bytes, caption

    except Exception as e:
        print(f"WILDFIRE MAP BUILD FAILED: {e}")
        return None, "Wildfire map could not be generated — see Action logs."


def build_wildfire_section(fires, site_lat, site_lon, now_utc, tz_name,
                           bbox_3413=None, center_x=None, center_y=None,
                           rotation_deg=0.0, half_width_m=150_000):
    """
    Builds Notion blocks for the wildfire hotspot section.
    fires: list returned by fetch_cwfis_wildfires (may be empty).
    When bbox_3413/center_x/center_y are supplied, the map uses a Blue
    Marble satellite basemap at the same extent as the MODIS image.
    """
    section_heading = heading("🔥 Wildfire Activity — CWFIS Hotspots (7-day)", level=2)

    if fires is None:
        return [
            section_heading,
            callout("Wildfire data unavailable — fetch failed. Check Action logs.", color="gray_background"),
            divider(),
        ]

    if not fires:
        return [
            section_heading,
            callout("No active fire hotspots detected within 600 km in the past 7 days.", color="green_background"),
            divider(),
        ]

    # Summary statistics
    fwi_vals = [f["fwi"] for f in fires if f["fwi"] is not None]
    max_fwi = max(fwi_vals) if fwi_vals else None
    n = len(fires)

    # Nearest fire distance
    km_per_deg_lat = 111.1
    km_per_deg_lon = 111.1 * math.cos(math.radians(site_lat))
    def _dist_km(f):
        dlat = (f["lat"] - site_lat) * km_per_deg_lat
        dlon = (f["lon"] - site_lon) * km_per_deg_lon
        return math.sqrt(dlat**2 + dlon**2)
    nearest_km = min(_dist_km(f) for f in fires)

    fwi_label = (
        f"Max FWI: {max_fwi:.0f}" if max_fwi is not None else "FWI: N/A"
    )
    summary = [
        ("Active hotspots (7-day, 600 km radius): ", str(n)),
        ("Nearest hotspot: ", f"{nearest_km:.0f} km"),
        (fwi_label, ""),
    ]
    callout_color = (
        "red_background"    if (max_fwi or 0) >= 20 else
        "orange_background" if (max_fwi or 0) >= 12 else
        "yellow_background" if fires else
        "green_background"
    )

    map_bytes, map_caption = build_wildfire_map(
        fires, site_lat, site_lon, now_utc, tz_name,
        bbox_3413=bbox_3413, center_x=center_x, center_y=center_y,
        rotation_deg=rotation_deg, half_width_m=half_width_m,
    )
    map_block, fallback = _upload_chart_or_caption(map_bytes, "wildfire_map.png",
                                                    "Wildfire map could not be generated — see Action logs.")

    blocks = [section_heading, callout(summary, color=callout_color)]
    if map_block:
        blocks.append(map_block)
    blocks.append(gray_caption(fallback or map_caption))
    blocks.append(divider())
    return blocks


def publish_blocks_to_notion(blocks):
    """
    Clears every existing block on the configured Notion page, then
    appends the new set. Shared by every site's entrypoint — the actual
    page-clear-and-republish mechanics never need to vary per site.
    """
    existing = notion.blocks.children.list(block_id=PAGE_ID)
    print("EXISTING BLOCK COUNT:", len(existing["results"]))

    for b in existing["results"]:
        notion.blocks.delete(block_id=b["id"])

    response = notion.blocks.children.append(block_id=PAGE_ID, children=blocks)
    print("APPEND RESPONSE BLOCK COUNT:", len(response.get("results", [])))
    print("Dashboard updated successfully")
    return response





