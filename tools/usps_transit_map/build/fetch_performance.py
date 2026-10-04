#!/usr/bin/env python3
"""Download USPS on-time performance results and write web/data/perf.json.

USPS posts quarterly results by district on about.usps.com:
  - "quarterly performance": percent delivered on time
  - "service variance": percent delivered within +1, +2 and +3 days of the target
They cover market-dominant mail only. This script reads:
  fcm  Single-Piece First-Class Mail (letters you mail yourself), by district,
       split by target (2-day, 3-to-5-day)
  mkt  USPS Marketing Mail, end-to-end, by district
  per  Periodicals, by area (USPS publishes only 4 areas for these)
USPS publishes no geographic results for Priority Mail or Ground Advantage.

It also reads USPS's list of which 3-digit ZIP prefixes belong to each
district, so the web page can look up the district for any ZIP.

Usage (from tools/usps_transit_map/):
  python build/fetch_performance.py                 # newest quarter
  python build/fetch_performance.py --quarter 2026-3
  python build/fetch_performance.py --html-dir DIR  # read saved pages instead of downloading

Never fails the build: if anything is missing it prints a warning and the
map simply shows no on-time numbers.
"""

import argparse
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

APP = Path(__file__).resolve().parent.parent
BASE = "https://about.usps.com/what/performance/service-performance/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}
PRODUCTS = {
    # key: (USPS page slug, label, level, column group to use)
    "fcm": ("single-piece-first-class-mail", "Single-Piece First-Class Mail", "district", None),
    "mkt": ("marketing-mail", "USPS Marketing Mail (end-to-end)", "district", "end-to-end"),
    "per": ("periodicals", "Periodicals", "area", None),
}
# Target groups in the First-Class Mail tables -> key used by the web page.
STD_KEYS = {"overnight": "1", "two-day": "2", "three-to-five-day": "3"}


