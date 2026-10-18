#!/usr/bin/env python
"""Make the shaded-relief tiles for the avalanche map.

Downloads elevation from the AWS Terrain Tiles open dataset (USGS 3DEP in the
US), reprojects it to the map's projection (D3's geoAlbersUsa, scale 1300,
translate [487.5, 305], with the Alaska and Hawaii insets), shades it, and cuts
it into JPEG tiles at four zoom levels:

    tools/maps_site/avalanche-deaths/relief/{level}/{x}/{y}.jpg

It also writes the list of tiles into the page's <script id="relief-tiles">
block, and mountain range and peak names from Natural Earth into its
<script id="terrain-labels"> block. Gray 128 means flat: the page blends the
tiles over the state colors with hard-light, so mid-gray changes nothing,
shadows darken and sunlit slopes lighten.

Usage:
    build_relief.py [page.html]

Needs numpy and Pillow. Downloads (about 95 MB) are kept in .cache/ next to
this script, so a second run is fast.
"""
import io
import json
import math
import re
import shutil
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
DEFAULT_PAGE = HERE.parent / "maps_site" / "avalanche-deaths" / "index.html"
CACHE = HERE / ".cache" / "terrarium"
TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
NATURAL_EARTH = "https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@v5.1.2/geojson/{name}.geojson"

W, H = 975, 610          # the page's SVG viewBox
TILE = 512               # tile size in pixels
LEVELS = 4               # level L has 2**L pixels per map unit
EARTH_R = 6371008.8      # D3 projects onto a sphere of this radius (in effect)

# Elevation sources: (web-mercator zoom, west, south, east, north). Level 3 is
# about 0.6 km per pixel in the lower 48, so zoom 8 (about 0.45 km) is enough.
# Alaska is drawn at 0.35x scale and Hawaii is tiny, so they need less.
SOURCES = {
    0: (8, -125.0, 24.3, -66.8, 49.5),   # lower 48
    1: (6, -180.0, 51.0, -129.5, 71.6),  # Alaska
    2: (7, -160.6, 18.8, -154.6, 22.4),  # Hawaii
}
# Vertical exaggeration per level: coarser levels flatten slopes, so lift them more.
EXAGGERATION = {0: 6.0, 1: 4.0, 2: 2.8, 3: 2.0}
# Light from the northwest, with a little from the west and north so ridges
# running northwest-southeast don't go flat.
LIGHTS = [(315, 0.6), (270, 0.2), (0, 0.2)]
ALTITUDE = 45


# ---- D3's geoAlbersUsa, in numpy -------------------------------------------------------

class Conic:
    """d3.geoConicEqualArea with rotate([lambda, 0]), center, parallels, scale and translate."""

    def __init__(self, parallels, rotate, center, scale, translate):
        y0, y1 = np.radians(parallels)
        sy0 = math.sin(y0)
        self.n = (sy0 + math.sin(y1)) / 2
        self.c = 1 + sy0 * (2 * self.n - sy0)
        self.r0 = math.sqrt(self.c) / self.n
        self.dl = math.radians(rotate)
        self.k = scale
        self.tx, self.ty = translate
        self.cx, self.cy = self.raw(*np.radians(center))

    def raw(self, x, y):
        r = np.sqrt(self.c - 2 * self.n * np.sin(y)) / self.n
        x = x * self.n
        return r * np.sin(x), self.r0 - r * np.cos(x)

    def raw_invert(self, x, y):
        r0y = self.r0 - y
        lam = np.arctan2(x, np.abs(r0y)) * np.sign(r0y)
        lam = np.where(r0y * self.n < 0, lam - np.pi * np.sign(x) * np.sign(r0y), lam)
        phi = np.arcsin(np.clip((self.c - (x * x + r0y * r0y) * self.n * self.n) / (2 * self.n), -1, 1))
        return lam / self.n, phi

    @staticmethod
    def wrap(lam):
        return np.where(lam > np.pi, lam - 2 * np.pi, np.where(lam < -np.pi, lam + 2 * np.pi, lam))

    def forward(self, lon, lat):
        rx, ry = self.raw(self.wrap(np.radians(lon) + self.dl), np.radians(lat))
        return self.tx + self.k * (rx - self.cx), self.ty - self.k * (ry - self.cy)

    def invert(self, x, y):
        lam, phi = self.raw_invert((x - self.tx) / self.k + self.cx, (self.ty - y) / self.k + self.cy)
        return np.degrees(self.wrap(lam - self.dl)), np.degrees(phi)


