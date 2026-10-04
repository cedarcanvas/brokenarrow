#!/usr/bin/env python3
"""Build the data files for the USPS Delivery Days map.

Reads two downloads and writes small files the web page can load:

  1. Census ZIP Code Tabulation Areas (ZCTAs) - the 5-digit ZIP shapes.
     cb_2020_us_zcta520_500k.zip from
     https://www2.census.gov/geo/tiger/GENZ2020/shp/
  2. USPS service standards - days in transit between 3-digit ZIP prefixes,
     per mail class, from https://postalpro.usps.com/service-standards

Writes into web/data/:
  zcta.topo.json   simplified ZIP shapes (TopoJSON), each tagged with
                   z = 5-digit ZIP, i = index of its 3-digit prefix, p = map panel
  states.topo.json state outlines (from the us-atlas npm package)
  zip3.json        list of 3-digit prefixes with state names
  days_<class>.bin one byte per (origin prefix, destination prefix) pair;
                   row = origin, column = destination, 0 = no standard
  meta.json        mail classes, data date, demo flag, build stats

Usage (from tools/usps_transit_map/):
  python build/build_data.py
  python build/build_data.py --zcta raw/census/cb_2020_us_zcta520_500k.zip \
                             --usps raw/usps --vintage "FY2027 Q1"
  python build/build_data.py --demo-days     # fake days, for previewing only

Needs: pip install -r build/requirements.txt  and  (cd build && npm install)
"""

import argparse
import csv
import datetime as dt
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
APP = HERE.parent

# Mail classes, in the order the web page shows them.
CLASSES = {
    "fcm": "First-Class Mail",
    "ga": "Ground Advantage",
    "pm": "Priority Mail",
    "mkt": "Marketing Mail",
    "per": "Periodicals",
}

# Words that identify a mail class in a USPS column value or file name.
# Checked in this order, so the more specific words come first. PostalPro's
# Combined Service Standard Directory files end in product codes: _FCM, _GAH,
# _GAL (Ground Advantage), _PRI (Priority), _MKT, _PER; _PFC and _PKG are not mapped.
CLASS_WORDS = [
    ("ga", ["ground advantage", "groundadvantage", "usps ground", "gnd", "ga", "uga", "gah", "gal"]),
    ("pm", ["priority mail", "priority", "pm", "pri"]),
    ("fcm", ["first-class", "first class", "firstclass", "fcm", "fc", "fcmail"]),
    ("mkt", ["marketing", "standard mail", "mkt", "std", "usps marketing"]),
    ("per", ["periodical", "per", "pd"]),
]

# Map panels: 0 = lower 48, then the inset boxes.
PANELS = ["Lower 48", "Alaska", "Hawaii", "Puerto Rico & USVI", "Guam & N. Mariana Is.", "American Samoa"]


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def panel_for(lon, lat):
    """Which map box a point belongs in, from its longitude/latitude."""
    if lat > 50 and (lon < -129 or lon > 170):
        return 1  # Alaska (Aleutians cross the 180th meridian)
    if -179 < lon < -150 and 15 < lat < 30:
        return 2  # Hawaii
    if -69 < lon < -63 and 16 < lat < 20:
        return 3  # Puerto Rico & US Virgin Islands
    if lon > 140 and 10 < lat < 25:
        return 4  # Guam & Northern Mariana Islands
    if lat < -5 and lon < -160:
        return 5  # American Samoa
    return 0


def mapshaper_cmd():
    """Path to the mapshaper command installed by `npm install` in build/."""
    local = HERE / "node_modules" / ".bin" / "mapshaper-xl"
    if local.exists():
        return [str(local)]
    if shutil.which("mapshaper-xl"):
        return ["mapshaper-xl"]
    sys.exit("mapshaper not found. Run:  cd tools/usps_transit_map/build && npm install")


