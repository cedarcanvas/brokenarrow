#!/usr/bin/env python
"""Local web app: draw an area on a map, pick a river, generate a RiverREM.

Run inside the rem_env conda env (see tools/rem_app.sh), then open http://127.0.0.1:5057
"""
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_from_directory

import pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_MEGAPIXELS = 400  # ~1m over 20x20 km; larger runs exhaust RAM in RiverREM's interpolation

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
procs: dict[str, subprocess.Popen] = {}


class QuietPolling(logging.Filter):
    """Drop successful job-status polls (the page asks every 1.5 s); keep errors and everything else."""
    POLL = re.compile(r'"GET /api/jobs(/[^ ?"]*)? HTTP/[\d.]+" 200 ')

    def filter(self, record: logging.LogRecord) -> bool:
        return not self.POLL.search(record.getMessage())


logging.getLogger("werkzeug").addFilter(QuietPolling())


def parse_bbox(raw: str | None) -> tuple:
    try:
        w, s, e, n = (float(v) for v in (raw or "").split(","))
    except ValueError:
        abort(400, "bbox must be W,S,E,N")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        abort(400, "bbox out of range")
    return (w, s, e, n)


def job_dir(job_id: str) -> str:
    path = os.path.join(pipeline.RUNS_DIR, job_id)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(pipeline.RUNS_DIR) or not os.path.isdir(path):
        abort(404)
    return path


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)   # signal 0: existence check only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_job(job_id: str) -> dict:
    path = job_dir(job_id)
    try:
        with open(os.path.join(path, "job.json")) as f:
            job = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        job = {"status": "running", "step": "starting"}
    proc = procs.get(job_id)
    if job.get("status") == "running":
        if proc is not None:
            alive = proc.poll() is None
        else:   # started by another server or from the command line: check the pipeline's pid
            alive = pid_alive(job.get("pid"))
        if not alive:
            job.update(status="error", error=job.get("error") or "Process exited unexpectedly; see log.")
    job["id"] = job_id
    return job


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.svg", mimetype="image/svg+xml")


@app.get("/api/rivers")
def rivers():
    return jsonify(pipeline.list_rivers(parse_bbox(request.args.get("bbox"))))


def estimates(bbox: tuple) -> dict:
    return {"by_res": {str(r): pipeline.estimate(bbox, r) for r in (1, 3, 10, 30)}, "max_megapixels": MAX_MEGAPIXELS}


def parse_point(raw) -> tuple:
    try:
        lon, lat = (float(v) for v in raw)
    except (TypeError, ValueError):
        abort(400, "points must be [lon, lat]")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        abort(400, "point out of range")
    return (lon, lat)


def parse_corridor(raw) -> float:
    try:
        corridor_m = float(raw)
    except (TypeError, ValueError):
        abort(400, "corridor_m must be a number")
    if not 100 <= corridor_m <= 10000:
        abort(400, "corridor_m must be between 100 and 10000")
    return corridor_m


@app.get("/api/estimate")
def estimate():
    return jsonify(estimates(parse_bbox(request.args.get("bbox"))))


def parse_stretch(raw) -> dict:
    stretch = raw or {}
    if stretch.get("type") != "LineString" or len(stretch.get("coordinates", [])) < 2:
        abort(400, "stretch must be a GeoJSON LineString")
    return stretch


def parse_print(body: dict) -> tuple[str, str]:
    page, orientation = body.get("page", "11x17"), body.get("orientation", "auto")
    if page not in pipeline.print_spec.PAGES:
        abort(400, f"page must be one of {', '.join(pipeline.print_spec.PAGES)}")
    if orientation not in ("auto", "horizontal", "vertical"):
        abort(400, "orientation must be auto, horizontal or vertical")
    return page, orientation


def frame_for(body: dict, stretch: dict | None) -> dict:
    """Print frame for a stretch or a drawn bbox, with resolution estimates for the DEM it needs."""
    page, orientation = parse_print(body)
    if stretch:
        frame = pipeline.print_frame(page, orientation, stretch=stretch,
                                     corridor_m=parse_corridor(body.get("corridor_m", 1500)))
    else:
        frame = pipeline.print_frame(page, orientation, bbox=parse_bbox(",".join(map(str, body.get("bbox", [])))))
    frame["estimate"] = estimates(tuple(frame["bbox"]))
    return frame


# stand-in ramp top for previews when the real one is automatic (known only after the REM)
PREVIEW_TOP = {"ft": 500, "m": 150}


