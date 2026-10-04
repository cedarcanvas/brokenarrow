#!/usr/bin/env python3
"""Make FAKE ZIP shapes for testing the map without the Census download.

Each ZIP becomes the area closest to its center point (a Voronoi cell),
clipped to the state outlines. The shapes are rough and NOT real ZIP
boundaries - use them only to test the build and the web page.

Usage (from tools/usps_transit_map/):
  python build/make_demo_zcta.py path/to/zip_centroids.csv
  python build/build_data.py --zcta raw/demo/demo_zcta.gpkg --demo-days

zip_centroids.csv needs columns zip, lat, lng (for example the Census ZCTA
Gazetteer, or data/zip_centroids.csv from the us-zip-centroids npm package).
"""

import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).parent))
from build_data import HERE, APP, mapshaper_cmd, panel_for  # noqa: E402


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    pts = pd.read_csv(sys.argv[1], dtype={"zip": str})
    pts["zip"] = pts["zip"].str.zfill(5)
    pts["p"] = [panel_for(x, y) for x, y in zip(pts.lng, pts.lat)]

    out_dir = APP / "raw" / "demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    states_json = out_dir / "states.geojson"
    subprocess.run(mapshaper_cmd() + ["-i", str(HERE / "node_modules/us-atlas/states-10m.json"),
                                      "-o", "format=geojson", "target=states", str(states_json)], check=True)
    land = gpd.read_file(states_json)
    land_lonlat = shapely.union_all(land.geometry.make_valid().values)

    cells = []
    for p, grp in pts.groupby("p"):
        # Work per map panel so cells never stretch across oceans or the 180th meridian.
        lon = grp.lng.where(grp.lng < 0, grp.lng - 360) if p == 1 else grp.lng
        mp = shapely.MultiPoint(list(zip(lon, grp.lat)))
        vor = shapely.voronoi_polygons(mp, extend_to=mp.envelope.buffer(3))
        clip = land_lonlat
        if p == 1:  # shift Alaska's far-west islands to match the shifted points
            west = shapely.affinity.translate(shapely.clip_by_rect(land_lonlat, 170, 50, 180, 60), -360)
            clip = shapely.union(shapely.clip_by_rect(land_lonlat, -180, 50, -129, 72), west)
        g = gpd.GeoDataFrame(geometry=list(vor.geoms), crs=4326)
        pt = gpd.GeoDataFrame(grp[["zip"]].reset_index(drop=True),
                              geometry=gpd.points_from_xy(lon, grp.lat), crs=4326)
        g = gpd.sjoin(g, pt, predicate="contains").drop(columns="index_right")
        g["geometry"] = g.geometry.intersection(clip)
        g = g[~g.geometry.is_empty]
        # Keep only the pieces that sit in this panel (a cell's clip can catch
        # land far away, e.g. a Florida cell reaching Puerto Rico).
        g = g.explode(index_parts=False)
        rp = g.geometry.representative_point()
        g = g[[panel_for(x if x > -180 else x + 360, y) == p for x, y in zip(rp.x, rp.y)]]
        g = g.dissolve(by="zip").reset_index()
        cells.append(g)
        print(f"panel {p}: {len(g)} cells")

    out = pd.concat(cells).rename(columns={"zip": "ZCTA5CE20"})
    out = gpd.GeoDataFrame(out, crs=4326)
    path = out_dir / "demo_zcta.gpkg"
    out.to_file(path, driver="GPKG")
    print(f"wrote {path} ({len(out):,} fake ZIP shapes)")


if __name__ == "__main__":
    main()
