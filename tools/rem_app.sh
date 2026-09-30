#!/bin/zsh

# Start River REM Studio: draw an area on a map, pick a river, generate a REM.
# Opens http://127.0.0.1:5057 (override with PORT=...). Runs are saved to river_rem_runs/.
#
# Setup (one time):
#   mamba create -n rem_env -c conda-forge riverrem "osmnx=1.9.4" flask

set -euo pipefail

CONDA_BIN="${CONDA_BIN:-/Users/michaelfloyd/miniforge3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-rem_env}"
PORT="${PORT:-5057}"
APP_DIR="${0:A:h}/rem_app"

if [[ "${NO_OPEN:-0}" != "1" ]]; then
  (sleep 2 && open "http://127.0.0.1:$PORT") &
fi

PORT="$PORT" exec "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python -u "$APP_DIR/app.py"
