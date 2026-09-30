#!/usr/bin/env python
"""River REM workflow: bbox + river name -> 3DEP DEM -> NHD (or OSM) centerline -> RiverREM.

Each run writes to its own folder with a job.json manifest the app reads.

Usage (run inside the rem_env conda env):
    pipeline.py rivers --bbox W S E N
    pipeline.py run --bbox W S E N --river "Arkansas River" [--res 10] [--cmap mako] [--out DIR]
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

import geopandas as gpd
import rasterio
from rasterio.merge import merge
from rasterio.warp import transform_bounds
from shapely.geometry import LineString

NHD_FLOWLINES = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3/query"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
DEM_3DEP = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
USER_AGENT = "RiverREM-Studio/1.0 (ridgelinemaps)"
TILE_PX = 4000  # 3DEP caps requests at 8000 px; smaller tiles fail less often
NODATA = -9999.0
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RUNS_DIR = os.path.join(REPO_ROOT, "river_rem_runs")
CACHE_DIR = os.path.join(REPO_ROOT, ".osm_cache", "rem_app")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def http_get(url: str, params: dict | None = None, timeout: int = 120, retries: int = 3,
             data: dict | None = None) -> bytes:
    """GET (or POST form `data`) with retries on HTTP 5xx. Timeouts are not retried."""
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(full, data=body, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (TimeoutError, socket.timeout):
            raise
        except Exception as exc:  # 502/504s from USGS services are common
            if attempt == retries:
                raise
            log(f"  request failed ({exc}); retrying {attempt}/{retries - 1}")
            time.sleep(2 * attempt)


def utm_epsg(lon: float, lat: float) -> int:
    """NAD83 / UTM zone EPSG for a point (CONUS: 26910-26919)."""
    zone = int((lon + 180) // 6) + 1
    return 26900 + zone if lat >= 0 else 32700 + zone


def bbox_epsg(bbox: tuple) -> int:
    w, s, e, n = bbox
    return utm_epsg((w + e) / 2, (s + n) / 2)


def estimate(bbox: tuple, res_m: float) -> dict:
    epsg = bbox_epsg(bbox)
    xmin, ymin, xmax, ymax = transform_bounds("EPSG:4326", f"EPSG:{epsg}", *bbox, densify_pts=21)
    w, h = math.ceil((xmax - xmin) / res_m), math.ceil((ymax - ymin) / res_m)
    return {"epsg": epsg, "width": w, "height": h, "megapixels": round(w * h / 1e6, 1),
            "width_km": round((xmax - xmin) / 1000, 2), "height_km": round((ymax - ymin) / 1000, 2)}


# ---------------------------------------------------------------- river lines (NHD, OSM fallback)

def _nhd_lines(bbox: tuple) -> list:
    """Named NHD HighRes flowlines intersecting bbox as GeoJSON features (EPSG:4326)."""
    w, s, e, n = bbox
    feats, offset = [], 0
    while True:
        params = {
            "where": "gnis_name IS NOT NULL",
            "geometry": json.dumps({"xmin": w, "ymin": s, "xmax": e, "ymax": n}),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "gnis_name",
            "returnGeometry": "true",
            "outSR": 4326,
            "geometryPrecision": 6,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": 2000,
        }
        gj = json.loads(http_get(NHD_FLOWLINES, params, timeout=30, retries=2))
        if "error" in gj:
            raise RuntimeError(f"NHD error: {gj['error']}")
        page = gj.get("features", [])
        feats.extend(page)
        more = gj.get("exceededTransferLimit") or gj.get("properties", {}).get("exceededTransferLimit")
        if not more or not page:
            return feats
        offset += len(page)


def _osm_lines(bbox: tuple) -> list:
    """Named OSM waterways intersecting bbox as GeoJSON features (EPSG:4326)."""
    w, s, e, n = bbox
    query = (f'[out:json][timeout:90];way["waterway"~"^(river|stream|canal|ditch|drain)$"]["name"]'
             f"({s},{w},{n},{e});out geom;")
    last_exc = None
    for endpoint in OVERPASS:
        try:
            data = json.loads(http_get(endpoint, data={"data": query}, timeout=100, retries=2))
            break
        except Exception as exc:
            last_exc = exc
            log(f"  Overpass {endpoint} failed ({exc})")
    else:
        raise RuntimeError(f"OpenStreetMap Overpass unavailable: {last_exc}")
    return [{"type": "Feature", "properties": {"gnis_name": el["tags"]["name"]},
             "geometry": LineString([(p["lon"], p["lat"]) for p in el["geometry"]]).__geo_interface__}
            for el in data.get("elements", []) if len(el.get("geometry", [])) >= 2]


def named_lines(bbox: tuple) -> tuple[gpd.GeoDataFrame, str]:
    """Named river lines in bbox from NHD, falling back to OSM. Cached per bbox. Returns (gdf, source)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha1(",".join(f"{v:.5f}" for v in bbox).encode()).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(path):
        with open(path) as f:
            cached = json.load(f)
        feats, source = cached["features"], cached["source"]
    else:
        try:
            log("Querying USGS NHD flowlines")
            feats, source = _nhd_lines(bbox), "NHD"
        except Exception as exc:
            log(f"NHD unavailable ({exc}); falling back to OpenStreetMap")
            feats, source = _osm_lines(bbox), "OSM"
        with open(path, "w") as f:
            json.dump({"source": source, "features": feats}, f)
    if not feats:
        return gpd.GeoDataFrame({"gnis_name": []}, geometry=[], crs="EPSG:4326"), source
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")[["gnis_name", "geometry"]], source


