# Alfred — Environmental Dashboards: Portal Map

Single-file static page (`index.html`) — a circumpolar-projected map with animated
wind streaks and site pins linking to the individual Alfred dashboards.

No build step, no dependencies to install. Everything (Leaflet, Proj4Leaflet)
loads from CDN at runtime.

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
   `https://<your-username>.github.io/alfred-portal/`
   (takes ~1 minute to go live after the first push.)

## Embed in Notion

On the Notion page: type `/embed`, paste the GitHub Pages URL above, press Enter.
Notion renders it as a live, interactive iframe — pan/zoom/tap all work the same
as opening it directly.

## Wiring to real data (next step)

This currently uses synthetic wind data and placeholder site conditions.
See `ALFRED_PORTAL_PLAN.md` (in the main project outputs) for the data
contract to connect it to the existing ECMWF/TOPAZ6 pipeline.

## Known constraints

- Basemap tiles: Esri Arctic Ocean Base (EPSG:3413, polar stereographic) —
  requires `server.arcgisonline.com` to be reachable; no API key needed.
- Projection libraries: Leaflet 1.9.4, Proj4js 2.9.0, Proj4Leaflet 1.0.2,
  all loaded from cdnjs.cloudflare.com.