def run(cmd):
    log("  $ " + " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def class_from_text(text):
    t = " " + str(text).lower().replace("_", " ").replace("-", " ") + " "
    tn = norm(text)
    for key, words in CLASS_WORDS:
        for w in words:
            if " " in w or len(w) > 3:
                if w.replace("-", " ") in t or norm(w) == tn:
                    return key
            elif tn == w or re.search(rf"(^|[^a-z]){w}([^a-z]|$)", t):
                return key
    return None


def parse_days(v):
    """'3' -> 3, '10+' -> 10, '3-5' -> 5 (the slower end), blanks -> 0."""
    m = re.findall(r"\d+", str(v))
    if not m:
        return 0
    d = max(int(x) for x in m)
    return d if 0 < d < 255 else 0


# --------------------------------------------------------------------------
# 1. ZIP shapes
# --------------------------------------------------------------------------

def load_zcta(path):
    log(f"Reading ZIP shapes: {path}")
    src = f"zip://{path}" if str(path).endswith(".zip") else str(path)
    gdf = gpd.read_file(src)
    zcol = next((c for c in gdf.columns if c.upper().startswith("ZCTA5")), None)
    if zcol is None:
        zcol = next((c for c in gdf.columns if c.upper() in ("ZIP", "ZIPCODE", "ZCTA", "GEOID20", "GEOID")), None)
    if zcol is None:
        sys.exit(f"Could not find a ZIP column in {path}. Columns: {list(gdf.columns)}")
    gdf = gdf[[zcol, "geometry"]].rename(columns={zcol: "z"})
    gdf["z"] = gdf["z"].astype(str).str.zfill(5)
    gdf = gdf.to_crs(4326)
    pts = gdf.geometry.representative_point()
    gdf["lon"], gdf["lat"] = pts.x, pts.y
    gdf["p"] = [panel_for(x, y) for x, y in zip(gdf["lon"], gdf["lat"])]
    gdf["zip3"] = gdf["z"].str[:3]
    log(f"  {len(gdf):,} ZIP shapes, {gdf['zip3'].nunique()} three-digit prefixes")
    return gdf


def load_states(tmp):
    """State outlines as GeoJSON, converted from us-atlas TopoJSON."""
    topo = HERE / "node_modules" / "us-atlas" / "states-10m.json"
    if not topo.exists():
        sys.exit("us-atlas not found. Run:  cd tools/usps_transit_map/build && npm install")
    out = tmp / "states.geojson"
    run(mapshaper_cmd() + ["-i", topo, "-o", "format=geojson", "target=states", out])
    return topo, gpd.read_file(out).set_crs(4326, allow_override=True)


def zip3_states(gdf, states):
    """Most common state for each 3-digit prefix (for tooltips)."""
    pts = gpd.GeoDataFrame(gdf[["zip3"]], geometry=gpd.points_from_xy(gdf.lon, gdf.lat), crs=4326)
    j = gpd.sjoin(pts, states[["name", "geometry"]], how="left", predicate="within")
    return j.dropna(subset=["name"]).groupby("zip3")["name"].agg(lambda s: s.value_counts().index[0]).to_dict()


def write_shapes(gdf, zip3_index, out_dir, tmp, simplify):
    gdf = gdf.copy()
    gdf["i"] = gdf["zip3"].map(zip3_index).astype(int)
    src = tmp / "zcta.geojson"
    gdf[["z", "i", "p", "geometry"]].to_file(src, driver="GeoJSON")
    dst = out_dir / "zcta.topo.json"
    run(mapshaper_cmd() + [
        "-i", src, "name=zcta",
        "-simplify", f"{simplify}%", "weighted", "keep-shapes",
        "-clean", "allow-overlaps",
        "-filter-fields", "z,i,p",
        "-o", "format=topojson", "quantization=100000", "precision=0.00001", dst,
    ])
    log(f"  wrote {dst.name}  ({dst.stat().st_size / 1e6:.1f} MB)")


# --------------------------------------------------------------------------
# 2. USPS service standards
# --------------------------------------------------------------------------

def iter_tables(folder):
    """Yield (name, open text stream or Excel bytes) for every data file in folder,
    looking inside .zip files too."""
    exts = (".csv", ".txt", ".tsv", ".dat", ".psv", ".xlsx", ".xls")
    for f in sorted(Path(folder).rglob("*")):
        if f.name.startswith(".") or f.name.lower() in ("readme.md", "columns.json", "vintage.txt"):
            continue
        if f.suffix.lower() == ".zip":
            with zipfile.ZipFile(f) as zf:
                for m in zf.namelist():
                    if m.lower().endswith(exts) and not m.startswith("__MACOSX"):
                        yield f"{f.name}/{m}", zf.read(m)
        elif f.suffix.lower() in exts:
            yield f.name, f.read_bytes()


def sniff(text_head):
    first = text_head.splitlines()[0] if text_head else ""
    counts = {d: first.count(d) for d in [",", "|", "\t", ";"]}
    d = max(counts, key=counts.get)
    return d if counts[d] else None


def pick_column(cols, kind, override=None):
    if override:
        for c in cols:
            if c == override or norm(c) == norm(override):
                return c
        sys.exit(f"Column '{override}' (from columns.json) not found. Columns: {cols}")
    n = {c: norm(c) for c in cols}
    rules = {
        "origin": lambda s: "orig" in s and ("zip" in s or "3" in s or "scf" in s or s.startswith("orig")),
        "dest": lambda s: "dest" in s and ("zip" in s or "3" in s or "scf" in s or s.startswith("dest")),
        "days": lambda s: ("day" in s or "svcstd" in s or "servicestandard" in s or s in ("std", "standard", "sstd"))
                          and "desc" not in s and "eff" not in s and "date" not in s,
        "class": lambda s: ("class" in s or "product" in s or "mailclass" in s) and "sub" not in s,
    }
    hits = [c for c in cols if rules[kind](n[c])]
    if kind in ("origin", "dest"):
        # Prefer a ZIP column over a facility-name column.
        hits.sort(key=lambda c: ("zip" not in n[c], "name" in n[c] or "fac" in n[c]))
        hits = [c for c in hits if "name" not in n[c] and "fac" not in n[c] and "city" not in n[c]] or hits
    return hits[0] if hits else None


def read_usps(folder, keep_zip3):
    """Return {class: {(origin zip3, dest zip3): days}}."""
    folder = Path(folder)
    cfg_path = folder / "columns.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    if cfg:
        log(f"  using column names from {cfg_path}")
    files = list(iter_tables(folder))
    if not files:
        return None

    # (class, origin, dest) -> {days: count}; using the most common value lets
    # 5-digit files fold down to 3-digit prefixes.
    tallies = {}
    for name, raw in files:
        if name.lower().endswith((".xlsx", ".xls")):
            frames = [pd.read_excel(io.BytesIO(raw), dtype=str, sheet_name=None)]
            frames = list(frames[0].values())
        else:
            text = raw.decode("utf-8-sig", errors="replace")
            delim = cfg.get("delimiter") or sniff(text[:5000])
            if delim is None:
                log(f"  ! {name}: no delimiter found - fixed-width files need 'delimiter' in columns.json; skipped")
                continue
            header = 0 if re.search(r"[A-Za-z]", text.splitlines()[0]) else None
            names = cfg.get("names") if header is None else None
            frames = [pd.read_csv(io.StringIO(text), sep=delim, dtype=str, header=header,
                                  names=names, engine="python" if len(delim) > 1 else "c",
                                  quoting=csv.QUOTE_MINIMAL, on_bad_lines="skip")]
        for df in frames:
            df.columns = [str(c).strip() for c in df.columns]
            cols = list(df.columns)
            o = pick_column(cols, "origin", cfg.get("origin"))
            d = pick_column(cols, "dest", cfg.get("dest"))
            k = pick_column(cols, "days", cfg.get("days"))
            c = pick_column(cols, "class", cfg.get("class")) if cfg.get("class", "auto") else None
            if not (o and d and k):
                log(f"  ! {name}: could not find origin/destination/days columns, skipped.\n"
                    f"    Columns seen: {cols}\n"
                    f"    Fix: add raw/usps/columns.json, e.g. "
                    '{"origin": "ORIG_ZIP3", "dest": "DEST_ZIP3", "days": "SVC_STD", "class": "MAIL_CLASS"}')
                continue
            file_class = cfg.get("file_classes", {}).get(Path(name).name) or class_from_text(Path(name).stem)
            if c is None and file_class is None:
                log(f"  ! {name}: no mail-class column and the file name doesn't say which class; skipped")
                continue
            sub = pd.DataFrame({
                "o": df[o].astype(str).str.strip().str.zfill(3).str[:3],
                "d": df[d].astype(str).str.strip().str.zfill(3).str[:3],
                "days": df[k].map(parse_days),
            })
            if c is not None:
                raw_cls = df[c].astype(str)
                lookup = {v: (cfg.get("class_values", {}).get(v) or class_from_text(v)) for v in raw_cls.unique()}
                sub["cls"] = raw_cls.map(lookup)
                unknown = [v for v, kk in lookup.items() if kk is None]
                if unknown:
                    log(f"    {name}: ignoring rows with unknown class values {unknown[:8]}")
            else:
                sub["cls"] = file_class
            sub = sub[sub["cls"].notna() & (sub["days"] > 0)
                      & sub["o"].isin(keep_zip3) & sub["d"].isin(keep_zip3)]
            grp = sub.groupby(["cls", "o", "d", "days"]).size()
            for (cl, oo, dd, dy), n in grp.items():
                t = tallies.setdefault((cl, oo, dd), {})
                t[dy] = t.get(dy, 0) + int(n)
            hist = sub.groupby(["cls", "days"]).size()
            hist = {cl: {int(dy): int(n) for (c2, dy), n in hist.items() if c2 == cl}
                    for cl in sub["cls"].unique()}
            log(f"  {name}: {len(sub):,} usable rows "
                f"(origin={o!r}, dest={d!r}, days={k!r}, class={c or file_class!r})\n"
                f"    days per row: {hist}")

    out = {}
    for (cl, oo, dd), t in tallies.items():
        out.setdefault(cl, {})[(oo, dd)] = max(t, key=lambda dy: (t[dy], -dy))
    return out


def demo_days(zip3_list, centers, noncontig):
    """Made-up days from straight-line distance. ONLY for previewing the map."""
    lon = np.radians([centers[z][0] for z in zip3_list])
    lat = np.radians([centers[z][1] for z in zip3_list])
    dlat = lat[:, None] - lat[None, :]
    dlon = lon[:, None] - lon[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:, None]) * np.cos(lat[None, :]) * np.sin(dlon / 2) ** 2
    km = 6371 * 2 * np.arcsin(np.sqrt(a))
    far = np.array([z in noncontig for z in zip3_list])
    either_far = far[:, None] | far[None, :]
    steps = {
        "fcm": ([300, 900, 2000], [2, 3, 4, 5]),
        "ga": ([400, 1100, 2200], [2, 3, 4, 5]),
        "pm": ([500, 1600], [1, 2, 3]),
        "mkt": ([300, 900, 1800, 2600], [3, 4, 5, 7, 9]),
        "per": ([300, 900, 1800, 2600], [2, 3, 4, 6, 8]),
    }
    far_days = {"fcm": 5, "ga": 12, "pm": 3, "mkt": 10, "per": 9}
    out = {}
    for cl, (edges, vals) in steps.items():
        m = np.array(vals, dtype=np.uint8)[np.digitize(km, edges)]
        m[either_far & (km > 50)] = far_days[cl]
        out[cl] = m
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--zcta", default=APP / "raw/census/cb_2020_us_zcta520_500k.zip", type=Path,
                    help="Census ZCTA shapefile (.zip or .shp)")
    ap.add_argument("--usps", default=APP / "raw/usps", type=Path,
                    help="folder with USPS service-standard files (.zip/.txt/.csv/.xlsx)")
    ap.add_argument("--out", default=APP / "web/data", type=Path)
    ap.add_argument("--vintage", default=None, help='label for the USPS data date, e.g. "FY2027 Q1"')
    ap.add_argument("--simplify", default=6.0, type=float, help="percent of shape detail to keep (default 6)")
    ap.add_argument("--demo-days", action="store_true",
                    help="make up days from distance instead of reading USPS files (preview only)")
    args = ap.parse_args()

    if not args.zcta.exists():
        sys.exit(f"ZIP shape file not found: {args.zcta}\n"
                 "Download cb_2020_us_zcta520_500k.zip from "
                 "https://www2.census.gov/geo/tiger/GENZ2020/shp/ into raw/census/")
    args.out.mkdir(parents=True, exist_ok=True)
    vintage_file = args.usps / "vintage.txt"
    if args.vintage is None and vintage_file.exists() and not args.demo_days:
        args.vintage = vintage_file.read_text().strip()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        gdf = load_zcta(args.zcta)
        states_topo, states = load_states(tmp)

        zip3_list = sorted(gdf["zip3"].unique())
        zip3_index = {z: i for i, z in enumerate(zip3_list)}
        n = len(zip3_list)
        state_of = zip3_states(gdf, states)
        centers = gdf.groupby("zip3")[["lon", "lat"]].median()
        centers = {z: (float(r.lon), float(r.lat)) for z, r in centers.iterrows()}
        panel_of = gdf.groupby("zip3")["p"].agg(lambda s: int(s.value_counts().index[0])).to_dict()

        # ---- days ----
        log("Reading USPS service standards")
        demo = args.demo_days
        mats = {}
        if not demo:
            pairs = read_usps(args.usps, set(zip3_list))
            if not pairs:
                sys.exit(f"No USPS service-standard files found in {args.usps}.\n"
                         "Download them from https://postalpro.usps.com/service-standards "
                         "(see raw/usps/README.md), or run with --demo-days to preview.")
            for cl, d in pairs.items():
                m = np.zeros((n, n), dtype=np.uint8)
                for (oo, dd), days in d.items():
                    m[zip3_index[oo], zip3_index[dd]] = min(days, 254)
                mats[cl] = m
        else:
            log("  DEMO MODE: days are made up from distance, not USPS data")
            noncontig = {z for z, p in panel_of.items() if p != 0}
            mats = demo_days(zip3_list, centers, noncontig)

        # ---- write ----
        log("Writing web data")
        write_shapes(gdf, zip3_index, args.out, tmp, args.simplify)
        shutil.copy(states_topo, args.out / "states.topo.json")
        classes = []
        for cl, label in CLASSES.items():
            if cl not in mats:
                log(f"  (no data for {label})")
                continue
            m = mats[cl]
            (args.out / f"days_{cl}.bin").write_bytes(m.tobytes())
            filled = m[m > 0]
            cover = float((m > 0).mean())
            hist = {int(k): int(v) for k, v in zip(*np.unique(filled, return_counts=True))}
            log(f"  days_{cl}.bin  {label}: {cover:.0%} of prefix pairs filled, days {hist}")
            if cover < 0.5:
                log(f"  ! {label} covers only {cover:.0%} of pairs - check the USPS file")
            classes.append({"key": cl, "label": label, "coverage": round(cover, 4), "histogram": hist})

        zip3_json = [{"z": z, "s": state_of.get(z, ""), "p": panel_of.get(z, 0),
                      "c": [round(centers[z][0], 3), round(centers[z][1], 3)]} for z in zip3_list]
        (args.out / "zip3.json").write_text(json.dumps(zip3_json, separators=(",", ":")))
        missing = sorted(set(zip3_list) - set().union(*[
            {zip3_list[i] for i in np.where(mats[c["key"]].any(axis=1))[0]} for c in classes]))
        meta = {
            "demo": demo,
            "vintage": args.vintage or ("Demo data" if demo else "USPS service standards"),
            "built": dt.date.today().isoformat(),
            "zcta_source": args.zcta.name,
            "n_zip3": n,
            "n_zcta": int(len(gdf)),
            "classes": classes,
            "panels": PANELS,
            "zip3_without_data": missing,
        }
        (args.out / "meta.json").write_text(json.dumps(meta, indent=1))
        if missing:
            log(f"  ! {len(missing)} ZIP prefixes have shapes but no USPS data: {missing[:20]}...")
        log("Done.")


if __name__ == "__main__":
    main()
