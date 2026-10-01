#!/usr/bin/env python
"""Local web app: draw an area on a map, pick a river, generate a RiverREM.

Run inside the rem_env conda env (see tools/rem_app.sh), then open http://127.0.0.1:5057
"""
import json
import os
import subprocess
import sys
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_from_directory

import pipeline

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_MEGAPIXELS = 400  # ~1m over 20x20 km; larger runs exhaust RAM in RiverREM's interpolation

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
procs: dict[str, subprocess.Popen] = {}


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


def read_job(job_id: str) -> dict:
    path = job_dir(job_id)
    try:
        with open(os.path.join(path, "job.json")) as f:
            job = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        job = {"status": "running", "step": "starting"}
    proc = procs.get(job_id)
    if job.get("status") == "running" and (proc is None or proc.poll() is not None):
        # process exited (or server restarted) without the pipeline recording a result
        if proc is None or proc.returncode != 0:
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


@app.post("/api/trace")
def trace():
    body = request.get_json(force=True)
    start, end = parse_point(body.get("start")), parse_point(body.get("end"))
    corridor_m = parse_corridor(body.get("corridor_m", 1500))
    try:
        result = pipeline.trace_stretch(start, end, body.get("river") or None)
    except ValueError as exc:
        abort(400, str(exc))
    poly, bbox = pipeline.corridor(result["stretch"], corridor_m)
    result.update(corridor=poly, bbox=bbox, estimate=estimates(bbox))
    return jsonify(result)


@app.post("/api/corridor")
def corridor():
    """Recompute corridor + estimates for an already-traced stretch (e.g. when the width changes)."""
    body = request.get_json(force=True)
    stretch = body.get("stretch") or {}
    if stretch.get("type") != "LineString" or len(stretch.get("coordinates", [])) < 2:
        abort(400, "stretch must be a GeoJSON LineString")
    poly, bbox = pipeline.corridor(stretch, parse_corridor(body.get("corridor_m", 1500)))
    return jsonify({"corridor": poly, "bbox": bbox, "estimate": estimates(bbox)})


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
    stretch = body.get("stretch")
    if not river:
        abort(400, "Pick a river first.")
    if res not in (1, 3, 10, 30):
        abort(400, "res must be 1, 3, 10 or 30")
    if stretch:
        if stretch.get("type") != "LineString" or len(stretch.get("coordinates", [])) < 2:
            abort(400, "stretch must be a GeoJSON LineString")
        corridor_m = parse_corridor(body.get("corridor_m", 1500))
        _, bbox = pipeline.corridor(stretch, corridor_m)
    else:
        bbox = parse_bbox(",".join(str(v) for v in body.get("bbox", [])))
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
            "--res", str(res), "--cmap", cmap, "--out", out]
    if stretch:
        stretch_path = os.path.join(out, "stretch_input.geojson")
        with open(stretch_path, "w") as f:
            json.dump({"type": "Feature", "properties": {"river": river}, "geometry": stretch}, f)
        args += ["--stretch", stretch_path, "--corridor", str(corridor_m)]
        if not body.get("clip", True):
            args.append("--no-clip")
    else:
        args += ["--bbox", *map(str, bbox)]
    log = open(os.path.join(out, "run.log"), "w")
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