def parse_ramp(src) -> dict:
    """Colour ramp options from query args or a JSON body, in pipeline.ramp()'s terms."""
    style = str(src.get("ramp_style", "smooth"))
    if style not in pipeline.RAMP_STYLES:
        abort(400, f"ramp_style must be one of {', '.join(pipeline.RAMP_STYLES)}")
    units = str(src.get("ramp_units", "ft"))
    if units not in pipeline.UNITS:
        abort(400, "Units must be ft or m.")
    log = str(src.get("ramp_log", "1")).lower() not in ("0", "false", "no")
    try:
        steps = int(src.get("ramp_steps", 12))
    except (TypeError, ValueError):
        abort(400, "Steps must be a whole number.")
    if not 2 <= steps <= 30:
        abort(400, "Steps must be between 2 and 30.")

    def height(key: str, label: str, low: float, high: float) -> float | None:
        raw = src.get(key)
        if raw in (None, "", "auto"):
            return None
        try:
            value = float(str(raw).replace(",", ""))
        except ValueError:
            abort(400, f"{label} must be a number of {pipeline.UNIT_NAMES[units]}, or blank for auto.")
        if not low <= value <= high:
            abort(400, f"{label} must be between {low:g} and {high:,g} {units}.")
        return value

    top = height("ramp_top", "Top of ramp", 1, 20000)
    first = height("ramp_first", "Lowest band", 0.1, 5000)
    if first is not None and top is not None and first >= top:
        abort(400, "Lowest band must be smaller than the top of the ramp.")
    invert = str(src.get("ramp_invert", "0")).lower() in ("1", "true", "yes")
    return {"style": style, "log": log, "steps": steps, "top": top, "units": units, "first": first, "invert": invert}


def parse_labels(src) -> dict:
    color, scope = str(src.get("label_color", "auto")), str(src.get("label_scope", "river"))
    if color not in ("auto", "white", "black", "off"):
        abort(400, "River labels must be auto, white, black or off.")
    if scope not in ("river", "all"):
        abort(400, "Label scope must be river or all.")
    places = str(src.get("places", "1")).lower() not in ("0", "false", "no")
    return {"color": color, "scope": scope, "places": places}


def label_cli_args(opts: dict) -> list[str]:
    return ["--labels", opts["color"], "--label-scope", opts["scope"],
            "--places" if opts.get("places", True) else "--no-places"]


def ramp_cli_args(opts: dict) -> list[str]:
    return ["--ramp", opts["style"], "--spacing", "log" if opts["log"] else "linear", "--steps", str(opts["steps"]),
            "--units", opts["units"], "--top", str(opts["top"] or "auto"), "--first", str(opts["first"] or "auto"),
            "--invert" if opts["invert"] else "--no-invert"]


@app.get("/api/ramp")
def ramp_preview():
    """Legend for a colour ramp, so the page can preview it before running."""
    opts = parse_ramp(request.args)
    try:
        legend = pipeline.ramp(request.args.get("cmap", "mako"), opts["top"] or PREVIEW_TOP[opts["units"]],
                               opts["style"], opts["log"], opts["steps"], opts["units"], opts["first"],
                               opts["invert"])["legend"]
    except ValueError as exc:
        abort(400, str(exc))
    legend["auto_top"] = opts["top"] is None
    legend["preview_top"] = PREVIEW_TOP[opts["units"]]
    return jsonify(legend)


@app.get("/api/pages")
def pages():
    return jsonify([{"id": k, "label": pipeline.print_spec.PAGE_LABELS[k]} for k in pipeline.print_spec.PAGES])


@app.post("/api/trace")
def trace():
    body = request.get_json(force=True)
    start, end = parse_point(body.get("start")), parse_point(body.get("end"))
    try:
        result = pipeline.trace_stretch(start, end, body.get("river") or None)
    except ValueError as exc:
        abort(400, str(exc))
    result["frame"] = frame_for(body, result["stretch"])
    return jsonify(result)


@app.post("/api/frame")
def frame():
    """Recompute the print frame (e.g. when page, orientation or corridor width changes)."""
    body = request.get_json(force=True)
    stretch = parse_stretch(body["stretch"]) if body.get("stretch") else None
    return jsonify(frame_for(body, stretch))


@app.get("/api/jobs")
def list_jobs():
    if not os.path.isdir(pipeline.RUNS_DIR):
        return jsonify([])
    ids = sorted((d for d in os.listdir(pipeline.RUNS_DIR)
                  if os.path.isfile(os.path.join(pipeline.RUNS_DIR, d, "job.json"))), reverse=True)
    return jsonify([read_job(i) for i in ids[:50]])


