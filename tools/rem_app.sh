#!/bin/zsh

# Start River REM Studio: draw an area on a map, pick a river, generate a REM.
# Opens http://127.0.0.1:5057 (override with PORT=...). Runs are saved to river_rem_runs/.
# If the app is already running on that port, just opens it; if another program has the
# port, starts on the next free one.
#
# Setup (one time):
#   mamba create -n rem_env -c conda-forge riverrem "osmnx=1.9.4" flask

set -euo pipefail

CONDA_BIN="${CONDA_BIN:-/Users/michaelfloyd/miniforge3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-rem_env}"
PORT="${PORT:-5057}"
APP_DIR="${0:A:h}/rem_app"

port_in_use() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t >/dev/null 2>&1
}

is_rem_app() {
  # Capture first: `curl | grep -q` fails under pipefail when grep exits early and curl gets SIGPIPE.
  local page
  page="$(curl -s --max-time 2 "http://127.0.0.1:$1/" || true)"
  [[ "$page" == *"<title>River REM Studio</title>"* ]]
}

open_browser() {
  [[ "${NO_OPEN:-0}" == "1" ]] || open "http://127.0.0.1:$1"
}

if port_in_use "$PORT"; then
  if is_rem_app "$PORT"; then
    print -u2 "River REM Studio is already running at http://127.0.0.1:$PORT"
    open_browser "$PORT"
    exit 0
  fi
  requested="$PORT"
  for _ in {1..20}; do
    PORT=$((PORT + 1))
    port_in_use "$PORT" || break
  done
  if port_in_use "$PORT"; then
    print -u2 "Ports $requested-$PORT are all in use. Set PORT=... to choose another."
    exit 1
  fi
  print -u2 "Port $requested is used by another program; starting on $PORT instead."
fi

if [[ "${NO_OPEN:-0}" != "1" ]]; then
  (sleep 2 && open "http://127.0.0.1:$PORT") &
fi

PORT="$PORT" exec "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python -u "$APP_DIR/app.py"
