#!/usr/bin/env python
"""River REM workflow: bbox + river name -> 3DEP DEM -> NHD (or OSM) centerline -> RiverREM.

Each run writes to its own folder with a job.json manifest the app reads.

Usage (run inside the rem_env conda env):
    pipeline.py rivers --bbox W S E N
    pipeline.py run --bbox W S E N --river "Arkansas River" [--res 10] [--cmap mako] [--out DIR]
    pipeline.py trace --start LON LAT --end LON LAT
    pipeline.py run --start LON LAT --end LON LAT [--corridor 1500] [--res 10]
        [--page 11x17] [--orientation auto|horizontal|vertical] [--title T] [--subtitle S] [--no-print]
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
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
    """Download one 3DEP tile (top-left x0, y1); on a server error, split it into quarters and retry.

    The ImageServer gives up (HTTP 500 after ~25 s) when a request needs too much resampling,
    which happens with 4000 px tiles at 1 m. Smaller requests succeed.
    """
    import urllib.error

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
        data = http_get(DEM_3DEP, params, timeout=300, retries=2)
    except (urllib.error.HTTPError, TimeoutError, socket.timeout) as exc:
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
LEGEND_TICKS_FT = (0, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000)


def legend_info(rem_tif: str, cmap: str, n_stops: int = 33) -> dict:
    """Colour stops and tick positions matching RiverREM's log-scaled colour relief.

    RiverREM samples 255 colours at heights logspace(0, log10(max/2), 255) - 1 metres, so colour
    step k (of 254) sits at height 10**(k/254 * log10(max/2)) - 1. Heights above max/2 get the
    last colour.
    """
    from osgeo import gdal
    from riverrem.RasterViz import RasterViz

    gdal.UseExceptions()
    ds = gdal.Open(rem_tif)  # keep a reference: the band is invalid once its dataset is freed
    band = ds.GetRasterBand(1)
    band.ComputeStatistics(False)
    top_m = 0.5 * band.GetMaximum()
    ds = None
    span = math.log10(top_m + 1) if top_m > 0 else 1.0
    cm = RasterViz._get_cm_mpl(cmap)

    def hexcolor(k: int) -> str:
        r, g, b = (round(c * 254 + 1) for c in cm(k)[:3])
        return f"#{r:02x}{g:02x}{b:02x}"

    stops = [[round(i / (n_stops - 1), 4), hexcolor(round(i / (n_stops - 1) * 254))] for i in range(n_stops)]
    ticks, last = [], -1.0
    for ft in LEGEND_TICKS_FT:
        frac = math.log10(ft * FT + 1) / span
        if frac > 1.0001:
            break
        if frac - last >= 0.09:   # keep labels from crowding on short ramps
            ticks.append([round(frac, 4), f"{ft:,}"])
            last = frac
    return {"top_m": round(top_m, 2), "top_ft": round(top_m / FT, 1), "stops": stops, "ticks": ticks}


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


def run(river: str, res_m: float, cmap: str, out_dir: str | None = None, *,
        bbox: tuple | None = None, stretch: dict | None = None, corridor_m: float = 1500,
        page: str = "11x17", orientation: str = "auto", title: str | None = None,
        subtitle: str | None = None, make_pdf: bool = True) -> dict:
    """Generate a REM and print layout for `river`, framed on a drawn bbox or a stretch (GeoJSON, EPSG:4326)."""
    from riverrem.REMMaker import REMMaker

    frame = print_frame(page, orientation, stretch=stretch, bbox=bbox, corridor_m=corridor_m)
    bbox = tuple(frame["bbox"])
    slug = "".join(c if c.isalnum() else "-" for c in river.lower()).strip("-")
    out_dir = out_dir or os.path.join(RUNS_DIR, f"{datetime.now():%Y%m%d-%H%M%S}_{slug}_{res_m:g}m")
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "job.json")
    manifest = {"bbox": list(bbox), "river": river, "res_m": res_m, "cmap": cmap,
                "mode": "stretch" if stretch else "box", "page": page,
                "orientation": frame["orientation"], "scale": frame["scale"],
                "rotation": frame["rotation"], "frame": frame["frame"], "pid": os.getpid(),
                "status": "running", "started": datetime.now().isoformat(timespec="seconds")}
    with open(os.path.join(out_dir, "frame.geojson"), "w") as f:
        json.dump({"type": "Feature", "properties": {k: frame[k] for k in ("page", "orientation", "scale", "rotation")},
                   "geometry": frame["frame"]}, f)
    if stretch:
        manifest["corridor_m"] = corridor_m
        with open(os.path.join(out_dir, "stretch.geojson"), "w") as f:
            json.dump({"type": "Feature", "properties": {"river": river}, "geometry": stretch}, f)

    def save(**updates):
        manifest.update(updates)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

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
        save(step="viz")
        viz_tif = rem.make_rem_viz(cmap=cmap, make_png=True)
        viz_png = viz_tif.replace(".tif", ".png")

        save(step="preview")
        preview = os.path.join(out_dir, "preview_4326.png")
        bounds = make_preview(viz_tif, preview)
        shutil.rmtree(os.path.join(out_dir, ".cache"), ignore_errors=True)
        files = {"rem": os.path.basename(rem_tif), "viz": os.path.basename(viz_tif),
                 "viz_png": os.path.basename(viz_png), "dem": os.path.basename(dem),
                 "centerline": "centerline.gpkg", "frame": "frame.geojson",
                 "preview": os.path.basename(preview)}
        if stretch:
            files["stretch"] = "stretch.geojson"

        if make_pdf:
            save(step="print")
            data = "USGS NHD HighRes" if source == "NHD" else "© OpenStreetMap contributors"
            spec = {
                "out_dir": os.path.abspath(out_dir),
                "viz_tif": os.path.abspath(viz_tif),
                "frame": frame,
                "title": title or river,
                "subtitle": subtitle if subtitle is not None else "River Relative Elevation Model",
                "legend": legend_info(rem_tif, cmap),
                "credits": (f"Elevation: USGS 3DEP {res_m:g} m  ·  River: {data}  ·  "
                            f"REM: RiverREM  ·  UTM {epsg - 26900}N  ·  {datetime.now():%b %Y}"),
                "basename": f"{slug}_{page}_{frame['orientation']}",
                "dpi": 300,
            }
            printed = make_print(spec, out_dir)
            files.update({k: printed[k] for k in ("pdf", "png", "qgz")})
            save(fonts=printed.get("fonts"))

        save(status="done", step="done", finished=datetime.now().isoformat(timespec="seconds"),
             bounds=bounds, files=files)
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
    g.add_argument("--cmap", default="mako")
    g.add_argument("--page", default="11x17", choices=list(print_spec.PAGES))
    g.add_argument("--orientation", default="auto", choices=["auto", "horizontal", "vertical"])
    g.add_argument("--title", help="map title (default: river name)")
    g.add_argument("--subtitle", help="map subtitle (default: River Relative Elevation Model)")
    g.add_argument("--no-print", action="store_true", help="skip the QGIS print layout")
    g.add_argument("--out", default=None)
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
        title=args.title, subtitle=args.subtitle, make_pdf=not args.no_print)
    return 0


if __name__ == "__main__":
    sys.exit(main())