@app.post("/api/jobs")
def create_job():
    body = request.get_json(force=True)
    river = str(body.get("river", "")).strip()
    res = float(body.get("res", 10))
    cmap = str(body.get("cmap", "mako"))
    stretch = parse_stretch(body["stretch"]) if body.get("stretch") else None
    title = str(body.get("title") or river).strip()[:120]
    subtitle = str(body.get("subtitle", "River Relative Elevation Model")).strip()[:160]
    if not river:
        abort(400, "Pick a river first.")
    if res not in (1, 3, 10, 30):
        abort(400, "res must be 1, 3, 10 or 30")
    page, orientation = parse_print(body)
    ramp_opts = parse_ramp(body)
    label_opts = parse_labels(body)
    frame = frame_for(body, stretch)
    bbox = tuple(frame["bbox"])
    if any(p.poll() is None for p in procs.values()):
        abort(409, "A REM is already running. Wait for it to finish.")
    mp = pipeline.estimate(bbox, res)["megapixels"]
    if mp > MAX_MEGAPIXELS:
        abort(400, f"{mp} MP is too large at {res:g} m (limit {MAX_MEGAPIXELS}). Use a coarser resolution or smaller area.")

    slug = "".join(c if c.isalnum() else "-" for c in river.lower()).strip("-")
    job_id = f"{datetime.now():%Y%m%d-%H%M%S}_{slug}_{res:g}m"
    out = os.path.join(pipeline.RUNS_DIR, job_id)
    os.makedirs(out, exist_ok=True)
    args = [sys.executable, "-u", os.path.join(HERE, "pipeline.py"), "run", "--river", river,
            "--res", str(res), "--cmap", cmap, "--out", out, "--page", page, "--orientation", orientation,
            "--title", title, "--subtitle", subtitle, *ramp_cli_args(ramp_opts), *label_cli_args(label_opts)]
    if stretch:
        stretch_path = os.path.join(out, "stretch_input.geojson")
        with open(stretch_path, "w") as f:
            json.dump({"type": "Feature", "properties": {"river": river}, "geometry": stretch}, f)
        args += ["--stretch", stretch_path, "--corridor", str(parse_corridor(body.get("corridor_m", 1500)))]
    else:
        args += ["--bbox", *map(str, parse_bbox(",".join(map(str, body.get("bbox", [])))))]
    log = open(os.path.join(out, "run.log"), "w")
    procs[job_id] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=out)
    return jsonify({"id": job_id}), 202


@app.post("/api/jobs/<job_id>/restyle")
def restyle_job(job_id):
    """Recolour a finished run (new ramp, colours or title) without downloading or running RiverREM again."""
    out = job_dir(job_id)
    job = read_job(job_id)
    if job.get("status") == "running":
        abort(409, "This run is still in progress.")
    if not (job.get("files") or {}).get("rem"):
        abort(400, "This run has no saved REM to recolour. Generate it again instead.")
    if any(p.poll() is None for p in procs.values()):
        abort(409, "Another job is running. Wait for it to finish.")
    body = request.get_json(force=True)
    ramp_opts = parse_ramp(body)
    args = [sys.executable, "-u", os.path.join(HERE, "pipeline.py"), "restyle", "--run", out,
            "--cmap", str(body.get("cmap", job.get("cmap", "mako"))), *ramp_cli_args(ramp_opts),
            *label_cli_args(parse_labels(body))]
    for key in ("title", "subtitle"):
        if body.get(key) is not None:
            args += [f"--{key}", str(body[key]).strip()[:160]]
    # mark it running now: the pipeline takes a few seconds to start, and the page would otherwise
    # read the old "done" status and stop watching straight away
    manifest_path = os.path.join(out, "job.json")
    with open(manifest_path) as f:
        manifest = json.load(f)
    manifest.update(status="running", step="viz", error=None)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    log = open(os.path.join(out, "run.log"), "a")
    log.write(f"\n--- recolour {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
    log.flush()
    procs[job_id] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, cwd=out)
    return jsonify({"id": job_id}), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    job = read_job(job_id)
    log_path = os.path.join(job_dir(job_id), "run.log")
    if os.path.exists(log_path):
        with open(log_path, errors="replace") as f:
            lines = [l.rstrip() for l in f.read().replace("\r", "\n").splitlines() if l.strip()]
        job["log"] = [l for l in lines if "UserWarning" not in l and "river_length =" not in l][-40:]
    return jsonify(job)


@app.get("/runs/<job_id>/<path:filename>")
def run_file(job_id, filename):
    return send_from_directory(job_dir(job_id), filename, as_attachment=request.args.get("dl") == "1")


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
def err(e):
    return jsonify({"error": e.description}), e.code


@app.errorhandler(pipeline.ServiceUnavailable)
def service_down(e):
    return jsonify({"error": str(e)}), 503


if __name__ == "__main__":
    os.makedirs(pipeline.RUNS_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", 5057))
    print(f"RiverREM app: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