def list_rivers(bbox: tuple) -> dict:
    """GeoJSON of named rivers (dissolved per name, clipped to bbox) plus a summary list."""
    gdf, source = named_lines(bbox)
    empty = {"rivers": [], "source": source, "geojson": {"type": "FeatureCollection", "features": []}}
    if gdf.empty:
        return empty
    gdf = gdf.clip(bbox)
    gdf = gdf[~gdf.geometry.is_empty]
    if gdf.empty:
        return empty
    gdf["len_m"] = gdf.to_crs(bbox_epsg(bbox)).length
    grouped = gdf.dissolve(by="gnis_name", aggfunc={"len_m": "sum"}).reset_index()
    grouped = grouped.sort_values("len_m", ascending=False)
    grouped["length_km"] = (grouped["len_m"] / 1000).round(2)
    rivers = [{"name": r.gnis_name, "length_km": r.length_km} for r in grouped.itertuples()]
    geojson = json.loads(grouped[["gnis_name", "length_km", "geometry"]].to_json())
    return {"rivers": rivers, "source": source, "geojson": geojson}


# ---------------------------------------------------------------- 3DEP

def fetch_dem(bbox: tuple, res_m: float, out_tif: str) -> str:
    """Download 3DEP elevation for bbox at res_m, in the bbox's UTM zone, tiling as needed."""
    epsg = bbox_epsg(bbox)
    xmin, ymin, xmax, ymax = transform_bounds("EPSG:4326", f"EPSG:{epsg}", *bbox, densify_pts=21)
    width, height = math.ceil((xmax - xmin) / res_m), math.ceil((ymax - ymin) / res_m)
    xmax, ymin = xmin + width * res_m, ymax - height * res_m  # snap to whole pixels
    nx, ny = math.ceil(width / TILE_PX), math.ceil(height / TILE_PX)
    log(f"DEM: {width}x{height} px at {res_m} m, EPSG:{epsg}, {nx * ny} tile(s)")

    tile_dir = os.path.join(os.path.dirname(out_tif), ".tiles")
    os.makedirs(tile_dir, exist_ok=True)
    tiles = []
    for j in range(ny):
        for i in range(nx):
            tx0 = xmin + i * TILE_PX * res_m
            ty1 = ymax - j * TILE_PX * res_m
            tw = min(TILE_PX, width - i * TILE_PX)
            th = min(TILE_PX, height - j * TILE_PX)
            tbox = (tx0, ty1 - th * res_m, tx0 + tw * res_m, ty1)
            params = {
                "bbox": ",".join(f"{v:.3f}" for v in tbox),
                "bboxSR": epsg,
                "imageSR": epsg,
                "size": f"{tw},{th}",
                "format": "tiff",
                "pixelType": "F32",
                "noData": NODATA,
                "interpolation": "RSP_BilinearInterpolation",
                "f": "image",
            }
            n = j * nx + i + 1
            log(f"  tile {n}/{nx * ny} ({tw}x{th})")
            data = http_get(DEM_3DEP, params, timeout=300)
            if not data.startswith((b"II*\x00", b"MM\x00*")):
                raise RuntimeError(f"3DEP returned non-TIFF data: {data[:200]!r}")
            path = os.path.join(tile_dir, f"tile_{j}_{i}.tif")
            with open(path, "wb") as f:
                f.write(data)
            tiles.append(path)

    srcs = [rasterio.open(p) for p in tiles]
    try:
        mosaic, transform = merge(srcs, bounds=(xmin, ymin, xmax, ymax), res=res_m, nodata=NODATA)
        profile = {"driver": "GTiff", "height": mosaic.shape[1], "width": mosaic.shape[2], "count": 1,
                   "dtype": "float32", "crs": f"EPSG:{epsg}", "transform": transform, "nodata": NODATA,
                   "compress": "deflate", "tiled": True, "BIGTIFF": "IF_SAFER"}
        with rasterio.open(out_tif, "w", **profile) as dst:
            dst.write(mosaic)
    finally:
        for s in srcs:
            s.close()
    shutil.rmtree(tile_dir, ignore_errors=True)
    log(f"DEM saved: {out_tif}")
    return out_tif


