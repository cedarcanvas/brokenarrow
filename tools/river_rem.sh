#!/bin/zsh

# Generate a River Relative Elevation Model from a DEM using RiverREM.
# https://github.com/OpenTopography/RiverREM
#
# Usage:
#   tools/river_rem.sh <dem.tif> [out_dir] [cmap]
#
# Env overrides:
#   CONDA_ENV       conda env name (default: rem_env)
#   CONDA_BIN       conda binary path (default: /Users/michaelfloyd/miniforge3/bin/conda)
#   CENTERLINE_SHP  optional river centerline shapefile (overrides OSM)
#   Z               hillshade z factor (default: 4)
#   BLEND_PERCENT   hillshade weight 0-100 in the blended viz (default: 25)
#   MAKE_KMZ        set to 1 to also write a Google Earth kmz
#   INTERP_PTS      max interpolation points along centerline (default: 1000)
#   STAGE_DIR       where cloud-synced DEMs are copied before processing
#                   (default: ${TMPDIR}/river_rem_stage)
#
# Setup (one time):
#   mamba create -n rem_env -c conda-forge riverrem "osmnx=1.9.4"
#   osmnx 1.9.3 crashes on shapely 2 when fetching OSM centerlines.
#
# DEMs under ~/Library/CloudStorage (Dropbox, Google Drive, ...) are copied to
# STAGE_DIR first. Reading an online-only file directly makes GDAL block inside
# fread() with no output while the sync client downloads it; copying makes that
# step visible and fails fast if the download stalls.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  print -u2 "Usage: $0 <dem.tif> [out_dir] [cmap]"
  exit 1
fi

DEM="${1:A}"
OUT_DIR="${2:-$PWD/river_rem_$(basename "$DEM" .tif)}"
CMAP="${3:-topo}"

CONDA_BIN="${CONDA_BIN:-/Users/michaelfloyd/miniforge3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-rem_env}"
CENTERLINE_SHP="${CENTERLINE_SHP:-}"
Z="${Z:-4}"
BLEND_PERCENT="${BLEND_PERCENT:-25}"
MAKE_KMZ="${MAKE_KMZ:-0}"
INTERP_PTS="${INTERP_PTS:-1000}"

if [[ ! -f "$DEM" ]]; then
  print -u2 "DEM not found: $DEM"
  exit 1
fi

if [[ ! -x "$CONDA_BIN" ]]; then
  print -u2 "conda not found at $CONDA_BIN (override with CONDA_BIN=...)"
  exit 1
fi

if [[ "$DEM" == "$HOME/Library/CloudStorage/"* ]]; then
  STAGE_DIR="${STAGE_DIR:-${TMPDIR:-/tmp}/river_rem_stage}"
  mkdir -p "$STAGE_DIR"
  staged="$STAGE_DIR/$(basename "$DEM")"
  src_size="$(stat -f %z "$DEM")"
  if [[ ! -f "$staged" || "$(stat -f %z "$staged")" != "$src_size" ]]; then
    print -r -- "Staging cloud-synced DEM to $staged ($(( src_size / 1048576 )) MB)..."
    print -r -- "If this stalls, open Dropbox and make the file available offline."
    cp "$DEM" "$staged.part"
    mv "$staged.part" "$staged"
  fi
  DEM="$staged"
fi

mkdir -p "$OUT_DIR"
OUT_DIR="${OUT_DIR:A}"

print -r -- "RiverREM: $DEM -> $OUT_DIR (cmap=$CMAP)"

"$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python - <<PY
import os, sys, logging
from riverrem.REMMaker import REMMaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

dem = r"""$DEM"""
out_dir = r"""$OUT_DIR"""
centerline = r"""$CENTERLINE_SHP""" or None
cmap = r"""$CMAP"""
make_kmz = bool(int(r"""$MAKE_KMZ"""))

rem = REMMaker(
    dem=dem,
    out_dir=out_dir,
    centerline_shp=centerline,
    interp_pts=int(r"""$INTERP_PTS"""),
    cache_dir=os.path.join(out_dir, ".cache"),
)

rem_ras = rem.make_rem()
viz_ras = rem.make_rem_viz(
    cmap=cmap,
    z=float(r"""$Z"""),
    blend_percent=float(r"""$BLEND_PERCENT"""),
    make_png=True,
    make_kmz=make_kmz,
)

print(f"REM raster: {rem_ras}")
print(f"Visualization: {viz_ras}")
PY

print -r -- "Done. Outputs in: $OUT_DIR"
