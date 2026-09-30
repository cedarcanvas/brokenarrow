# River REM Studio

Draw a box on a map, pick a river, get a River Relative Elevation Model.

```bash
tools/rem_app.sh
```

Opens http://127.0.0.1:5057.

## Workflow

1. **Area**: click *Draw area* and drag a box around the stretch of river.
2. **River**: named rivers in the box load from USGS NHD HighRes. If NHD is down, they
   load from OpenStreetMap instead. Click one on the map or in the list. Ditches and
   canals are hidden unless you tick the box.
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
| `job.json`, `run.log` | parameters, status, log |

## Command line

The same pipeline runs without the UI:

```bash
conda run -n rem_env python tools/rem_app/pipeline.py rivers --bbox -106.19 38.79 -106.10 38.88
conda run -n rem_env python tools/rem_app/pipeline.py run --bbox -106.19 38.79 -106.10 38.88 \
    --river "Arkansas River" --res 10 --cmap mako
```

## Notes

- Setup: `mamba create -n rem_env -c conda-forge riverrem "osmnx=1.9.4" flask`
- River lines for each box are cached in `.osm_cache/rem_app/`, so re-running the same area
  doesn't query NHD again.
- 1 m lidar isn't available everywhere. Where it's missing, 3DEP resamples the best
  available data.
- Runs are capped at 400 megapixels (about 20×20 km at 1 m). For 1 m runs over 60 MP, expect
  several minutes and a few GB of RAM.
- For a lidar tile you already have locally, use `tools/river_rem.sh` instead.
