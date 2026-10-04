#!/usr/bin/env python3
"""Write web/data/hubs.json: USPS mail processing plants, for the map's hub layer.

Source: USPS PostalPro "Facility File" (FACILITY.xlsx,
https://postalpro.usps.com/service-hubs-and-facilities/facilityfile), which
lists every USPS facility with its type, name and ZIP code. We keep:
  P  processing plants (Processing & Distribution Centers / Facilities, SCFs):
     where letters and packages for an area are sorted
  N  network hubs (Network / Regional Distribution Centers): big package hubs
Each plant is placed at the middle of its ZIP code (Census ZCTA). Plants on
a ZIP with no Census shape use the nearest-numbered ZIP in the same prefix.

The file does not say which plant serves which ZIP code, so the map shows
the *nearest* plant, not the assigned one.

Output: {"date": "Feb 28, 2019", "source": url,
         "h": [[name, kind, city, ST, lon, lat], ...]}

Usage (from tools/usps_transit_map/):
  python build/fetch_hubs.py                    # download from PostalPro
  python build/fetch_hubs.py --xlsx FACILITY.xlsx --zcta raw/demo/demo_zcta.gpkg

Never fails the build.
"""

import argparse
import io
import json
import re
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from fetch_usps import get, page_links  # noqa: E402

APP = Path(__file__).resolve().parent.parent
PAGE = "https://postalpro.usps.com/service-hubs-and-facilities/facilityfile"
KINDS = {
    "Processing and Distribution Center/Facility (PDC/PDF)": "P",
    "Processing and Distribution Center (PDC)": "P",
    "Sectional Center Processing Facility (SCF)": "P",
    "Network Distribution Center (NDC/ASF)": "N",
    "Regional Distribution Center (RDC)": "N",
}
# Keep these short words in capitals when tidying names.
ABBR = {"P&DC", "P&DF", "PDC", "PDF", "NDC", "RDC", "SCF", "ASF", "ISC", "L&DC", "HUB",
        "AMC", "ANX", "DDC", "RPDC", "LPC", "MPC", "PO", "USPS", "II", "III", "NE", "NW", "SE", "SW"}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def tidy(s):
    words = re.sub(r"\s+", " ", str(s)).strip().split(" ")
    return " ".join(w if w.upper() in ABBR else w.title() for w in words)


def zip_points(zcta_path):
    """5-digit ZIP -> (lon, lat) of a point inside its Census ZCTA shape."""
    gdf = gpd.read_file(zcta_path)
    zcol = next(c for c in gdf.columns if re.search(r"zcta|^z$|zip", c, re.I) and c != "geometry")
    pts = gdf.to_crs(4326).geometry.representative_point()
    return {str(z).zfill(5): (round(p.x, 4), round(p.y, 4)) for z, p in zip(gdf[zcol], pts)}


def locate(z, pts, by3):
    if z in pts:
        return pts[z]
    near = by3.get(z[:3])
    if not near:
        return None
    best = min(near, key=lambda q: abs(int(q) - int(z)))
    return pts[best]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", type=Path, help="local FACILITY.xlsx instead of downloading")
    ap.add_argument("--zcta", type=Path, default=APP / "raw/census/cb_2020_us_zcta520_500k.zip")
    ap.add_argument("--out", type=Path, default=APP / "web/data/hubs.json")
    args = ap.parse_args()

    if args.xlsx:
        data, url = args.xlsx.read_bytes(), args.xlsx.name
    else:
        url = next((u for u, _ in page_links(PAGE) if re.search(r"facility.*\.xlsx?$", u, re.I)), None)
        if not url:
            print("::warning::No USPS Facility File link found; the map will show no postal hubs.")
            return
        print(f"USPS facilities: {url}")
        data, _, _ = get(url, timeout=180)

    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)
    sheet, df = max(sheets.items(), key=lambda kv: len(kv[1]))
    df.columns = [str(c).strip().upper() for c in df.columns]
    print(f"  sheet {sheet!r}: {len(df):,} facilities")
    m = re.search(r"(\d{2})_(\d{2})_(\d{4})", sheet)
    date = f"{MONTHS[int(m[1]) - 1]} {int(m[2])}, {m[3]}" if m else ""

    df = df[df["FACILITY SUBTYPE"].isin(KINDS)].copy()
    print(f"  {len(df)} plants and hubs: {df['FACILITY SUBTYPE'].value_counts().to_dict()}")

    pts = zip_points(args.zcta)
    by3 = {}
    for z in pts:
        by3.setdefault(z[:3], []).append(z)

    hubs, seen, missed = [], set(), []
    for _, r in df.sort_values("FACILITY NAME").iterrows():
        z = re.sub(r"\D", "", str(r["ZIP"]))[:5].zfill(5)
        kind = KINDS[r["FACILITY SUBTYPE"]]
        name = tidy(r["FACILITY NAME"])
        st = str(r.get("FACILITY STATE", "")).strip()
        # Many plants are named by city alone ("ABILENE"): add what they are,
        # so they don't read like city labels on the map.
        if not any(w.upper() in ABBR for w in name.split()):
            name += {"Network Distribution Center (NDC/ASF)": " NDC",
                     "Regional Distribution Center (RDC)": " RDC"}.get(r["FACILITY SUBTYPE"], " mail plant")
        if (name, kind, st) in seen:
            continue
        ll = locate(z, pts, by3)
        if not ll:
            missed.append(f"{name} ({z})")
            continue
        seen.add((name, kind, st))
        hubs.append([name, kind, tidy(r.get("FACILITY CITY", "")), st, *ll])

    if missed:
        print(f"  ! {len(missed)} could not be placed: {missed[:10]}")
    if not hubs:
        print("::warning::No USPS plants could be placed; the map will show no postal hubs.")
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"date": date, "source": url, "h": hubs}, separators=(",", ":")))
    print(f"  wrote {args.out.name}: {len(hubs)} plants and hubs (as of {date or 'unknown date'}); "
          f"e.g. {hubs[:2]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the map build
        print(f"::warning::fetch_hubs.py failed: {e}")
    sys.exit(0)