# ---------------------------------------------------------------- RiverREM

def make_preview(viz_tif: str, out_png: str, max_px: int = 2048) -> list:
    """Warp the RiverREM visualization to EPSG:4326 PNG for web overlay; returns [[S, W], [N, E]]."""
    from osgeo import gdal
    gdal.UseExceptions()
    src = gdal.Open(viz_tif)
    scale = max(src.RasterXSize, src.RasterYSize) / max_px
    size = (int(src.RasterXSize / max(scale, 1)), int(src.RasterYSize / max(scale, 1)))
    warped = gdal.Warp("/vsimem/preview.tif", src, dstSRS="EPSG:4326", width=size[0], height=size[1],
                       resampleAlg="bilinear", dstAlpha=True)
    gt = warped.GetGeoTransform()
    w, n = gt[0], gt[3]
    e, s = w + gt[1] * warped.RasterXSize, n + gt[5] * warped.RasterYSize
    gdal.Translate(out_png, warped, format="PNG")
    warped = None
    gdal.Unlink("/vsimem/preview.tif")
    return [[s, w], [n, e]]


def run(bbox: tuple, river: str, res_m: float, cmap: str, out_dir: str | None = None) -> dict:
    from riverrem.REMMaker import REMMaker

    slug = "".join(c if c.isalnum() else "-" for c in river.lower()).strip("-")
    out_dir = out_dir or os.path.join(RUNS_DIR, f"{datetime.now():%Y%m%d-%H%M%S}_{slug}_{res_m:g}m")
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "job.json")
    manifest = {"bbox": list(bbox), "river": river, "res_m": res_m, "cmap": cmap,
                "status": "running", "started": datetime.now().isoformat(timespec="seconds")}

    def save(**updates):
        manifest.update(updates)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    save(step="centerline")
    try:
        log(f"Getting centerline for {river!r}")
        lines, source = named_lines(bbox)
        lines = lines[lines["gnis_name"] == river]
        if lines.empty:
            raise RuntimeError(f"No {source} river lines named {river!r} in this area.")
        epsg = bbox_epsg(bbox)
        centerline = os.path.join(out_dir, "centerline.gpkg")
        lines.to_crs(epsg).to_file(centerline, driver="GPKG")
        save(centerline_source=source)
        log(f"  {source}: {len(lines)} segments, {lines.to_crs(epsg).length.sum() / 1000:.1f} km")

        save(step="dem")
        dem = fetch_dem(bbox, res_m, os.path.join(out_dir, f"dem_{res_m:g}m.tif"))

        save(step="rem")
        log("Running RiverREM")
        rem = REMMaker(dem=dem, centerline_shp=centerline, out_dir=out_dir,
                       cache_dir=os.path.join(out_dir, ".cache"))
        rem_tif = rem.make_rem()
        save(step="viz")
        viz_tif = rem.make_rem_viz(cmap=cmap, make_png=True)

        save(step="preview")
        preview = os.path.join(out_dir, "preview_4326.png")
        bounds = make_preview(viz_tif, preview)
        shutil.rmtree(os.path.join(out_dir, ".cache"), ignore_errors=True)
        save(status="done", step="done", finished=datetime.now().isoformat(timespec="seconds"),
             bounds=bounds, files={"rem": os.path.basename(rem_tif), "viz": os.path.basename(viz_tif),
                                   "viz_png": os.path.basename(viz_tif).replace(".tif", ".png"),
                                   "dem": os.path.basename(dem), "preview": os.path.basename(preview)})
        log(f"Done: {out_dir}")
    except Exception as exc:
        save(status="error", error=str(exc))
        log(f"ERROR: {exc}")
        raise
    return manifest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rivers", help="list named rivers in a bbox")
    r.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    g = sub.add_parser("run", help="generate a REM")
    g.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    g.add_argument("--river", required=True, help="NHD GNIS name, e.g. 'Arkansas River'")
    g.add_argument("--res", type=float, default=10, help="DEM resolution in metres (1, 3, 10, 30)")
    g.add_argument("--cmap", default="mako")
    g.add_argument("--out", default=None)
    args = p.parse_args()

    bbox = tuple(args.bbox)
    if args.cmd == "rivers":
        result = list_rivers(bbox)
        print(f"Source: {result['source']}")
        for rv in result["rivers"]:
            print(f"{rv['length_km']:8.2f} km  {rv['name']}")
        return 0
    run(bbox, args.river, args.res, args.cmap, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
