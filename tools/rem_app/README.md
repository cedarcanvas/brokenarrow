# River REM Studio

Pick a stretch of river on a map and get a River Relative Elevation Model.

```bash
tools/rem_app.sh
```

Opens http://127.0.0.1:5057.

## Workflow

1. **Area**, in one of two modes:
   - **Pick stretch** (default): click *Click start & end*, then click two points on a
     river. The app finds the named river nearest both points (within 500 m) and traces it
     between them. It builds the area as a corridor around the stretch; choose the width
     each side (0.5 to 5 km). Drag the A/B pins to adjust. By default the output is
     clipped to the corridor.
   - **Draw box**: drag a box, then pick one of the named rivers inside it.
2. **River**: river lines come from USGS NHD HighRes. If NHD is down, they come from
   OpenStreetMap instead, and the app shows which one it used. RiverREM always uses the
   whole river within the area as its centerline, so the surface doesn't cut off at the
   stretch ends.
3. **Settings**: choose the DEM resolution (1 / 3 / 10 / 30 m; each option shows its pixel
   count) and a color ramp.
4. **Generate**: the app downloads USGS 3DEP elevation for the box and reprojects it to
   UTM. It runs RiverREM with the chosen river as the centerline, then overlays the
   result on the map.

Each run is saved to `river_rem_runs/<timestamp>_<river>_<res>m/`:

| File | What |
| --- | --- |
| `dem_*_hillshade-color.tif` / `.png` | finished REM visualization |
| `dem_*_REM.tif` | raw REM (metres above the river surface) |
| `dem_*.tif` | 3DEP DEM used |
| `centerline.gpkg` | river centerline used |
| `stretch.geojson`, `corridor.geojson` | stretch mode: traced stretch and corridor polygon |
| `*_corridor.tif` / `.png` | stretch mode: outputs clipped to the corridor |
| `job.json`, `run.log` | parameters, status, log |

## Command line

The same pipeline runs without the UI:

```bash
conda run -n rem_env python tools/rem_app/pipeline.py rivers --bbox -106.19 38.79 -106.10 38.88
conda run -n rem_env python tools/rem_app/pipeline.py run --bbox -106.19 38.79 -106.10 38.88 \
    --river "Arkansas River" --res 10 --cmap mako

# stretch between two points (lon lat), 1 km corridor each side
conda run -n rem_env python tools/rem_app/pipeline.py trace --start -106.150 38.875 --end -106.105 38.805
conda run -n rem_env python tools/rem_app/pipeline.py run --start -106.150 38.875 --end -106.105 38.805 \
    --corridor 1000 --res 10 --cmap mako   # add --no-clip to keep the full rectangle
```

## Notes

- Setup: `mamba create -n rem_env -c conda-forge riverrem "osmnx=1.9.4" flask`
- River lines are cached in `.osm_cache/rem_app/`. Any later request inside an area
  already fetched reuses the cache.
- The USGS NHD service often returns 502/504 errors or times out. When it does, the app
  falls back to OpenStreetMap. If both are down, it says so; click *Retry* or nudge a pin.
- 1 m lidar isn't available everywhere. Where it's missing, 3DEP resamples the best
  available data.
- Runs are capped at 400 megapixels (about 20×20 km at 1 m). For 1 m runs over 60 MP, expect
  several minutes and a few GB of RAM.
- For a lidar tile you already have locally, use `tools/river_rem.sh` instead.
