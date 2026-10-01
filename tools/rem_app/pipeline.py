#!/usr/bin/env python
"""River REM workflow: bbox + river name -> 3DEP DEM -> NHD (or OSM) centerline -> RiverREM.

Each run writes to its own folder with a job.json manifest the app reads.

Usage (run inside the rem_env conda env):
    pipeline.py rivers --bbox W S E N
    pipeline.py run --bbox W S E N --river "Arkansas River" [--res 10] [--cmap mako] [--out DIR]
    pipeline.py trace --start LON LAT --end LON LAT
    pipeline.py run --start LON LAT --end LON LAT [--corridor 1500] [--res 10] [--ramp stepped --units m --first 2]
        [--page 11x17] [--orientation auto|horizontal|vertical] [--title T] [--subtitle S] [--no-print]
    pipeline.py restyle --run river_rem_runs/<run> [--cmap mako] [--ramp stepped] [--spacing log] [--first 2]
"""
import argparse
import hashlib
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import geopandas as gpd
import rasterio
from rasterio.merge import merge
from rasterio.warp import transform_bounds
from shapely.geometry import LineString, box

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import print_spec  # noqa: E402  (shared with the QGIS layout script)

NHD_FLOWLINES = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3/query"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.openstreetmap.fr/api/interpreter"]
DEM_3DEP = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
USER_AGENT = "RiverREM-Studio/1.0 (ridgelinemaps)"
TILE_PX = 2000      # 3DEP caps requests at 8000 px, but 4000 px tiles at 1 m time out (HTTP 500)
MIN_TILE_PX = 250   # failing tiles are split down to this size before giving up
NODATA = -9999.0
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RUNS_DIR = os.path.join(REPO_ROOT, "river_rem_runs")
CACHE_DIR = os.path.join(REPO_ROOT, ".osm_cache", "rem_app")


class ServiceUnavailable(RuntimeError):
    """Every upstream data source failed; worth retrying later."""


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


BUSY_CODES = (502, 503, 504)   # gateway/overload responses: the server is busy, not failing on this request
BUSY_MAX_WAIT = 60             # seconds; cap for one busy back-off


def http_get(url: str, params: dict | None = None, timeout: int = 120, retries: int = 3,
             data: dict | None = None, busy_retries: int = 0) -> bytes:
    """GET (or POST form `data`) with retries. Timeouts are not retried.

    Errors are retried up to `retries` attempts in total with short pauses. Overload responses
    (502/503/504) first get up to `busy_retries` extra attempts with exponential back-off
    (5, 10, 20, 40, 60 s, with jitter) so a busy service gets breathing room instead of more load.
    """
    full = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(full, data=body, headers={"User-Agent": USER_AGENT})
    attempt = busy = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (TimeoutError, socket.timeout):
            raise
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in BUSY_CODES and busy < busy_retries:
                busy += 1
                delay = min(BUSY_MAX_WAIT, 5 * 2 ** (busy - 1)) * random.uniform(0.8, 1.2)
                log(f"  service busy (HTTP {exc.code}); waiting {delay:.0f} s before retry {busy}/{busy_retries}")
                time.sleep(delay)
                continue
            attempt += 1
            if attempt >= retries:
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


def _cached_lines(bbox: tuple) -> tuple[list, str] | None:
    """Features from any cached query whose bbox contains this one (NHD preferred over OSM)."""
    w, s, e, n = bbox
    hits = []
    for name in os.listdir(CACHE_DIR):
        try:
            with open(os.path.join(CACHE_DIR, name)) as f:
                cached = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        cw, cs, ce, cn = cached.get("bbox", (0, 0, 0, 0))
        if cw <= w and cs <= s and ce >= e and cn >= n:
            hits.append(cached)
    if not hits:
        return None
    best = min(hits, key=lambda c: (c["source"] != "NHD", (c["bbox"][2] - c["bbox"][0]) * (c["bbox"][3] - c["bbox"][1])))
    return best["features"], best["source"]


