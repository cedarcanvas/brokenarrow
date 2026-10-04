#!/usr/bin/env python3
"""Write web/data/zipnames.json: the city name for each 5-digit ZIP code.

Source: USPS PostalPro "ZIP Code Locale Detail" spreadsheet
(https://postalpro.usps.com/ZIP_Locale_Detail), which lists every delivery
ZIP code with its post office's city and state.

Output: {"c": [["Palos Verdes Peninsula", "CA"], ...], "z": {"90274": 0, ...}}
(city list + ZIP -> index, to keep the file small).

Also prints a short look at two USPS facility lists (3-digit "SCF" hubs) so
a later version of the map can mark postal hubs. Never fails the build.
"""

import io
import re
import sys
import urllib.error
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from fetch_usps import get, page_links  # noqa: E402

APP = Path(__file__).resolve().parent.parent
PAGE = "https://postalpro.usps.com/ZIP_Locale_Detail"


def titlecase(s):
    s = re.sub(r"\s+", " ", str(s)).strip().title()
    # Keep short tokens like "Ft" / "St" readable; fix "Mc" names.
    return re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), s)


def pick(cols, *wants):
    norm = {c: re.sub(r"[^a-z]", "", str(c).lower()) for c in cols}
    for w in wants:
        for c in cols:
            if w in norm[c]:
                return c
    return None


def zip_names():
    links = page_links(PAGE)
    url = next((u for u, t in links if re.search(r"zip_?locale.*\.xlsx?$", u, re.I)), None)
    if not url:
        print("::warning::No ZIP Locale Detail download found; ZIP codes will show the nearest city instead.")
        return
    print(f"ZIP names: {url}")
    data, _, _ = get(url, timeout=300)
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)
    df = max(sheets.values(), key=len)
    cols = list(df.columns)
    print(f"  {len(df):,} rows; columns: {cols}")
    zc = pick(cols, "deliveryzipcode", "deliveryzip", "zipcode", "zip")
    cc = pick(cols, "physicalcity", "city", "localename")
    sc = pick(cols, "physicalstate", "state")
    print(f"  using zip={zc!r} city={cc!r} state={sc!r}")
    if not (zc and cc):
        print("::warning::Could not find ZIP/city columns; skipping ZIP names.")
        return
    best = {}
    for z, c, s in zip(df[zc], df[cc], df[sc] if sc else [""] * len(df)):
        z = str(z).strip()
        if not re.fullmatch(r"\d{3,5}", z) or str(c) == "nan":
            continue
        best.setdefault(z.zfill(5), Counter())[(titlecase(c), str(s).strip().upper() if s == s else "")] += 1
    cities, index, out = [], {}, {}
    for z, cnt in sorted(best.items()):
        key = cnt.most_common(1)[0][0]
        if key not in index:
            index[key] = len(cities)
            cities.append(list(key))
        out[z] = index[key]
    dest = APP / "web/data/zipnames.json"
    dest.write_text(__import__("json").dumps({"c": cities, "z": out}, separators=(",", ":")))
    print(f"  wrote {dest.name}: {len(out):,} ZIP codes, {len(cities):,} city names "
          f"({dest.stat().st_size / 1e3:.0f} KB); e.g. {[(z, cities[i]) for z, i in list(out.items())[:3]]}")


def probe_hubs():
    """Print what USPS's facility file / hub location pages hold (for the hub layer)."""
    print("\nProbe: postal hub sources")
    for page in ("https://postalpro.usps.com/service-hubs-and-facilities/facilityfile",):
        try:
            links = page_links(page)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  - {page}: {e}")
            continue
        files = [(u, t) for u, t in links if re.search(r"\.(zip|xlsx?|txt|csv|pdf)$", u, re.I)]
        print(f"  + {page}: {len(links)} links; files: {files[:10]}")
        for u, _ in files[:3]:
            if not u.lower().endswith(".pdf"):
                show_file(u)


def show_file(url):
    try:
        data, _, _ = get(url, timeout=120)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"     ! {e}")
        return
    blobs = []
    if data[:2] == b"PK" and not url.lower().endswith((".xlsx", ".xls")):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for n in zf.namelist()[:3]:
                blobs.append((n, zf.read(n)))
    else:
        blobs.append((url.rsplit("/", 1)[-1], data))
    for name, b in blobs:
        if name.lower().endswith(".pdf"):
            print(f"     {name}: PDF, {len(b):,} bytes")
        elif name.lower().endswith((".xlsx", ".xls")):
            for sh, df in pd.read_excel(io.BytesIO(b), sheet_name=None, dtype=str, header=None).items():
                print(f"     {name} [{sh}] {df.shape}:\n" + df.head(12).to_string(max_colwidth=40))
                # Short value counts for columns that look like categories.
                for c in df.columns:
                    vc = df[c].value_counts()
                    if 1 < len(vc) <= 40:
                        print(f"       col {c}: {dict(vc.head(40))}")
        else:
            txt = b[:1500].decode("latin-1", "replace")
            print(f"     {name} ({len(b):,} bytes) first lines:\n       " + "\n       ".join(txt.splitlines()[:10]))


if __name__ == "__main__":
    for step in (zip_names, probe_hubs):
        try:
            step()
        except Exception as e:  # never break the map build over extras
            print(f"::warning::{step.__name__} failed: {e}")
    sys.exit(0)
