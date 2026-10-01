# River REM Studio

Pick a stretch of river on a map and get a print-ready River Relative Elevation Model.

```bash
tools/rem_app.sh
```

Opens http://127.0.0.1:5057.

## Workflow

1. **Area**, in one of two modes:
   - **Pick stretch** (default): click *Click start & end*, then click two points on a
     river. The app finds the named river nearest both points (within 500 m) and traces it
     between them. Choose how much to show each side of the river (0.5 to 5 km). Drag the
     A/B pins to adjust.
   - **Draw box**: drag a box, then pick one of the named rivers inside it.
2. **River**: river lines come from USGS NHD HighRes. If NHD is down, they come from
   OpenStreetMap instead, and the app shows which one it used. RiverREM always uses the
   whole river within the area as its centerline, so the surface doesn't cut off at the
   stretch ends.
3. **Print**: page size (Letter, 11×17, 18×24, 24×36), layout (Auto / Horizontal /
   Vertical), title and subtitle. The dashed rectangle on the map is exactly what the page
   will show:
   - In stretch mode the map is rotated so the river runs horizontally (landscape page)
     or vertically (portrait). *Auto* picks whichever needs less rotation from north-up.
   - The rectangle is set to the smallest round scale (1:24,000, 1:30,000, …) that fits
     the stretch plus the width you chose, then grown to fill the page's map area.
   - Box mode keeps north up and grows your box to the page's proportions.
4. **Settings**: DEM resolution (1 / 3 / 10 / 30 m; each option shows its pixel count) and
   color ramp.
5. **Generate**: the app downloads USGS 3DEP elevation covering the whole rectangle, runs
   RiverREM, overlays the result on the map, and builds the print layout in QGIS.

### Print layout

Built headless with the newest installed QGIS (override with `QGIS_APP=/Applications/….app`):

- Title in **High Alpine**; everything else in **Neue Frutiger World** (Book / Medium).
  If a font isn't available (e.g. Adobe Fonts deactivated) **Helvetica** is used instead;
  `job.json` records which fonts were used.
- Map rotated to the frame, color legend in feet matching RiverREM's log scale, scale bars
  in miles and kilometres, a north arrow that turns with the map, scale, and data credits.
- Type, margins and line weights scale with the page, so every size has the same look.
- Outputs: vector **PDF** (fonts embedded, georeferenced), **PNG** at 300 dpi, and a
  **QGIS project** (`.qgz`) to adjust the layout by hand.

Each run is saved to `river_rem_runs/<timestamp>_<river>_<res>m/`:

| File | What |
| --- | --- |
| `<river>_<page>_<layout>.pdf` / `.png` / `.qgz` | print layout |
| `dem_*_hillshade-color.tif` / `.png` | REM visualization (north-up, covers the frame) |
| `dem_*_REM.tif` | raw REM (metres above the river surface) |
| `dem_*.tif` | 3DEP DEM used |
| `centerline.gpkg` | river centerline used |
| `frame.geojson` | print frame rectangle |
| `stretch.geojson` | stretch mode: traced stretch |
| `print_spec.json` | everything passed to the layout script |
| `job.json`, `run.log` | parameters, status, log |

## Command line

The same pipeline runs without the UI:

```bash
conda run -n rem_env python tools/rem_app/pipeline.py rivers --bbox -106.19 38.79 -106.10 38.88
conda run -n rem_env python tools/rem_app/pipeline.py run --bbox -106.19 38.79 -106.10 38.88 \
    --river "Arkansas River" --res 10 --cmap mako --page letter

# stretch between two points (lon lat), at least 1 km each side, on an 18x24 page
conda run -n rem_env python tools/rem_app/pipeline.py trace --start -106.150 38.875 --end -106.105 38.805
conda run -n rem_env python tools/rem_app/pipeline.py run --start -106.150 38.875 --end -106.105 38.805 \
    --corridor 1000 --res 10 --page 18x24 --orientation auto --title "Arkansas River" \
    --subtitle "Buena Vista"            # --no-print skips the QGIS layout
```

## Notes

- Setup: `mamba create -n rem_env -c conda-forge riverrem "osmnx=1.9.4" flask`, plus QGIS
  4.x in /Applications.
- River lines are cached in `.osm_cache/rem_app/`. Any later request inside an area
  already fetched reuses the cache.
- The USGS NHD service often returns 502/504 errors or times out. When it does, the app
  falls back to OpenStreetMap. If both are down, it says so; click *Retry* or nudge a pin.
- 3DEP times out on 4000 px tiles at 1 m, so DEMs are fetched as 2000 px tiles, and any
  tile the server still rejects is split into quarters and retried (down to 250 px).
- 1 m lidar isn't available everywhere. Where it's missing, 3DEP resamples the best
  available data.
- Runs are capped at 400 megapixels. A rotated frame needs a larger north-up DEM than its
  own area, so 1 m on big pages adds up quickly: an 11×17 frame at 1:24,000 is about
  100 MP and takes 10+ minutes.
- For a lidar tile you already have locally, use `tools/river_rem.sh` instead.
