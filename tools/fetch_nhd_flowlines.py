#!/usr/bin/env python
"""Fetch NHD HighRes flowlines for a named river within a DEM bbox.

Output is a shapefile in the DEM's CRS, suitable for RiverREM's CENTERLINE_SHP.

Usage:
    fetch_nhd_flowlines.py <dem.tif> <"GNIS Name"> <out.shp>
"""
import sys
import json
import urllib.parse
import urllib.request

import geopandas as gpd
import rasterio
from rasterio.warp import transform_bounds


NHD_LAYER = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3/query"


def fetch_flowlines(gnis_name: str, bbox_wgs84: tuple[float, float, float, float]) -> dict:
    xmin, ymin, xmax, ymax = bbox_wgs84
    params = {
        "where": f"gnis_name = '{gnis_name}'",
        "geometry": json.dumps({"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "permanent_identifier,gnis_name,lengthkm,reachcode,streamorde",
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "geojson",
        "resultRecordCount": 2000,
    }
    url = f"{NHD_LAYER}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read())


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 1
    dem_path, gnis_name, out_shp = sys.argv[1], sys.argv[2], sys.argv[3]

    with rasterio.open(dem_path) as src:
        dem_crs = src.crs
        bbox_wgs84 = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)

    print(f"DEM CRS:    {dem_crs}")
    print(f"WGS84 bbox: {bbox_wgs84}")
    print(f"GNIS:       {gnis_name!r}")

    gj = fetch_flowlines(gnis_name, bbox_wgs84)
    feats = gj.get("features", [])
    print(f"Features:   {len(feats)}")
    if not feats:
        print("No flowlines returned. Try a different name or check the bbox.", file=sys.stderr)
        return 2

    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    gdf = gdf.to_crs(dem_crs)
    total_km = gdf["lengthkm"].sum() if "lengthkm" in gdf.columns else float("nan")
    print(f"Total km:   {total_km:.2f}")
    gdf.to_file(out_shp)
    print(f"Wrote:      {out_shp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
