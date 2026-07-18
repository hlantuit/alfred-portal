# Alfred — Environmental Dashboards
### Portal + interactive map: implementation plan

Purpose of this doc: a working spec you (or a Claude Code session) can follow to turn the prototype map into the real front door for all Alfred dashboards. Drop this in the repo root as `ALFRED_PORTAL_PLAN.md` or fold it into a `CLAUDE.md` if running Claude Code sessions against it.

---

## 1. What exists today

- Individual dashboards per site, running on GitHub Actions, pulling ECMWF IFS + TOPAZ6 data (Qikiqtaruk, Shingle Point/Taqpaq, others)
- A working prototype (`arctic-portal-prototype.html`): Leaflet dark map, animated synthetic wind-particle overlay, site pins with popup conditions + dashboard links
- Confirmed: renders well on mobile, no framework dependencies (pure Leaflet + canvas)

## 2. What "done" looks like

A single static page — `index.html` for Alfred — that:
- Shows all monitored sites as pins on one map
- Animates real wind direction/speed as particle streaks (replacing synthetic field)
- Each pin's popup shows live basic conditions (temp, wind, surge/water level) and a link into that site's full dashboard
- Loads fast on a phone on marginal connectivity (Inuvik/Aklavik-realistic, not just Potsdam office wifi)

## 3. Corporate identity (matched to the live Qikiqtaruk dashboard)

Sampled directly from the existing dashboard so the portal reads as the same product, not a separate tool:

| Token | Value | Used for |
|---|---|---|
| `--alfred-blue` | `#00ACE8` | logo tile, primary accent, active states |
| `--alfred-blue-deep` | `#0B8FC2` | links, hover states |
| `--card-bg` | `#E5F2FC` | info card backgrounds (matches "Weather"/"Wind" cards) |
| `--ink` | `#1A1A1A` | headings |
| `--body` | `#5B6672` | body/secondary text |
| `--rule` | `#E9EEF2` | hairline dividers |
| `--green` | `#3FA772` | wind speed figures/charts |
| `--orange` | `#F5A623` | temperature forecast line, wind icon |
| `--red` | `#E0483E` | "now" markers, location pin accents |

- **Font**: body text uses the system UI stack (`-apple-system, "Segoe UI", Inter, Roboto, sans-serif`), matching the dashboard. The **Alfred wordmark specifically uses Trebuchet MS** (confirmed from the source logo file) — the portal sets this only on the brand text, not body copy, so it reads as branding rather than becoming the page's working font.
- **Logo asset**: the actual logo file (`alfred_trebuchet.png`, 512×512, rounded-square blue tile with white 8-point sparkle mark) is now embedded directly — used both in the header and as the map marker icon, so pins on the map are literally the same logo tile rather than a redrawn approximation. For the real build, save this as `assets/alfred-logo.png` in the repo and reference it by path instead of inlining base64.
- **Cards**: light-blue (`--card-bg`) rounded rectangles, no border, used inside popups the same way the dashboard uses them for Weather/Wind/Tide/Sun blocks.
- **Background**: white, light Carto basemap (not the dark theme from the first prototype) — this was switched specifically to match the dashboard's white page background rather than defaulting to a "map app" dark theme.

## 4. Repo structure (proposed)

```
alfred/
├── index.html                  # the portal/map (this is the prototype, wired to real data)
├── data/
│   ├── sites.json               # static-ish: names, coords, dashboard URLs
│   ├── conditions_latest.json    # refreshed each Action run: per-site temp/wind/surge
│   └── wind_grid_latest.json     # refreshed each Action run: u/v grid for the region
├── dashboards/
│   ├── hiq/…                    # existing individual dashboards, unchanged
│   └── shingle-point/…
└── .github/workflows/
    └── update-data.yml          # extend existing pipeline to also emit the two JSON files above
```

Nothing about the existing per-site dashboards needs to change — the portal only *reads* two new lightweight JSON exports from the pipeline that already runs.

## 5. Data contract

### `data/sites.json` (edited by hand, rarely changes)
```json
[
  { "id": "hiq", "name": "Herschel Island – Qikiqtaruk", "lat": 69.575, "lon": -139.08,
    "dashboard": "dashboards/hiq/index.html" },
  { "id": "shingle-point", "name": "Shingle Point / Taqpaq", "lat": 68.96, "lon": -137.23,
    "dashboard": "dashboards/shingle-point/index.html" }
]
```

### `data/conditions_latest.json` (regenerated every Actions run)
```json
{
  "generated_utc": "2026-07-18T06:00:00Z",
  "sites": {
    "hiq": { "air_temp_c": 3, "wind_kmh": 24, "wind_dir_deg": 315, "surge_m": 0.31 },
    "shingle-point": { "air_temp_c": 5, "wind_kmh": 18, "wind_dir_deg": 270, "surge_m": 0.12 }
  }
}
```

### `data/wind_grid_latest.json` (regenerated every Actions run)
Lightweight regional grid — this does **not** need to be the full ECMWF resolution, just enough points (e.g. 15×15) over the Beaufort Sea/ISR extent to drive believable particle motion:
```json
{
  "generated_utc": "2026-07-18T06:00:00Z",
  "bounds": { "south": 68.5, "north": 70.2, "west": -140.5, "east": -136.5 },
  "nx": 15, "ny": 15,
  "u": [[...]],
  "v": [[...]]
}
```
`u`/`v` in m/s, same convention already used internally for the TOPAZ/ECMWF pipeline outputs — reuse rather than re-derive.

## 6. Pipeline change (small, additive)

In the existing GitHub Action that already fetches ECMWF IFS output:
1. After the existing fetch/processing step, add a short export step that subsamples wind u/v to the grid above and writes `wind_grid_latest.json`
2. Pull whatever per-site scalar values the individual dashboards already compute (temp, wind, surge) and write them to `conditions_latest.json`
3. Commit both alongside whatever the existing dashboards commit — no new schedule/trigger needed, same run

This is intentionally kept as a "read what's already computed and reshape it" step, not a new data source.

## 7. Front-end changes to the prototype

- Replace `windAt(lat, lon)` synthetic function with a bilinear sample against `wind_grid_latest.json`
- Replace hardcoded `sites` array with a `fetch('data/sites.json')` + `fetch('data/conditions_latest.json')` merge at load
- Add a small "last updated" timestamp in the badge (currently says "synthetic demo data")
- Identity (light Carto basemap, `--alfred-blue` palette, star marker, card-style popups) is already matched in the prototype — keep as-is, it already tested fine on mobile

## 8. Nice-to-haves (not required for v1)

- Auto-refresh conditions every N minutes without a full page reload (poll the JSON, re-render pins)
- Tap-to-zoom clustering if the site count grows well beyond a handful
- A tide-curve mini-sparkline in the popup for coastal sites, reusing the same curve logic from the TOPAZ4b summer water-level work
- Offline-friendly fallback (cache last-known JSON) for field use in Inuvik/Aklavik with patchy connectivity

## 9. Suggested order of work

1. Add `sites.json` by hand for current dashboards
2. Extend the Actions pipeline to emit `conditions_latest.json` (reuse existing computed values — no new logic)
3. Extend it again to emit `wind_grid_latest.json` (subsample existing ECMWF u/v)
4. Wire the prototype's JS to fetch these three files instead of using hardcoded/synthetic data
5. Deploy as `index.html` at the root of the GitHub Pages site, individual dashboards stay where they are and just get linked from the pins

---
*Prototype file: `arctic-portal-prototype.html` — rename to `index.html` once wired to real data.*
