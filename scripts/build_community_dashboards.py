"""
Build static dashboard HTML for all communities from the Herschel template.

Usage:
    python scripts/build_community_dashboards.py [--community <id>]

Without --community, builds all communities listed in COMMUNITIES.
Reads docs/herschel/index.html as the base template and writes
docs/<community>/index.html with per-community substitutions.
Also copies manifest.json, sw.js, and alfred-logo.png.
"""

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
COMMS = ROOT / "communities"
HERSCHEL_DIR = DOCS / "herschel"

# Per-community display metadata (supplements sparse config.json fields)
# Keys: id, display_name, display_name_alt, region, alerts_prov
# Everything else is read from config.json
META = {
    "aklavik": {
        "display_name": "Aklavik", "display_name_alt": "",
        "region": "Northwest Territories", "alerts_prov": "nt",
    },
    "shingle-point": {
        "display_name": "Shingle Point", "display_name_alt": "Taqpaq",
        "region": "Yukon Coast", "alerts_prov": "yt",
    },
    "barrow": {
        "display_name": "Utqiaġvik", "display_name_alt": "Barrow",
        "region": "Alaska North Slope", "alerts_prov": "",  # US — no EC alerts
    },
    "hendrickson-island": {
        "display_name": "Hendrickson Island", "display_name_alt": "",
        "region": "Mackenzie Delta", "alerts_prov": "nt",
    },
    "inuvik": {
        "display_name": "Inuvik", "display_name_alt": "",
        "region": "Northwest Territories", "alerts_prov": "nt",
    },
    "paulatuk": {
        "display_name": "Paulatuk", "display_name_alt": "",
        "region": "Mackenzie Coast", "alerts_prov": "nt",
    },
    "police-cabin": {
        "display_name": "Police Cabin", "display_name_alt": "Police Cabin Lake",
        "region": "Northwest Territories", "alerts_prov": "nt",
    },
    "prudhoe-bay": {
        "display_name": "Prudhoe Bay", "display_name_alt": "",
        "region": "Alaska North Slope", "alerts_prov": "",  # US
    },
    "sachs-harbour": {
        "display_name": "Sachs Harbour", "display_name_alt": "",
        "region": "Banks Island", "alerts_prov": "nt",
    },
    "trail-valley": {
        "display_name": "Trail Valley Creek", "display_name_alt": "",
        "region": "Northwest Territories", "alerts_prov": "nt",
    },
    "tuktoyaktuk": {
        "display_name": "Tuktoyaktuk", "display_name_alt": "",
        "region": "Mackenzie Delta", "alerts_prov": "nt",
    },
    "ulukhaktok": {
        "display_name": "Ulukhaktok", "display_name_alt": "",
        "region": "Victoria Island", "alerts_prov": "nt",
    },
}


def coord_str(lat, lon):
    lat_s = f"{abs(lat):.3f}°{'N' if lat >= 0 else 'S'}"
    lon_s = f"{abs(lon):.3f}°{'E' if lon >= 0 else 'W'}"
    return f"{lat_s} · {lon_s}"


def build_links(cfg, meta, blocks):
    """Build the data-sources links list for this community."""
    links = []
    tid = cfg.get("tide_station_code", "")
    noaa_tid = cfg.get("noaa_tide_station_id", "")
    mzone = cfg.get("marine_zone_id", "")
    prov = meta["alerts_prov"]

    if tid:
        links.append({
            "icon": "🌊",
            "label": f"Tide gauge — {cfg.get('tide_station_name', meta['display_name'])}",
            "src": f"Fisheries & Oceans Canada · Stn {tid}",
            "url": f"https://www.tides.gc.ca/en/stations/{tid}",
        })
    if noaa_tid:
        links.append({
            "icon": "🌊",
            "label": f"Tide gauge — {meta['display_name']}",
            "src": f"NOAA Tides & Currents · Stn {noaa_tid}",
            "url": f"https://tidesandcurrents.noaa.gov/stationhome.html?id={noaa_tid}",
        })
    if mzone:
        zone_name = cfg.get("marine_zone_name", meta["region"])
        links.append({
            "icon": "⛅",
            "label": f"{zone_name} marine forecast",
            "src": f"Environment Canada · Zone {mzone}",
            "url": f"https://weather.gc.ca/marine/region_e.html?id=MID",
        })
    if prov:
        links.append({
            "icon": "⚠️",
            "label": f"Weather alerts — {meta['region']}",
            "src": "Environment Canada",
            "url": f"https://weather.gc.ca/warnings/index_e.html?prov={prov}",
        })
    links.append({
        "icon": "🌡️",
        "label": "Weather forecast — GEM/ERA5",
        "src": "Open-Meteo · ECCC GEM seamless",
        "url": "https://open-meteo.com",
    })
    links.append({
        "icon": "💻",
        "label": "Dashboard source code",
        "src": "Alfred Wegener Institute · GitHub",
        "url": "https://github.com/hlantuit/alfred-portal",
    })
    return links


def links_html(links):
    parts = []
    for lk in links:
        parts.append(
            f'    <a class="lk" href="{lk["url"]}" target="_blank" rel="noopener">\n'
            f'      <span class="lk-ico">{lk["icon"]}</span>\n'
            f'      <div><div class="lk-label">{lk["label"]}</div>'
            f'<div class="lk-src">{lk["src"]}</div></div>\n'
            f'    </a>'
        )
    return "\n".join(parts)