def named_lines(bbox: tuple) -> tuple[gpd.GeoDataFrame, str]:
    """Named river lines intersecting bbox from NHD, falling back to OSM. Cached. Returns (gdf, source)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    hit = _cached_lines(bbox)
    if hit:
        feats, source = hit
    else:
        try:
            log("Querying USGS NHD flowlines")
            feats, source = _nhd_lines(bbox), "NHD"
        except Exception as exc:
            log(f"NHD unavailable ({exc}); falling back to OpenStreetMap")
            try:
                feats, source = _osm_lines(bbox), "OSM"
            except Exception as osm_exc:
                raise ServiceUnavailable("Couldn't load river lines: USGS NHD and OpenStreetMap are both "
                                         "unavailable right now. Try again in a minute.") from osm_exc
        key = hashlib.sha1(",".join(f"{v:.5f}" for v in bbox).encode()).hexdigest()[:16]
        with open(os.path.join(CACHE_DIR, f"{key}.json"), "w") as f:
            json.dump({"bbox": list(bbox), "source": source, "features": feats}, f)
    if not feats:
        return gpd.GeoDataFrame({"gnis_name": []}, geometry=[], crs="EPSG:4326"), source
    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")[["gnis_name", "geometry"]]
    return gdf[gdf.intersects(box(*bbox))], source


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


# ---------------------------------------------------------------- stretch (start/end points)

SNAP_M = 500     # clicks farther than this from any named river are rejected
BRIDGE_M = 75    # join line ends closer than this (NHD/OSM gaps at confluences, bridges)


def _river_graph(lines_utm: gpd.GeoSeries):
    """Graph of line vertices (UTM metres) weighted by segment length, with small gaps bridged."""
    import networkx as nx
    from scipy.spatial import cKDTree

    g = nx.Graph()
    for geom in lines_utm.explode(index_parts=False):
        pts = [(round(x, 1), round(y, 1)) for x, y in geom.coords]
        for a, b in zip(pts, pts[1:]):
            if a != b:
                g.add_edge(a, b, weight=math.dist(a, b))
    ends = [n for n, d in g.degree() if d == 1]
    if len(ends) > 1:
        for i, j in cKDTree(ends).query_pairs(BRIDGE_M):
            g.add_edge(ends[i], ends[j], weight=math.dist(ends[i], ends[j]))
    return g


def trace_stretch(start: tuple, end: tuple, river: str | None = None) -> dict:
    """Follow the named river between two clicked (lon, lat) points.

    Picks the river closest to both clicks (or `river` if given) and returns the path along it.
    """
    import networkx as nx
    from pyproj import Transformer
    from scipy.spatial import cKDTree
    from shapely.geometry import Point
    from shapely.ops import transform as shp_transform

    (x1, y1), (x2, y2) = start, end
    # generous padding so pin nudges and wider corridors reuse the cached lines
    pad = max(0.04, 0.25 * max(abs(x2 - x1), abs(y2 - y1)))
    qbbox = (round(min(x1, x2) - pad, 3), round(min(y1, y2) - pad, 3),
             round(max(x1, x2) + pad, 3), round(max(y1, y2) + pad, 3))
    lines, source = named_lines(qbbox)
    if river:
        lines = lines[lines["gnis_name"] == river]
    if lines.empty:
        raise ValueError("No named rivers near those points.")

    epsg = utm_epsg((x1 + x2) / 2, (y1 + y2) / 2)
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)
    p1, p2 = Point(to_utm.transform(x1, y1)), Point(to_utm.transform(x2, y2))
    lines_utm = lines.to_crs(epsg)

    by_name = lines_utm.groupby("gnis_name").geometry
    dists = {name: (geoms.distance(p1).min(), geoms.distance(p2).min()) for name, geoms in by_name}
    name, (d1, d2) = min(dists.items(), key=lambda kv: max(kv[1]))
    if max(d1, d2) > SNAP_M:
        near_a = min(dists.items(), key=lambda kv: kv[1][0])
        near_b = min(dists.items(), key=lambda kv: kv[1][1])
        far = [f"{label} is {d:.0f} m from the nearest named river ({river_name})"
               for label, (river_name, d) in (("A", (near_a[0], near_a[1][0])), ("B", (near_b[0], near_b[1][1])))
               if d > SNAP_M]
        if far:
            raise ValueError(f"Place both pins within {SNAP_M} m of a river: " + "; ".join(far) + ".")
        raise ValueError(f"The pins are on different rivers (A: {near_a[0]}, B: {near_b[0]}). "
                         "Put both on the same river.")

    g = _river_graph(lines_utm[lines_utm["gnis_name"] == name].geometry)
    nodes = list(g.nodes)
    tree = cKDTree(nodes)
    n1, n2 = nodes[tree.query(p1.coords[0])[1]], nodes[tree.query(p2.coords[0])[1]]
    try:
        path = nx.shortest_path(g, n1, n2, weight="weight")
    except nx.NetworkXNoPath:
        raise ValueError(f"The {source} lines for {name} have a gap between your points. "
                         "Move the points closer together or use box mode.")
    if len(path) < 2:
        raise ValueError("Start and end snap to the same spot. Pick points farther apart.")

    stretch_utm = LineString(path)
    stretch = shp_transform(to_ll.transform, stretch_utm)
    return {"river": name, "source": source, "length_km": round(stretch_utm.length / 1000, 2),
            "snap_m": [round(d1), round(d2)], "stretch": stretch.__geo_interface__}


def _norm_deg(a: float) -> float:
    """Normalise an axis angle to (-90, 90]; a line at 100° is the same axis as one at -80°."""
    a = (a + 90) % 180 - 90
    return 90.0 if a == -90 else a


def print_frame(page: str = "11x17", orientation: str = "auto", stretch: dict | None = None,
                bbox: tuple | None = None, corridor_m: float = 1500) -> dict:
    """The rotated rectangle that a print page shows, at a nice scale.

    Stretch mode turns the map so the river's main axis runs horizontally (landscape page) or
    vertically (portrait); "auto" picks whichever needs less rotation from north-up. The frame
    covers the stretch plus corridor_m each side, grown to the page's map aspect. Box mode keeps
    north up and grows the drawn box to the page aspect.

    Returns the frame geometry (UTM and EPSG:4326), the map rotation for QGIS, the scale, and
    the north-up bbox the DEM must cover.
    """
    import numpy as np
    from pyproj import Transformer

    if stretch:
        lonlat = np.array(stretch["coordinates"], dtype=float)
    elif bbox:
        w, s, e, n = bbox
        lonlat = np.array([(w, s), (e, s), (e, n), (w, n)], dtype=float)
    else:
        raise ValueError("print_frame needs a stretch or a bbox")
    if page not in print_spec.PAGES:
        raise ValueError(f"page must be one of {', '.join(print_spec.PAGES)}")
    if orientation not in ("auto", "horizontal", "vertical"):
        raise ValueError("orientation must be auto, horizontal or vertical")

    lon_c, lat_c = lonlat.mean(axis=0)
    epsg = utm_epsg(lon_c, lat_c)
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    to_ll = Transformer.from_crs(epsg, 4326, always_xy=True)
    pts = np.column_stack(to_utm.transform(lonlat[:, 0], lonlat[:, 1]))
    origin = pts.mean(axis=0)

    if stretch:
        # principal axis of the stretch (robust to meanders, unlike start->end)
        _, _, vt = np.linalg.svd(pts - origin, full_matrices=False)
        axis = math.degrees(math.atan2(vt[0][1], vt[0][0]))
        phi_h, phi_v = _norm_deg(axis), _norm_deg(axis - 90)
        if orientation == "auto":
            orientation = "horizontal" if abs(phi_h) <= abs(phi_v) else "vertical"
        phi = phi_h if orientation == "horizontal" else phi_v
        pad_across = corridor_m
    else:
        span_x, span_y = np.ptp(pts[:, 0]), np.ptp(pts[:, 1])
        if orientation == "auto":
            orientation = "horizontal" if span_x >= span_y else "vertical"
        phi, pad_across = 0.0, 0.0
    landscape = orientation == "horizontal"

    # page axes in world coordinates: x along phi, y perpendicular (up)
    ux = np.array([math.cos(math.radians(phi)), math.sin(math.radians(phi))])
    vy = np.array([-ux[1], ux[0]])
    rel = pts - origin
    u, v = rel @ ux, rel @ vy
    # the river runs along page x when horizontal, along page y when vertical
    along, across = (u, v) if landscape else (v, u)
    pad_along = 0.03 * np.ptp(along) if stretch else 0.0
    pad_u, pad_v = (pad_along, pad_across) if landscape else (pad_across, pad_along)
    u0, u1 = u.min() - pad_u, u.max() + pad_u
    v0, v1 = v.min() - pad_v, v.max() + pad_v

    map_w, map_h = print_spec.map_size(page, landscape)
    scale = print_spec.nice_scale(max((u1 - u0) / map_w, (v1 - v0) / map_h) * 1000)
    width_m, height_m = map_w * scale / 1000, map_h * scale / 1000
    center = origin + (u0 + u1) / 2 * ux + (v0 + v1) / 2 * vy
    corners = [center + sx * width_m / 2 * ux + sy * height_m / 2 * vy
               for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]

    # north-up area the DEM must cover, with a small margin so resampling has data at the edges
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    margin = 0.01 * max(width_m, height_m) + 30
    dem_utm = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    dem_bbox = transform_bounds(f"EPSG:{epsg}", "EPSG:4326", *dem_utm, densify_pts=21)
    ring = [list(to_ll.transform(*c)) for c in corners]
    return {
        "page": page,
        "orientation": orientation,
        "landscape": landscape,
        "epsg": epsg,
        "rotation": round(phi, 3),          # QGIS map rotation (content clockwise, degrees)
        "scale": scale,
        "center": [round(float(center[0]), 2), round(float(center[1]), 2)],
        "width_m": round(width_m, 1),
        "height_m": round(height_m, 1),
        "frame": {"type": "Polygon", "coordinates": [ring + [ring[0]]]},
        "bbox": [round(v, 6) for v in dem_bbox],
    }


# ---------------------------------------------------------------- 3DEP

def _fetch_tile(x0: float, y1: float, w: int, h: int, res_m: float, epsg: int, stem: str) -> list[str]:
    """Download one 3DEP tile (top-left x0, y1).

    Two kinds of failure need opposite responses:
    - HTTP 500 or a timeout: this request is too big (the ImageServer gives up after ~25 s,
      e.g. 4000 px tiles at 1 m). Split into quarters and retry; smaller requests succeed.
    - HTTP 502/503/504: the service is overloaded. More, smaller requests only add load, so
      http_get backs off and retries the same tile; if it stays busy, stop and say so.
    """
    params = {
        "bbox": f"{x0:.3f},{y1 - h * res_m:.3f},{x0 + w * res_m:.3f},{y1:.3f}",
        "bboxSR": epsg,
        "imageSR": epsg,
        "size": f"{w},{h}",
        "format": "tiff",
        "pixelType": "F32",
        "noData": NODATA,
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }
    try:
        data = http_get(DEM_3DEP, params, timeout=300, retries=2, busy_retries=5)
    except (urllib.error.HTTPError, TimeoutError, socket.timeout) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code in BUSY_CODES:
            raise ServiceUnavailable(
                f"The USGS 3DEP elevation service is overloaded (HTTP {exc.code}) and didn't recover after "
                "backing off for about 2 minutes. Try again in a few minutes, or use a coarser resolution.") from exc
        if max(w, h) <= MIN_TILE_PX:
            raise
        log(f"    {w}x{h} tile failed ({exc}); splitting into quarters")
        hw, hh = math.ceil(w / 2), math.ceil(h / 2)
        parts = []
        for k, (dx, dy, sw, sh) in enumerate([(0, 0, hw, hh), (hw, 0, w - hw, hh),
                                               (0, hh, hw, h - hh), (hw, hh, w - hw, h - hh)]):
            if sw > 0 and sh > 0:
                parts += _fetch_tile(x0 + dx * res_m, y1 - dy * res_m, sw, sh, res_m, epsg, f"{stem}{k}")
        return parts
    if not data.startswith((b"II*\x00", b"MM\x00*")):
        raise RuntimeError(f"3DEP returned non-TIFF data: {data[:200]!r}")
    path = f"{stem}.tif"
    with open(path, "wb") as f:
        f.write(data)
    return [path]


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
            log(f"  tile {j * nx + i + 1}/{nx * ny} ({tw}x{th})")
            tiles += _fetch_tile(tx0, ty1, tw, th, res_m, epsg, os.path.join(tile_dir, f"tile_{j}_{i}"))

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


FT = 0.3048
UNITS = {"ft": FT, "m": 1.0}      # metres per display unit
UNIT_NAMES = {"ft": "feet", "m": "metres"}
LEGEND_TICKS = {"ft": (0, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000),
                "m": (0, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500)}
RAMP_STYLES = ("smooth", "stepped")
RAMP_STEPS = (4, 6, 8, 10, 12, 16, 20, 24)
MAX_CLASSES = 30


def rem_auto_top(rem_tif: str, units: str = "ft") -> float:
    """RiverREM's default ramp top: half the REM's maximum, in `units`."""
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(rem_tif)  # keep a reference: the band is invalid once its dataset is freed
    band = ds.GetRasterBand(1)
    band.ComputeStatistics(False)
    top_m = 0.5 * band.GetMaximum()
    ds = None
    return max(top_m / UNITS[units], 1.0)


def _nice(v: float) -> float:
    """Round a class break to a value that reads well on a legend (same rules for feet or metres)."""
    if v < 1:
        return max(0.5, round(v * 2) / 2)
    for limit, step in ((10, 1), (30, 2), (100, 5), (300, 10), (1000, 50), (3000, 100)):
        if v < limit:
            return round(v / step) * step
    return round(v / 500) * 500


def _nice_step(v: float) -> float:
    """Smallest 1/2/2.5/5 x 10^k that is >= v (an even step size for linear ramps)."""
    k = math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * 10 ** k >= v - 1e-9:
            return m * 10 ** k
    return 10 ** (k + 1)


def _knee_for_first_band(top_m: float, first_m: float, steps: int) -> float | None:
    """Knee c (metres) for the log curve h(f) = c * ((top/c + 1)**f - 1) so the first of `steps`
    bands is first_m tall. Larger c widens the low bands. None if first_m is so large that even
    evenly spaced bands are thinner (then the caller should space evenly).
    """
    if first_m >= top_m / steps:
        return None

    def first(c: float) -> float:
        return c * ((top_m / c + 1) ** (1 / steps) - 1)

    lo, hi = 1e-6, 1e9   # first() rises monotonically with c, from ~0 to top/steps
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if first(mid) < first_m else (lo, mid)
    return math.sqrt(lo * hi)


def ramp(cmap: str, top: float, style: str = "smooth", log: bool = True, steps: int = 12,
         units: str = "ft", first: float | None = None, invert: bool = False) -> dict:
    """Colour table (for gdaldem color-relief) and legend for the REM colour ramp.

    Heights (top, first and the legend) are in `units` ("ft" or "m"); the colour table is in metres.

    log=True follows a log curve h(f) = c * ((top/c + 1)**f - 1) for f in [0, 1], so colours change
    fastest near the river. c = 1 m is RiverREM's own curve. For stepped ramps, `first` sets the
    height of the lowest band and c is solved to match, so the low bands can be widened for steep
    valleys. log=False spaces heights evenly (`first`, if given, is the step size).
    Stepped ramps get one evenly spaced colour per class, so thin classes stay distinct.
    Heights above the top get the last colour.
    """
    from riverrem.RasterViz import RasterViz

    if style not in RAMP_STYLES:
        raise ValueError(f"ramp style must be one of {RAMP_STYLES}")
    if units not in UNITS:
        raise ValueError(f"units must be one of {tuple(UNITS)}")
    base_cm = RasterViz._get_cm_mpl(cmap)
    # invert=True runs the colormap backwards (river gets the far end); works for any colormap
    cm = (lambda k: base_cm(254 - k)) if invert else base_cm
    u = UNITS[units]
    top_m = top * u
    knee = 1.0
    if log and style == "stepped" and first:
        knee = _knee_for_first_band(top_m, first * u, steps)
        if knee is None:   # requested first band is wider than even spacing allows
            log = False

    def rgb(k: int) -> tuple:   # same 1..255 scaling RiverREM uses (0 is reserved for nodata)
        return tuple(round(c * 254 + 1) for c in cm(int(k))[:3])

    def hexc(k: int) -> str:
        return "#%02x%02x%02x" % rgb(k)

    def height_m(f: float) -> float:
        return knee * ((top_m / knee + 1) ** f - 1) if log else top_m * f

    def position(v: float) -> float:   # inverse of height_m, as a fraction of the ramp
        h = v * u
        return math.log(h / knee + 1) / math.log(top_m / knee + 1) if log else h / top_m

    if style == "stepped":
        if log:
            bounds = [0.0]
            for i in range(1, steps):
                b = _nice(height_m(i / steps) / u)
                if bounds[-1] < b < top:
                    bounds.append(b)
            bounds.append(_nice(top) if _nice(top) > bounds[-1] else top)
        else:   # one step size, top rounded up to a whole step, so every class is the same height
            if first:
                step = first if top / first <= MAX_CLASSES else _nice_step(top / MAX_CLASSES)
            else:
                k = math.floor(math.log10(top / steps))
                options = [m * 10 ** e for e in (k - 1, k, k + 1) for m in (1, 2, 2.5, 5)]
                step = min(options, key=lambda s: (abs(math.ceil(top / s - 1e-9) - steps), -s))
            bounds = [round(step * i, 6) for i in range(math.ceil(top / step - 1e-9) + 1)]
        n = len(bounds) - 1
        ks = [round(i / (n - 1) * 254) if n > 1 else 0 for i in range(n)]
        table = [f"-100000 {' '.join(map(str, rgb(ks[0])))}"]
        for i in range(1, n):   # two entries per break, a hair apart, give a hard edge
            table.append(f"{bounds[i] * u - 0.0005:.4f} {' '.join(map(str, rgb(ks[i - 1])))}")
            table.append(f"{bounds[i] * u:.4f} {' '.join(map(str, rgb(ks[i])))}")
        table.append(f"100000 {' '.join(map(str, rgb(ks[-1])))}")
        legend = {"mode": "stepped", "top": round(bounds[-1], 2),
                  "classes": [[bounds[i], bounds[i + 1], hexc(ks[i])] for i in range(n)]}
    else:
        table = [f"{height_m(k / 254):.4f} {' '.join(map(str, rgb(k)))}" for k in range(255)]
        n_stops = 33
        stops = [[round(i / (n_stops - 1), 4), hexc(round(i / (n_stops - 1) * 254))] for i in range(n_stops)]
        if log:
            candidates = LEGEND_TICKS[units]
        else:
            step = _nice_step(top / 5)
            candidates = [step * i for i in range(int(top / step) + 1)]
        ticks, last = [], -1.0
        for v in candidates:
            frac = position(v)
            if frac > 1.0001:
                break
            if frac - last >= 0.09:   # keep labels from crowding; print_layout thins further by width
                ticks.append([round(frac, 4), f"{v:,g}"])
                last = frac
        legend = {"mode": "smooth", "top": round(top, 2), "stops": stops, "ticks": ticks}
    table.append("nv 0 0 0")
    legend.update(log=log, cmap=cmap, invert=invert, units=units, unit_name=UNIT_NAMES[units])
    return {"table": "\n".join(table) + "\n", "legend": legend}


def qgis_python() -> tuple[str, dict]:
    """Python executable and environment of the newest installed QGIS (override with QGIS_APP)."""
    import glob
    import plistlib

    def version(app: str) -> tuple:
        try:
            with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                v = plistlib.load(f).get("CFBundleShortVersionString", "0")
            return tuple(int(p) for p in v.split(".") if p.isdigit())
        except (OSError, ValueError):
            return (0,)

    apps = [os.environ["QGIS_APP"]] if os.environ.get("QGIS_APP") else sorted(
        glob.glob("/Applications/QGIS*.app") + glob.glob(os.path.expanduser("~/Applications/QGIS*.app")),
        key=version, reverse=True)
    for app in apps:
        exes = sorted(glob.glob(os.path.join(app, "Contents", "MacOS", "python3.*[0-9]")))
        if not exes:
            continue
        exe = exes[-1]
        res = os.path.join(app, "Contents", "Resources", os.path.basename(exe))
        env = dict(os.environ,
                   PYTHONPATH=os.pathsep.join([res, os.path.join(res, "lib-dynload"),
                                               os.path.join(res, "site-packages")]),
                   PROJ_DATA=os.path.join(app, "Contents", "Resources", "qgis", "proj"),
                   QT_QPA_PLATFORM="offscreen")
        env.pop("PYTHONHOME", None)
        return exe, env
    raise RuntimeError("QGIS not found in /Applications. Install QGIS or set QGIS_APP.")


def make_print(spec: dict, out_dir: str) -> dict:
    """Run print_layout.py in QGIS to build the PDF, PNG and .qgz for this run."""
    spec_path = os.path.join(out_dir, "print_spec.json")
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=2)
    exe, env = qgis_python()
    log(f"Building print layout with {exe.split('/Contents')[0]}")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "print_layout.py")
    proc = subprocess.run([exe, script, spec_path], env=env, capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        # harmless Qt/GDAL noise: font alias lookup, and QGIS trying to write georeferencing into the PNG
        if line.strip() and not line.startswith(("Could not find platform", "QStandardPaths", "qt.qpa.fonts",
                                                 "ERROR 6: The PNG driver")):
            log(f"  qgis: {line}")
    if proc.returncode != 0:
        raise RuntimeError(f"QGIS print layout failed (exit {proc.returncode}); see log.")
    with open(os.path.join(out_dir, "print_result.json")) as f:
        return json.load(f)


RAMP_DEFAULTS = {"style": "smooth", "log": True, "steps": 12, "top": None, "units": "ft", "first": None,
                 "invert": False}
LABEL_DEFAULTS = {"color": "auto", "scope": "river"}   # color: auto | white | black | off; scope: river | all
LABEL_INKS = {"white": "#ffffff", "black": "#1d2a2f"}
NOT_A_STREAM = ("ditch", "canal", "lateral", "drain", "flume", "pipeline", "aqueduct")


def make_viz(dem: str, rem_tif: str, out_dir: str, cmap: str, table_path: str,
             z: float = 4, blend_percent: float = 25) -> str:
    """Colour the REM with `table_path` and blend it with the DEM's hillshade.

    Mirrors REMMaker.make_rem_viz, but keeps the hillshade (it depends only on the DEM) in
    <run>/.hillshade/ so recolouring a run skips it. Returns the hillshade-colour GeoTIFF;
    a georeferenced PNG is written next to it.
    """
    import glob
    from riverrem import RasterViz as rasterviz

    hs_dir, work = os.path.join(out_dir, ".hillshade"), os.path.join(out_dir, ".cache")
    os.makedirs(hs_dir, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    dem_name = os.path.basename(dem).split(".")[0]
    cached = sorted(glob.glob(os.path.join(hs_dir, "*hillshade*.tif")))
    if cached:
        hillshade = cached[0]
        log("Reusing saved hillshade")
    else:
        dem_viz = rasterviz.RasterViz(dem, out_dir=hs_dir, out_ext=".tif")
        dem_viz.make_hillshade(multidirectional=True, z=z)
        hillshade = dem_viz.hillshade_ras

    # RiverREM builds its own colour table inside make_color_relief; hand it ours instead
    original = rasterviz.RasterViz._get_cmap_txt
    rasterviz.RasterViz._get_cmap_txt = lambda self, cmap, log_scale=False: table_path
    try:
        rem_viz = rasterviz.RasterViz(rem_tif, out_dir=work, out_ext=".tif", make_png=True, make_kmz=False)
        rem_viz.make_color_relief(cmap=cmap, log_scale=True)
    finally:
        rasterviz.RasterViz._get_cmap_txt = original
    rem_viz.out_rasters["hillshade-color"] = os.path.join(out_dir, f"{dem_name}_hillshade-color.tif")
    rem_viz.hillshade_ras = hillshade
    rem_viz.viz_srs = rem_viz.proj
    viz_tif = rem_viz.make_hillshade_color(blend_percent=blend_percent)
    shutil.rmtree(work, ignore_errors=True)
    return viz_tif


def label_layers(out_dir: str, manifest: dict, scope: str) -> list[dict]:
    """Line layers whose names are printed along the streams.

    Always the mapped river (from the run's centerline, high priority); with scope "all", also the
    other named streams in the frame (ditches and canals left out), at lower priority so the main
    river wins any conflict. Uses the cached river lines, so no network is needed.
    """
    files, epsg = manifest["files"], manifest["print_frame"]["epsg"]
    layers = [{"path": os.path.abspath(os.path.join(out_dir, files["centerline"])), "priority": 9}]
    if scope != "all":
        return layers
    bbox = tuple(manifest["bbox"])
    try:
        lines, _ = named_lines(bbox)
    except Exception as exc:
        log(f"Couldn't load other streams for labels ({exc}); labelling the main river only")
        return layers
    names = lines["gnis_name"].str.lower()
    lines = lines[(lines["gnis_name"] != manifest["river"]) & ~names.str.contains("|".join(NOT_A_STREAM))]
    lines = lines.clip(bbox)
    lines = lines[~lines.geometry.is_empty]
    if not lines.empty:
        path = os.path.join(out_dir, "streams.gpkg")
        lines.to_crs(epsg).to_file(path, driver="GPKG")
        layers.append({"path": os.path.abspath(path), "priority": 4})
    return layers


def label_ink(choice: str, legend: dict) -> str | None:
    """Label colour: white or black, or 'auto' = whichever contrasts with the ramp's colour at the river."""
    if choice == "off":
        return None
    if choice in LABEL_INKS:
        return LABEL_INKS[choice]
    lowest = legend["classes"][0][2] if legend["mode"] == "stepped" else legend["stops"][0][1]
    r, g, b = (int(lowest[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return LABEL_INKS["white" if 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5 else "black"]


def style_outputs(out_dir: str, manifest: dict, save, cmap: str, ramp_opts: dict,
                  title: str | None, subtitle: str | None, make_pdf: bool, label_opts: dict | None = None) -> None:
    """Colour the REM, then build the map preview and print layout. Used by run() and restyle()."""
    files = manifest["files"]
    dem, rem_tif = os.path.join(out_dir, files["dem"]), os.path.join(out_dir, files["rem"])
    opts = {**RAMP_DEFAULTS, **ramp_opts}
    units = opts["units"]
    top = opts["top"] or rem_auto_top(rem_tif, units)
    colors = ramp(cmap, top, opts["style"], opts["log"], opts["steps"], units, opts["first"], opts["invert"])
    table_path = os.path.join(out_dir, "color_table.txt")
    with open(table_path, "w") as f:
        f.write(colors["table"])
    legend = colors["legend"]
    log(f"Colour ramp: {cmap}{' inverted' if opts['invert'] else ''}, {opts['style']}, "
        f"{'log' if legend['log'] else 'linear'}, top {top:,.0f} {units}"
        + (f", {len(legend['classes'])} classes from {legend['classes'][0][1]:g} {units}"
           if opts["style"] == "stepped" else ""))
    title = title or manifest.get("title") or manifest["river"]
    subtitle = subtitle if subtitle is not None else manifest.get("subtitle", "River Relative Elevation Model")
    labels = {**LABEL_DEFAULTS, **(manifest.get("labels") or {}), **(label_opts or {})}
    save(step="viz", cmap=cmap, ramp=opts, title=title, subtitle=subtitle, labels=labels)
    viz_tif = make_viz(dem, rem_tif, out_dir, cmap, table_path)

    save(step="preview")
    preview = os.path.join(out_dir, "preview_4326.png")
    bounds = make_preview(viz_tif, preview)
    files.update(viz=os.path.basename(viz_tif), viz_png=os.path.basename(viz_tif).replace(".tif", ".png"),
                 color_table="color_table.txt", preview=os.path.basename(preview))
    for stale in ("pdf", "png", "qgz"):
        files.pop(stale, None)

    frame = manifest.get("print_frame")
    if make_pdf and frame:
        save(step="print", files=files, bounds=bounds)
        source = manifest.get("centerline_source", "NHD")
        data = "USGS NHD HighRes" if source == "NHD" else "© OpenStreetMap contributors"
        slug = "".join(c if c.isalnum() else "-" for c in manifest["river"].lower()).strip("-")
        style = (f"{''.join(c if c.isalnum() else '-' for c in cmap)}{'-inverted' if opts['invert'] else ''}"
                 f"-{opts['style']}")
        ink = label_ink(labels["color"], legend)
        spec_labels = None
        if ink:
            spec_labels = {"color": ink, "layers": label_layers(out_dir, manifest, labels["scope"])}
            log(f"River labels: {labels['color']} ({'white' if ink == LABEL_INKS['white'] else 'black'}), "
                f"{'all named streams' if labels['scope'] == 'all' else manifest['river']}")
        spec = {
            "out_dir": os.path.abspath(out_dir),
            "viz_tif": os.path.abspath(viz_tif),
            "frame": frame,
            "title": title,
            "subtitle": subtitle,
            "legend": legend,
            "labels": spec_labels,
            "credits": (f"Elevation: USGS 3DEP {manifest['res_m']:g} m  ·  River: {data}  ·  "
                        f"REM: RiverREM  ·  UTM {frame['epsg'] - 26900}N  ·  {datetime.now():%b %Y}"),
            # one set of print files per colour style, so restyles don't overwrite each other
            "basename": f"{slug}_{frame['page']}_{frame['orientation']}_{style}",
            "dpi": 300,
        }
        printed = make_print(spec, out_dir)
        files.update({k: printed[k] for k in ("pdf", "png", "qgz")})
        save(fonts=printed.get("fonts"))
    elif make_pdf:
        log("No print frame saved for this run; skipping the print layout.")
    save(files=files, bounds=bounds)


def _job_saver(out_dir: str, manifest: dict):
    path = os.path.join(out_dir, "job.json")

    def save(**updates):
        manifest.update(updates)
        with open(path, "w") as f:
            json.dump(manifest, f, indent=2)
    return save


def run(river: str, res_m: float, cmap: str, out_dir: str | None = None, *,
        bbox: tuple | None = None, stretch: dict | None = None, corridor_m: float = 1500,
        page: str = "11x17", orientation: str = "auto", title: str | None = None,
        subtitle: str | None = None, make_pdf: bool = True, ramp_opts: dict | None = None,
        label_opts: dict | None = None) -> dict:
    """Generate a REM and print layout for `river`, framed on a drawn bbox or a stretch (GeoJSON, EPSG:4326).

    ramp_opts: style, log, steps, top, units, first (see ramp()); top=None uses RiverREM's automatic top.
    """
    from riverrem.REMMaker import REMMaker

    frame = print_frame(page, orientation, stretch=stretch, bbox=bbox, corridor_m=corridor_m)
    bbox = tuple(frame["bbox"])
    slug = "".join(c if c.isalnum() else "-" for c in river.lower()).strip("-")
    out_dir = out_dir or os.path.join(RUNS_DIR, f"{datetime.now():%Y%m%d-%H%M%S}_{slug}_{res_m:g}m")
    os.makedirs(out_dir, exist_ok=True)
    manifest = {"bbox": list(bbox), "river": river, "res_m": res_m, "cmap": cmap,
                "mode": "stretch" if stretch else "box", "page": page,
                "orientation": frame["orientation"], "scale": frame["scale"],
                "rotation": frame["rotation"], "frame": frame["frame"], "print_frame": frame,
                "pid": os.getpid(), "files": {},
                "status": "running", "started": datetime.now().isoformat(timespec="seconds")}
    save = _job_saver(out_dir, manifest)
    with open(os.path.join(out_dir, "frame.geojson"), "w") as f:
        json.dump({"type": "Feature", "properties": {k: frame[k] for k in ("page", "orientation", "scale", "rotation")},
                   "geometry": frame["frame"]}, f)
    if stretch:
        manifest["corridor_m"] = corridor_m
        with open(os.path.join(out_dir, "stretch.geojson"), "w") as f:
            json.dump({"type": "Feature", "properties": {"river": river}, "geometry": stretch}, f)

    save(step="centerline")
    try:
        log(f"Frame: {print_spec.PAGE_LABELS[page]} {frame['orientation']}, 1:{frame['scale']:,}, "
            f"rotated {frame['rotation']:g}°, {frame['width_m'] / 1000:.1f} × {frame['height_m'] / 1000:.1f} km")
        log(f"Getting centerline for {river!r}")
        lines, source = named_lines(bbox)
        lines = lines[lines["gnis_name"] == river].clip(bbox)
        lines = lines[~lines.geometry.is_empty]
        if lines.empty:
            raise RuntimeError(f"No {source} river lines named {river!r} in this area.")
        epsg = frame["epsg"]
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
        shutil.rmtree(os.path.join(out_dir, ".cache"), ignore_errors=True)
        manifest["files"].update(rem=os.path.basename(rem_tif), dem=os.path.basename(dem),
                                 centerline="centerline.gpkg", frame="frame.geojson")
        if stretch:
            manifest["files"]["stretch"] = "stretch.geojson"

        style_outputs(out_dir, manifest, save, cmap, ramp_opts or {}, title, subtitle, make_pdf, label_opts)
        save(status="done", step="done", finished=datetime.now().isoformat(timespec="seconds"))
        log(f"Done: {out_dir}")
    except Exception as exc:
        save(status="error", error=str(exc))
        log(f"ERROR: {exc}")
        raise
    return manifest


def restyle(out_dir: str, cmap: str | None = None, ramp_opts: dict | None = None,
            title: str | None = None, subtitle: str | None = None, make_pdf: bool = True,
            label_opts: dict | None = None) -> dict:
    """Recolour a finished run and rebuild its preview and print, reusing its DEM and REM (no download).

    Options left as None keep the run's previous values.
    """
    with open(os.path.join(out_dir, "job.json")) as f:
        manifest = json.load(f)
    files = manifest.get("files", {})
    missing = [k for k in ("dem", "rem") if not files.get(k) or not os.path.exists(os.path.join(out_dir, files[k]))]
    if missing:
        raise RuntimeError(f"This run has no saved {' or '.join(missing).upper()}, so it can't be recoloured.")
    if not manifest.get("print_frame"):   # runs from before print_frame was stored kept it in print_spec.json
        spec_path = os.path.join(out_dir, "print_spec.json")
        if os.path.exists(spec_path):
            with open(spec_path) as f:
                manifest["print_frame"] = json.load(f).get("frame")
    previous = {**RAMP_DEFAULTS, **(manifest.get("ramp") or {})}
    if "top_ft" in previous:   # older runs stored the top in feet
        previous["top"], previous["units"] = previous.pop("top_ft"), "ft"
    opts = {**previous, **(ramp_opts or {})}   # ramp_opts holds only the options that were given
    cmap = cmap or manifest.get("cmap", "mako")

    save = _job_saver(out_dir, manifest)
    save(status="running", step="viz", pid=os.getpid(), error=None,
         restyled=datetime.now().isoformat(timespec="seconds"))
    try:
        log(f"Recolouring {os.path.basename(out_dir)} (reusing its DEM and REM)")
        style_outputs(out_dir, manifest, save, cmap, opts, title, subtitle, make_pdf, label_opts)
        save(status="done", step="done", finished=datetime.now().isoformat(timespec="seconds"))
        log("Done")
    except Exception as exc:
        save(status="error", error=str(exc))
        log(f"ERROR: {exc}")
        raise
    return manifest


def add_ramp_args(sp, keep: bool = False) -> None:
    """Colour options. With keep=True (restyle) every default is None, meaning "keep the run's value"."""
    d = (lambda v: None) if keep else (lambda v: v)
    sp.add_argument("--cmap", default=d("mako"), help="matplotlib / seaborn / cmocean colormap")
    sp.add_argument("--ramp", default=d("smooth"), choices=RAMP_STYLES, help="continuous ramp or distinct steps")
    sp.add_argument("--spacing", default=d("log"), choices=("log", "linear"),
                    help="log: finer steps near the stream (default); linear: even steps")
    sp.add_argument("--steps", type=int, default=d(12), help="number of colour steps (--ramp stepped)")
    sp.add_argument("--units", default=d("ft"), choices=tuple(UNITS), help="units for --top, --first and the legend")
    sp.add_argument("--top", default=d("auto"), help="height where the ramp ends, or 'auto'")
    sp.add_argument("--first", default=d("auto"),
                    help="stepped ramps: height of the lowest band (e.g. 2 with --units m), or 'auto'")
    sp.add_argument("--invert", default=d(False), action=argparse.BooleanOptionalAction,
                    help="run the colormap backwards (--no-invert to undo on restyle)")
    sp.add_argument("--labels", default=d("auto"), choices=("auto", "white", "black", "off"),
                    help="river name labels on the print; auto picks white or black to contrast with the river")
    sp.add_argument("--label-scope", default=d("river"), choices=("river", "all"),
                    help="label just the mapped river, or all named streams in the frame")


def ramp_from_args(args) -> dict:
    """Only the options that were given (None means 'not given')."""
    opts = {}
    if args.ramp is not None:
        opts["style"] = args.ramp
    if args.spacing is not None:
        opts["log"] = args.spacing == "log"
    if args.steps is not None:
        opts["steps"] = args.steps
    if args.units is not None:
        opts["units"] = args.units
    for key in ("top", "first"):
        value = getattr(args, key)
        if value is not None:
            opts[key] = None if str(value).lower() == "auto" else float(value)
    if args.invert is not None:
        opts["invert"] = args.invert
    return opts


def labels_from_args(args) -> dict:
    """Only the label options that were given."""
    opts = {}
    if args.labels is not None:
        opts["color"] = args.labels
    if args.label_scope is not None:
        opts["scope"] = args.label_scope
    return opts


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rivers", help="list named rivers in a bbox")
    r.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    t = sub.add_parser("trace", help="trace the river between two points")
    t.add_argument("--start", nargs=2, type=float, required=True, metavar=("LON", "LAT"))
    t.add_argument("--end", nargs=2, type=float, required=True, metavar=("LON", "LAT"))
    t.add_argument("--river", default=None)
    g = sub.add_parser("run", help="generate a REM for a bbox, or for a stretch between two points")
    g.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    g.add_argument("--start", nargs=2, type=float, metavar=("LON", "LAT"))
    g.add_argument("--end", nargs=2, type=float, metavar=("LON", "LAT"))
    g.add_argument("--stretch", help="GeoJSON file with the stretch LineString (from the app)")
    g.add_argument("--river", help="river name; required with --bbox, optional with --start/--end")
    g.add_argument("--corridor", type=float, default=1500,
                   help="stretch mode: minimum metres shown each side of the river")
    g.add_argument("--res", type=float, default=10, help="DEM resolution in metres (1, 3, 10, 30)")
    g.add_argument("--page", default="11x17", choices=list(print_spec.PAGES))
    g.add_argument("--orientation", default="auto", choices=["auto", "horizontal", "vertical"])
    g.add_argument("--title", help="map title (default: river name)")
    g.add_argument("--subtitle", help="map subtitle (default: River Relative Elevation Model)")
    g.add_argument("--no-print", action="store_true", help="skip the QGIS print layout")
    g.add_argument("--out", default=None)
    add_ramp_args(g)
    s = sub.add_parser("restyle", help="recolour a finished run without downloading again")
    s.add_argument("--run", required=True, help="run folder, e.g. river_rem_runs/20261001-..._10m")
    s.add_argument("--title")
    s.add_argument("--subtitle")
    s.add_argument("--no-print", action="store_true", help="skip the QGIS print layout")
    add_ramp_args(s, keep=True)
    args = p.parse_args()

    if args.cmd == "rivers":
        result = list_rivers(tuple(args.bbox))
        print(f"Source: {result['source']}")
        for rv in result["rivers"]:
            print(f"{rv['length_km']:8.2f} km  {rv['name']}")
        return 0
    if args.cmd == "trace":
        tr = trace_stretch(tuple(args.start), tuple(args.end), args.river)
        print(f"{tr['river']} ({tr['source']}): {tr['length_km']} km, snapped {tr['snap_m'][0]} m / {tr['snap_m'][1]} m")
        return 0
    if args.cmd == "restyle":
        restyle(args.run, args.cmap, ramp_from_args(args), args.title, args.subtitle, not args.no_print,
                labels_from_args(args))
        return 0

    stretch, river = None, args.river
    if args.stretch:
        with open(args.stretch) as f:
            gj = json.load(f)
        stretch = gj.get("geometry", gj)
        river = river or gj.get("properties", {}).get("river")
    elif args.start and args.end:
        tr = trace_stretch(tuple(args.start), tuple(args.end), river)
        stretch, river = tr["stretch"], tr["river"]
        log(f"Stretch: {river}, {tr['length_km']} km")
    elif not args.bbox:
        p.error("run needs --bbox, --start/--end, or --stretch")
    if not river:
        p.error("--river is required")
    run(river, args.res, args.cmap, args.out, bbox=tuple(args.bbox) if args.bbox else None,
        stretch=stretch, corridor_m=args.corridor, page=args.page, orientation=args.orientation,
        title=args.title, subtitle=args.subtitle, make_pdf=not args.no_print, ramp_opts=ramp_from_args(args),
        label_opts=labels_from_args(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