K, TX, TY = 1300, 487.5, 305
PROJ = {
    0: Conic((29.5, 45.5), 96, (-0.6, 38.7), K, (TX, TY)),
    1: Conic((55, 65), 154, (-2, 58.5), K * 0.35, (TX - 0.307 * K, TY + 0.201 * K)),
    2: Conic((8, 18), 157, (-3, 19.9), K, (TX - 0.205 * K, TY + 0.212 * K)),
}


def region_at(x, y):
    """Which part of geoAlbersUsa a map point belongs to: 0 lower 48, 1 Alaska, 2 Hawaii."""
    u, v = (x - TX) / K, (y - TY) / K
    ak = (v >= 0.120) & (v < 0.234) & (u >= -0.425) & (u < -0.214)
    hi = ~ak & (v >= 0.166) & (v < 0.234) & (u >= -0.214) & (u < -0.115)
    return np.where(ak, 1, np.where(hi, 2, 0)).astype(np.uint8)


# ---- Elevation ------------------------------------------------------------------------

def mercator(lon, lat, z):
    n = 256 * 2 ** z
    lat = np.radians(np.clip(lat, -85, 85))
    return (lon + 180) / 360 * n, (1 - np.log(np.tan(lat) + 1 / np.cos(lat)) / np.pi) / 2 * n


def fetch(z, x, y):
    path = CACHE / str(z) / str(x) / f"{y}.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(TERRARIUM.format(z=z, x=x, y=y), timeout=60) as resp:
            path.write_bytes(resp.read())
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)
    return rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768