def norm(s):
    s = re.sub(r"\barea\b", "", str(s), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def display_name(s):
    """'Co-Wy' / 'CO-WY' -> 'CO-WY'; 'NEW YORK 1' -> 'New York 1'."""
    s = str(s).strip()
    if s.lower() == "westpac":
        return "WestPac"
    parts = re.split(r"(-)", s)
    if all(len(p) <= 3 for p in parts if p != "-") and "-" in s:
        return s.upper()
    return " ".join(w.capitalize() if not w.isdigit() else w for w in s.split())


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def num(v):
    try:
        f = float(str(v).replace("%", "").strip())
        return f if f == f else None  # NaN -> None
    except ValueError:
        return None


def parse_report(html):
    """Read a USPS performance table into rows of {name, area, values{label: pct}}.

    The tables have one or two header rows (e.g. 'Two-Day' over 'Percent On Time'),
    then a row per area ('Atlantic Area') followed by its districts, then
    'Nation ...' summary rows."""
    tables = pd.read_html(io.StringIO(html))
    t = max(tables, key=lambda x: x.size)
    rows = [["" if (c is None or c != c or str(c) == "nan") else str(c).strip() for c in r]
            for r in t.values.tolist()]
    head = [r for r in rows if any(c in ("District", "Area") for c in r)]
    if not head:
        raise ValueError("no District/Area header row")
    name_col = next(i for i, c in enumerate(head[0]) if c in ("District", "Area"))
    labels = []
    for j in range(len(rows[0])):
        bits = []
        for h in head:
            v = h[j]
            if v and v not in ("District", "Area") and v not in bits:
                bits.append(v)
        labels.append(re.sub(r"\s+", " ", " / ".join(bits)).lower())
    out, nation, area = [], {}, None
    first_data = rows.index(head[-1]) + 1
    for r in rows[first_data:]:
        name = r[name_col]
        if not name:
            continue
        vals = {labels[j]: num(r[j]) for j in range(len(r)) if j != name_col and labels[j] and num(r[j]) is not None}
        if name.lower().startswith("nation"):
            nation[name] = vals
            continue
        if not vals or "target" in name.lower():
            continue
        is_area = name.lower().endswith(" area") or head[0][name_col] == "Area"
        if is_area:
            area = norm(name)
        out.append({"name": name, "key": norm(name), "area": area, "is_area": is_area, "values": vals})
    return out, nation


def pick(values, group, kind):
    """Value for column group ('two-day', 'end-to-end' or None) and kind ('on', 1, 2, 3)."""
    for label, v in values.items():
        if group and not label.startswith(group):
            continue
        if kind == "on" and "on time" in label:
            return v
        if kind != "on" and re.search(rf"within \+{kind}-day", label):
            return v
    return None


def build_class(key, on_html, var_html, quarter):
    slug, label, level, group = PRODUCTS[key]
    on_rows, on_nation = parse_report(on_html)
    var_rows = {r["key"]: r for r in parse_report(var_html)[0]} if var_html else {}
    groups = list(STD_KEYS) if key == "fcm" else [group]
    regions, areas = {}, {}
    for r in on_rows:
        if level == "district" and r["is_area"]:
            areas[r["key"]] = r["name"]
            continue
        rec = {"on": {}, "w": {}}
        for g in groups:
            k = STD_KEYS.get(g, "all")
            on = pick(r["values"], g, "on")
            if on is not None:
                rec["on"][k] = on
            vr = var_rows.get(r["key"])
            if vr:
                w = [pick(vr["values"], g, i) for i in (1, 2, 3)]
                if any(x is not None for x in w):
                    rec["w"][k] = w
        if rec["on"]:
            regions[r["key"]] = rec
            if level == "area":
                areas[r["key"]] = r["name"]
    qlabel = f"Nation FY{quarter[0]} Q{quarter[1]}"
    nat = next((v for n, v in on_nation.items() if n.lower().startswith(qlabel.lower())), {})
    nation = {STD_KEYS.get(g, "all"): pick(nat, g, "on") for g in groups}
    print(f"  {key}: {len(regions)} {level}s, nation on time {nation}")
    return {"label": label, "level": level, "by_std": key == "fcm",
            "regions": regions, "nation": {k: v for k, v in nation.items() if v is not None},
            "source": BASE + f"fy{quarter[0]}-q{quarter[1]}-{slug}-quarterly-performance.html"}, \
        {r["key"]: r["area"] for r in on_rows if not r["is_area"]}, areas


def parse_zip3_list(html):
    """USPS 'Three-digit ZIP Code list by area/district' -> {zip3: district key}, {key: name}."""
    z, names = {}, {}
    for t in pd.read_html(io.StringIO(html)):
        cols = [str(c).lower() for c in t.columns]
        if not any("district" in c for c in cols):
            continue
        dcol = t.columns[next(i for i, c in enumerate(cols) if "district" in c)]
        zcol = t.columns[next(i for i, c in enumerate(cols) if "zip" in c)]
        for _, r in t.iterrows():
            k = norm(r[dcol])
            names[k] = display_name(r[dcol])
            for code in re.findall(r"\b\d{3}\b", str(r[zcol])):
                z[code] = k
    return z, names


def newest_quarter(landing_html):
    qs = re.findall(r"fy(\d{4})-q([1-4])-single-piece-first-class-mail-quarterly-performance", landing_html)
    return max((int(a), int(b)) for a, b in qs) if qs else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=APP / "web/data/perf.json", type=Path)
    ap.add_argument("--quarter", help="e.g. 2026-3 (default: newest listed by USPS)")
    ap.add_argument("--html-dir", type=Path, help="read saved pages from this folder instead of downloading")
    args = ap.parse_args()

    def page(name):
        if args.html_dir:
            p = args.html_dir / name
            return p.read_text() if p.exists() else None
        try:
            return get(BASE + name)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  ! {name}: {e}")
            return None

    if args.quarter:
        quarter = tuple(int(x) for x in args.quarter.split("-"))
    else:
        landing = page("") if not args.html_dir else (args.html_dir / "index.html").read_text()
        quarter = newest_quarter(landing or "")
    if not quarter:
        print("::warning::Could not find a USPS quarterly performance report; skipping on-time data.")
        return
    print(f"USPS on-time performance: FY{quarter[0]} Q{quarter[1]}")

    zhtml = page("zip-3-by-area-district.htm")
    if not zhtml:
        print("::warning::Could not read USPS's ZIP-to-district list; skipping on-time data.")
        return
    zip3, dnames = parse_zip3_list(zhtml)
    print(f"  ZIP list: {len(zip3)} three-digit prefixes in {len(dnames)} districts")

    classes, district_area, area_names = {}, {}, {}
    for key, (slug, *_rest) in PRODUCTS.items():
        pre = f"fy{quarter[0]}-q{quarter[1]}-{slug}"
        on_html = page(f"{pre}-quarterly-performance.html")
        var_html = page(f"{pre}-service-variance.html")
        if not on_html:
            print(f"  ! no {slug} report for this quarter")
            continue
        try:
            cls, d_area, areas = build_class(key, on_html, var_html, quarter)
        except Exception as e:  # one odd table shouldn't sink the rest
            print(f"  ! could not read {slug}: {e}")
            continue
        classes[key] = cls
        if cls["level"] == "district":
            district_area.update(d_area)
        area_names.update(areas)

    if not classes:
        print("::warning::No USPS performance tables could be read; skipping on-time data.")
        return
    unmatched = sorted({d for d in zip3.values()} - set(classes.get("fcm", {}).get("regions", {})))
    if unmatched:
        print(f"  ! districts in the ZIP list without First-Class results: {unmatched}")

    perf = {
        "quarter": f"FY{quarter[0]} Q{quarter[1]}",
        "zip3": zip3,
        "districts": {k: {"name": n, "area": district_area.get(k)} for k, n in dnames.items()},
        "areas": {k: display_name(re.sub(r"\s*area$", "", n, flags=re.I)) for k, n in area_names.items()},
        "classes": classes,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(perf, separators=(",", ":")))
    print(f"  wrote {args.out.name} ({args.out.stat().st_size / 1e3:.0f} KB)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the map build over the extra data
        print(f"::warning::fetch_performance.py failed: {e}")
    sys.exit(0)