def build_dashboard(cid):
    cfg_path = COMMS / cid / "config.json"
    if not cfg_path.exists():
        print(f"  skip {cid}: no config.json")
        return

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    meta = META.get(cid, {
        "display_name": cfg.get("name", cid),
        "display_name_alt": "",
        "region": "",
        "alerts_prov": "",
    })

    blocks = cfg.get("blocks", [])
    lat = cfg.get("lat", 0.0)
    lon = cfg.get("lon", 0.0)
    disp = meta["display_name"]
    alt = meta["display_name_alt"]
    region = meta["region"]
    coords = coord_str(lat, lon)

    title = f"{disp} · {alt}" if alt else disp
    title_full = f"{disp} — {alt}" if alt else disp  # for sat-title

    # Read template
    tmpl = HERSCHEL_DIR.joinpath("index.html").read_text(encoding="utf-8")
    html = tmpl

    # ── Text substitutions ──────────────────────────────────────────────────
    # <title>
    html = html.replace(
        "<title>Qikiqtaruk · Herschel Island</title>",
        f"<title>{title}</title>",
    )
    # meta description
    html = html.replace(
        'content="Environmental conditions dashboard — Qikiqtaruk / Herschel Island, Yukon Coast"',
        f'content="Environmental conditions dashboard — {title}, {region}"',
    )
    # sidebar eyebrow (region)
    html = html.replace(
        '<div class="sb-eyebrow">Yukon Coast</div>',
        f'<div class="sb-eyebrow">{region}</div>',
    )
    # sidebar name
    if alt:
        html = html.replace(
            '<div class="sb-name">Qikiqtaruk<br><span class="sb-name-alt">Herschel Island</span></div>',
            f'<div class="sb-name">{disp}<br><span class="sb-name-alt">{alt}</span></div>',
        )
    else:
        html = html.replace(
            '<div class="sb-name">Qikiqtaruk<br><span class="sb-name-alt">Herschel Island</span></div>',
            f'<div class="sb-name">{disp}</div>',
        )
    # sidebar coords
    html = html.replace(
        '<div class="sb-meta">69.590°N · 139.099°W</div>',
        f'<div class="sb-meta">{coords}</div>',
    )
    # mobile topbar
    html = html.replace(
        "<div class=\"tb-title\">Qikiqtaruk · Herschel Island</div><div class=\"tb-sub\">69.590°N · 139.099°W</div>",
        f'<div class="tb-title">{title}</div><div class="tb-sub">{coords}</div>',
    )
    # MODIS sat-title
    html = html.replace(
        '<div class="sat-title">Qikiqtaruk — Beaufort Sea</div>',
        f'<div class="sat-title">{title_full}</div>',
    )
    # Marine section heading
    html = html.replace(
        "<div class=\"sec-hd\">Marine forecast · Yukon Coast</div>",
        f'<div class="sec-hd">Marine forecast · {region}</div>',
    )
    # Tide station label
    html = html.replace(
        "DFO IWLS · Herschel Island station 06525",
        f"DFO IWLS · {cfg.get('tide_station_name', disp)} station {cfg.get('tide_station_code', '—')}",
    )
    # Ice section heading — sea ice for coastal, lake/river ice for inland
    if "sea_ice" not in blocks and "lake_river_ice" in blocks:
        html = html.replace(
            '<div id="ice" class="sec"><div class="sec-hd">Sea ice · Sentinel-1 SAR</div></div>',
            '<div id="ice" class="sec"><div class="sec-hd">Lake · River ice · Sentinel-1 SAR</div></div>',
        )
    # SAR caption
    html = html.replace(
        "Sentinel-1 SAR · VV polarisation · Herschel Island area",
        f"Sentinel-1 SAR · VV polarisation · {disp} area",
    )
    # Fog block community ID (in both the key literal and the replace() call)
    html = html.replace(
        "const _fogKey = 'herschel'.replace(/-/g,'_');",
        f"const _fogKey = '{cid}'.replace(/-/g,'_');",
    )
    # Links grid — replace the entire static fallback block
    lk_html = links_html(build_links(cfg, meta, blocks))
    html = re.sub(
        r'(<div class="links-grid" id="linksGrid">).*?(</div>\s*\n\s*\n\s*<div class="foot">)',
        lambda m: m.group(1) + "\n" + lk_html + "\n  " + m.group(2),
        html,
        flags=re.DOTALL,
    )

    # ── Section visibility ──────────────────────────────────────────────────
    hide = []
    if "marine" not in blocks and "wave_forecast" not in blocks:
        hide.append("#marine")
        hide.append("#marine-content")
    if "sea_ice" not in blocks and "lake_river_ice" not in blocks:
        hide.append("#ice")
    if "water_level" not in blocks and "hydrometric" not in blocks:
        hide.append("#water")
    # Hide tide charts for non-coastal (no DFO/TOPAZ data)
    # Note: #cc-tide condition card is hidden by default in HTML and shown by JS
    if "water_level" not in blocks:
        hide.append("#tide-row")

    if hide:
        hide_css = (
            "<style>\n"
            + ",".join(hide) + "{display:none!important}\n"
            + "</style>\n"
        )
        html = html.replace("</head>", hide_css + "</head>")

    # ── Write output ────────────────────────────────────────────────────────
    out_dir = DOCS / cid
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")

    # Copy static assets from herschel
    for asset in ("manifest.json", "sw.js", "alfred-logo.png"):
        src = HERSCHEL_DIR / asset
        dst = out_dir / asset
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

    # Patch manifest.json with community name
    mf_path = out_dir / "manifest.json"
    if mf_path.exists():
        mf = json.loads(mf_path.read_text(encoding="utf-8"))
        mf["name"] = f"ALFRED · {title}"
        mf["short_name"] = disp
        mf_path.write_text(json.dumps(mf, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"  OK {cid} -> docs/{cid}/index.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--community", help="Build only this community")
    args = ap.parse_args()

    if args.community:
        targets = [args.community]
    else:
        targets = list(META.keys())

    for cid in targets:
        build_dashboard(cid)


if __name__ == "__main__":
    main()