def mosaic(z, west, south, east, north):
    x0, y0 = (int(v // 256) for v in mercator(west, north, z))
    x1, y1 = (int(v // 256) for v in mercator(east, south, z))
    jobs = [(z, x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    out = np.zeros(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256), np.float32)
    with ThreadPoolExecutor(16) as pool:
        for (zz, x, y), tile in zip(jobs, pool.map(lambda j: fetch(*j), jobs)):
            out[(y - y0) * 256:(y - y0 + 1) * 256, (x - x0) * 256:(x - x0 + 1) * 256] = tile
    print(f"  zoom {z}: {len(jobs)} tiles, {out.shape[1]} x {out.shape[0]} px")
    return out, x0 * 256, y0 * 256


def sample(grid, gx, gy):
    """Bilinear sample at fractional pixel positions; 0 outside the grid."""
    gx, gy = gx - 0.5, gy - 0.5
    ix, iy = np.floor(gx).astype(np.int64), np.floor(gy).astype(np.int64)
    fx, fy = (gx - ix).astype(np.float32), (gy - iy).astype(np.float32)
    h, w = grid.shape
    inside = (ix >= 0) & (iy >= 0) & (ix < w - 1) & (iy < h - 1)
    ix, iy = np.clip(ix, 0, w - 2), np.clip(iy, 0, h - 2)
    top = grid[iy, ix] * (1 - fx) + grid[iy, ix + 1] * fx
    bottom = grid[iy + 1, ix] * (1 - fx) + grid[iy + 1, ix + 1] * fx
    return np.where(inside, top * (1 - fy) + bottom * fy, 0).astype(np.float32)


def elevation_grid(level, mosaics):
    """Elevation in meters for every pixel of the map at this level, plus the region of each pixel."""
    scale = 2 ** level
    w, h = math.ceil(W * scale), math.ceil(H * scale)
    elev = np.zeros((h, w), np.float32)
    region = np.zeros((h, w), np.uint8)
    xs = (np.arange(w) + 0.5) / scale
    for r0 in range(0, h, 256):
        ys = (np.arange(r0, min(h, r0 + 256)) + 0.5) / scale
        X, Y = np.meshgrid(xs, ys)
        reg = region_at(X, Y)
        out = np.zeros(X.shape, np.float32)
        for rid, (grid, ox, oy) in mosaics.items():
            m = reg == rid
            if not m.any():
                continue
            lon, lat = PROJ[rid].invert(X[m], Y[m])
            gx, gy = mercator(lon, lat, SOURCES[rid][0])
            out[m] = sample(grid, gx - ox, gy - oy)
        elev[r0:r0 + len(ys)] = out
        region[r0:r0 + len(ys)] = reg
    return elev, region


def halve(a):
    """2 x 2 mean, for generalizing a finer level into a coarser one."""
    h, w = a.shape
    a = np.pad(a, ((0, h % 2), (0, w % 2)), mode="edge")
    return a.reshape(a.shape[0] // 2, 2, a.shape[1] // 2, 2).mean(axis=(1, 3))


# ---- Shading, land mask, tiles --------------------------------------------------------

def hillshade(elev, region, level):
    cell = np.where(region == 1, EARTH_R / (K * 0.35), EARTH_R / K) / 2 ** level  # meters per pixel
    gy, gx = np.gradient(elev)
    gx = gx / cell * EXAGGERATION[level]
    gy = gy / cell * EXAGGERATION[level]
    norm = np.sqrt(1 + gx * gx + gy * gy)
    alt = math.radians(ALTITUDE)
    shade = np.zeros_like(elev)
    for az, weight in LIGHTS:
        a = math.radians(az)
        # Surface normal (-dz/dx_east, -dz/dy_north, 1); rows run south, so dz/dy_north = -gy.
        shade += weight * (-gx * math.sin(a) * math.cos(alt) + gy * math.cos(a) * math.cos(alt) + math.sin(alt)) / norm
    return shade - math.sin(alt)  # 0 on flat ground


def land_polygons(page_html):
    """US state outlines from the page's own topology, projected to map units."""
    topo = json.loads(re.search(r'<script id="states-topo" type="application/json">(.*?)</script>', page_html, re.S)[1])
    (sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        q = np.cumsum(np.array(arc, dtype=np.float64), axis=0)
        arcs.append(np.column_stack([q[:, 0] * sx + tx, q[:, 1] * sy + ty]))
    polys = []
    for g in topo["objects"]["states"]["geometries"]:
        rid = {"AK": 1, "HI": 2}.get(g["id"], 0)
        for poly in g["arcs"] if g["type"] == "MultiPolygon" else [g["arcs"]]:
            ring = np.concatenate([arcs[i] if i >= 0 else arcs[~i][::-1] for i in poly[0]])  # outer ring only
            x, y = PROJ[rid].forward(ring[:, 0], ring[:, 1])
            polys.append(np.column_stack([x, y]))
    return polys


def land_mask(polys, level):
    scale = 2 ** level
    img = Image.new("L", (math.ceil(W * scale), math.ceil(H * scale)), 0)
    draw = ImageDraw.Draw(img)
    for p in polys:
        draw.polygon([tuple(v) for v in p * scale], fill=255)
    return np.asarray(img, np.float32) / 255


def natural_earth(name):
    path = HERE / ".cache" / "naturalearth" / f"{name}.geojson"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(NATURAL_EARTH.format(name=name), timeout=120) as resp:
            path.write_bytes(resp.read())
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def region_of_lonlat(lon, lat):
    return 1 if lon < -129.5 and lat > 50 else 2 if lat < 23 and lon < -154 else 0


def terrain_labels(state_polys):
    """Mountain ranges and peaks that fall in the US, as [name, kind, zoom to show at, x, y]."""
    mask = land_mask(state_polys, 1) > 0.5  # 2 px per map unit
    h, w = mask.shape
    X, Y = np.meshgrid((np.arange(w) + 0.5) / 2, (np.arange(h) + 0.5) / 2)
    region = region_at(X, Y)
    inland = mask.copy()  # US land at least 3 px (about 7 km) from the border, for peaks
    for _ in range(3):
        inland[1:-1, 1:-1] &= inland[:-2, 1:-1] & inland[2:, 1:-1] & inland[1:-1, :-2] & inland[1:-1, 2:]
    labels = []
    for f in natural_earth("ne_10m_geography_regions_polys"):
        p, g = f["properties"], f["geometry"]
        if p["FEATURECLA"] != "Range/mtn":
            continue
        img = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(img)
        rid = None
        for poly in g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]:
            ring = np.array(poly[0])
            rid = region_of_lonlat(*ring.mean(axis=0))
            x, y = PROJ[rid].forward(ring[:, 0], ring[:, 1])
            draw.polygon(list(zip(x * 2, y * 2)), fill=255)
        inside = (np.asarray(img) > 0) & (region == rid)  # drop parts that land in another region's inset
        ys, xs = np.nonzero(inside & mask)
        if len(xs) < 40 or len(xs) < 0.15 * inside.sum():
            continue  # not in the US, or only a sliver of it
        # Label the middle of the range's US part. Natural Earth writes big ranges in capitals and shortens "Mts."
        name = p["NAME"].title() if p["NAME"].isupper() else p["NAME"]
        name = name.replace("Mts.", "Mountains")
        rank = p["SCALERANK"]
        labels.append([name, "range", 1.6 if rank <= 2 else 2.4 if rank <= 4 else 3.2,
                       round(xs.mean() / 2, 1), round(ys.mean() / 2, 1)])
    for f in natural_earth("ne_10m_geography_regions_elevation_points"):
        p = f["properties"]
        if p["featurecla"] != "mountain":
            continue
        lon, lat = f["geometry"]["coordinates"]
        rid = region_of_lonlat(lon, lat)
        x, y = (float(v[0]) for v in PROJ[rid].forward(np.array([lon]), np.array([lat])))
        if not (0 <= x < W and 0 <= y < H) or not inland[int(y * 2), int(x * 2)] or region[int(y * 2), int(x * 2)] != rid:
            continue
        # Natural Earth's rank undersells some big peaks (Rainier), so height can bring a peak in sooner.
        rank, height = p["scalerank"], p["elevation"] or 0
        min_zoom = min(2.4 if rank <= 3 else 3.2 if rank <= 6 else 4.5, 2.4 if height >= 4000 else 3.2 if height >= 3000 else 4.5)
        labels.append([p["name"], "peak", min_zoom, round(x, 1), round(y, 1)])
    return labels


def write_block(html, block_id, blob):
    pattern = re.compile(rf'(<script id="{block_id}" type="application/json">).*?(</script>)', re.S)
    if not pattern.search(html):
        raise SystemExit(f'The page has no <script id="{block_id}"> block.')
    return pattern.sub(lambda m: m[1] + blob + m[2], html, count=1)


def write_tiles(gray, level, out_dir):
    h, w = gray.shape
    padded = np.pad(gray, 1, mode="edge")  # each tile carries a 1-px bleed so neighbors overlap
    names = []
    for ty in range(math.ceil(h / TILE)):
        for tx in range(math.ceil(w / TILE)):
            block = padded[ty * TILE:ty * TILE + TILE + 2, tx * TILE:tx * TILE + TILE + 2]
            if np.all(np.abs(block.astype(np.int16) - 128) <= 1):
                continue  # no land in this tile
            full = np.full((TILE + 2, TILE + 2), 128, np.uint8)
            full[:block.shape[0], :block.shape[1]] = block
            path = out_dir / str(level) / str(tx) / f"{ty}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(full, "L").save(path, quality=80, optimize=True)
            names.append([tx, ty])
    return names


def main() -> int:
    if len(sys.argv) > 2:
        print(__doc__, file=sys.stderr)
        return 1
    page = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PAGE
    html = page.read_text(encoding="utf-8")
    out_dir = page.parent / "relief"

    print("Elevation:")
    mosaics = {rid: mosaic(*src) for rid, src in SOURCES.items()}
    elev, region = elevation_grid(LEVELS - 1, mosaics)
    del mosaics
    polys = land_polygons(html)
    mask = land_mask(polys, LEVELS - 1)

    if out_dir.exists():
        shutil.rmtree(out_dir)
    manifest = {"tile": TILE, "levels": []}
    for level in range(LEVELS - 1, -1, -1):
        shade = hillshade(elev, region, level)
        gray = np.clip(128 + 200 * shade * mask, 0, 255).astype(np.uint8)
        names = write_tiles(gray, level, out_dir)
        manifest["levels"].insert(0, names)
        size = sum(f.stat().st_size for f in (out_dir / str(level)).rglob("*.jpg"))
        print(f"Level {level}: {gray.shape[1]} x {gray.shape[0]} px, {len(names)} tiles, {size / 1e6:.1f} MB")
        if level:
            elev, mask = halve(elev), halve(mask)
            region = region[::2, ::2][:elev.shape[0], :elev.shape[1]]

    labels = terrain_labels(polys)
    print(f"Terrain labels: {sum(1 for l in labels if l[1] == 'range')} ranges, {sum(1 for l in labels if l[1] == 'peak')} peaks")
    html = write_block(html, "relief-tiles", json.dumps(manifest, separators=(",", ":")))
    html = write_block(html, "terrain-labels", json.dumps(labels, ensure_ascii=False, separators=(",", ":")))
    page.write_text(html, encoding="utf-8")
    print(f"Wrote tiles to {out_dir} and the tile list and labels to {page.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
