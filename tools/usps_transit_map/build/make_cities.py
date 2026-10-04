#!/usr/bin/env python3
"""Make web/cities.json: city labels for the map, from Natural Earth (public domain).

Each city gets a tier, which decides the zoom level it appears at:
  1  major cities, shown on the national view: metro population of
     1 million or more, plus the largest city in every state and territory
  2  regional cities, shown when zoomed in a little
  3  smaller places, shown when zoomed in further

Usage (from tools/usps_transit_map/):
  python build/make_cities.py

Run it again only if you want to refresh the list; the output is committed.
"""

import json
import re
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
       "geojson/ne_10m_populated_places_simple.geojson")
COUNTRIES = {"USA", "PRI", "GUM", "VIR", "ASM", "MNP"}


def main():
    with urllib.request.urlopen(URL, timeout=120) as r:
        data = json.load(r)
    rows = [f["properties"] for f in data["features"] if f["properties"].get("adm0_a3") in COUNTRIES]

    # Largest city in each state / territory always makes the national view.
    largest = {}
    for p in rows:
        key = p["adm1name"] if p["adm0_a3"] == "USA" else p["adm0_a3"]
        if key not in largest or p["pop_max"] > largest[key]["pop_max"]:
            largest[key] = p
    top_ids = {id(p) for p in largest.values()}

    out = []
    for p in sorted(rows, key=lambda p: -p["pop_max"]):
        if p["pop_max"] >= 1_000_000 or id(p) in top_ids:
            tier = 1
        elif p["pop_max"] >= 150_000 or p["scalerank"] <= 4:
            tier = 2
        else:
            tier = 3
        name = re.sub(r"\s+", " ", p["name"]).strip()
        out.append([name, round(p["longitude"], 3), round(p["latitude"], 3), tier, int(p["pop_max"])])

    dest = APP / "web" / "cities.json"
    dest.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    tiers = {t: sum(1 for c in out if c[3] == t) for t in (1, 2, 3)}
    print(f"wrote {dest} - {len(out)} places, by tier {tiers}")


if __name__ == "__main__":
    main()
