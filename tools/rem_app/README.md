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
4. **Settings**: DEM resolution (1 / 3 / 10 / 30 m; each option shows its pixel count),
   color ramp, and how the colors step through the heights:
   - **Smooth / Stepped**: a continuous gradient, or distinct color bands (4–24 steps).
     Each band gets an evenly spaced color from the ramp, so thin bands near the river
     still look clearly different.
   - **Logarithmic** (on by default): bands are thin near the stream and widen with height
     (e.g. 0, 2, 5, 10, 18, 30, 50, 80 … ft), using RiverREM's own log spacing. Off spaces
     them evenly on a round step (0, 100, 200 … ft).
   - **Lowest band** (stepped): height of the first band, e.g. 2 m. The other bands still
     widen with height, but from that starting size, so steep valleys don't squeeze the
     floodplain into a few slivers along the channel (with a 100 m top: 0, 2, 4, 8, 12,
     16 … m). With Logarithmic off it is the step size.
   - **Top of ramp**: where the colors end; everything higher gets the last color. Blank
     uses RiverREM's automatic top (half the highest point above the river). Lowering it
     (e.g. 60–100 m) spends the colors on the valley floor rather than the ridges.
   - **Units**: feet or metres, for these fields and the print legend.
   - The strip under the controls previews the bands and labels. With an automatic top it
     assumes 500 ft / 150 m, because the real top is only known once the REM is computed.
   Smooth + Logarithmic + automatic top matches RiverREM's default look.
5. **Generate**: the app downloads USGS 3DEP elevation covering the whole rectangle, runs
   RiverREM, overlays the result on the map, and builds the print layout in QGIS.
6. **Recolor this run** (under Result): applies the current color ramp, steps, units and
   title to a finished run, reusing its DEM, REM and hillshade, so it takes seconds instead
   of a new download. Opening a past run loads its color settings into the controls to tweak
   from. Each color style gets its own print files, so you can compare versions. A run whose
   print or recolor failed can still be recolored, since its elevation data is kept.

### Print layout

Built headless with the newest installed QGIS (override with `QGIS_APP=/Applications/….app`):

- Title in **High Alpine**; everything else in **Neue Frutiger World** (Book / Medium).
  If a font isn't available (e.g. Adobe Fonts deactivated) **Helvetica** is used instead;
  `job.json` records which fonts were used.
- Map rotated to the frame, color legend (feet or metres) matching the map colors, scale bars
  in miles and kilometres, a north arrow that turns with the map, scale, and data credits.
- Type, margins and line weights scale with the page, so every size has the same look.
- Outputs: vector **PDF** (fonts embedded, georeferenced), **PNG** at 300 dpi, and a
  **QGIS project** (`.qgz`) to adjust the layout by hand.

Each run is saved to `river_rem_runs/<timestamp>_<river>_<res>m/`:

| File | What |
| --- | --- |
| `<river>_<page>_<layout>_<colors>.pdf` / `.png` / `.qgz` | print layout, one set per color style |
| `dem_*_hillshade-color.tif` / `.png` | REM visualization (north-up, covers the frame) |
| `dem_*_REM.tif` | raw REM (metres above the river surface) |
| `dem_*.tif` | 3DEP DEM used |
| `centerline.gpkg` | river centerline used |
| `frame.geojson` | print frame rectangle |
| `stretch.geojson` | stretch mode: traced stretch |
| `color_table.txt` | the gdaldem color table used to color the REM (latest style) |
| `.hillshade/` | saved hillshade, reused when recoloring |
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

# stepped colors: 12 bands up to 100 m, starting with a 2 m band (--spacing linear for even bands)
conda run -n rem_env python tools/rem_app/pipeline.py run --start -106.150 38.875 --end -106.105 38.805 \
    --ramp stepped --steps 12 --units m --top 100 --first 2

# recolor an existing run without downloading again; options left out keep the run's values
conda run -n rem_env python tools/rem_app/pipeline.py restyle --run river_rem_runs/<run> \
    --cmap rocket --first 3
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
