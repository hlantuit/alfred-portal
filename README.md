# Alfred — Environmental Dashboards: Portal Map

**Access the ALFRED Portal:** https://hlantuit.github.io/alfred-portal/

This repository contains a single-file static portal page (`index.html`): a circumpolar-projected map with animated wind streaks and site pins linking to the individual Alfred dashboards.

## Portal and dashboards

| Site | Dashboard |
|---|---|
| Herschel Island | [Open dashboard](https://hlantuit.github.io/alfred-portal/herschel/) |
| Shingle Point | [Open dashboard](https://hlantuit.github.io/alfred-portal/shingle-point/) |
| Aklavik | [Open dashboard](https://hlantuit.github.io/alfred-portal/aklavik/) |
| Barrow | [Open dashboard](https://hlantuit.github.io/alfred-portal/barrow/) |
| Hendrickson Island | [Open dashboard](https://hlantuit.github.io/alfred-portal/hendrickson-island/) |
| Inuvik | [Open dashboard](https://hlantuit.github.io/alfred-portal/inuvik/) |
| Paulatuk | [Open dashboard](https://hlantuit.github.io/alfred-portal/paulatuk/) |
| Police Cabin | [Open dashboard](https://hlantuit.github.io/alfred-portal/police-cabin/) |
| Prudhoe Bay | [Open dashboard](https://hlantuit.github.io/alfred-portal/prudhoe-bay/) |
| Sachs Harbour | [Open dashboard](https://hlantuit.github.io/alfred-portal/sachs-harbour/) |
| Trail Valley | [Open dashboard](https://hlantuit.github.io/alfred-portal/trail-valley/) |
| Tuktoyaktuk | [Open dashboard](https://hlantuit.github.io/alfred-portal/tuktoyaktuk/) |
| Ulukhaktok | [Open dashboard](https://hlantuit.github.io/alfred-portal/ulukhaktok/) |

## How it works

No build step and no dependencies to install. Everything (Leaflet, Proj4Leaflet) loads from CDN at runtime.

## Deploy to GitHub Pages

1. Create a new repository on GitHub (e.g. `alfred-portal`), public.
2. Push this folder's contents to it:

   ```bash
   cd alfred-portal
   git init
   git add .
   git commit -m "Alfred portal map"
   git branch -M main
   git remote add origin https://github.com/<your-username>/alfred-portal.git
   git push -u origin main
   ```

3. On GitHub: **Settings → Pages → Source → Deploy from a branch → `main` / `(root)`** → Save.
4. GitHub gives you a URL, typically:

   ```text
   https://<your-username>.github.io/alfred-portal/
   ```

   It takes about one minute to go live after the first push.

## Embed in Notion

On the Notion page: type `/embed`, paste the GitHub Pages URL above, press Enter. Notion renders it as a live, interactive iframe — pan, zoom, and tap all work the same as opening it directly.

## Wiring to real data

This currently uses synthetic wind data and placeholder site conditions. See `ALFRED_PORTAL_PLAN.md` (in the main project outputs) for the data contract to connect it to the existing ECMWF/TOPAZ6 pipeline.

## Known constraints

- Basemap tiles: Esri Arctic Ocean Base (EPSG:3413, polar stereographic) — requires `server.arcgisonline.com` to be reachable; no API key needed.
- Projection libraries: Leaflet 1.9.4, Proj4js 2.9.0, Proj4Leaflet 1.0.2, all loaded from cdnjs.cloudflare.com.
