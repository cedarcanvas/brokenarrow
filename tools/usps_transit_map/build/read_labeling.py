#!/usr/bin/env python3
"""Write web/data/sort.json: the official USPS sorting chain for each ZIP prefix.

Reads the USPS labeling lists in raw/labeling/ (from FAST, see the README there):
  L005  3-digit ZIP prefix -> SCF   (Sectional Center Facility: the local plant)
  L004  3-digit ZIP prefix -> ADC   (Area Distribution Center: the regional center
                                     for letters; rows for First-Class Mail are used)
  L601  3-digit ZIP prefix -> NDC   (Network Distribution Center, or one of the new
                                     Regional Processing & Distribution Centers, for
                                     packages)
Rows are pipe-separated; only rows in effect today are used.

Each facility is placed at the middle of its ZIP prefix (from zip3.json).

Output: {"date": "Sep 27, 2026",
         "f": [[level, name, ST, zip, lon, lat], ...],      level: SCF / ADC / NDC / RPDC
         "z": {"802": [scf, adc, hub], ...}}                indexes into f, or -1

If days_fcm.bin is there too, it also prints how the official plants (SCFs)
line up with the plant areas inferred from delivery days (probe_network.py).

Usage (from tools/usps_transit_map/):  python build/read_labeling.py
Never fails the build.
"""

import datetime
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
RAW = APP / "raw/labeling"
DATA = APP / "web/data"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
# Short names USPS uses on labels.
NAMES = {"SPFLD": "Springfield", "JAXVILLE": "Jacksonville", "CINCINN": "Cincinnati",
         "KANS CITY": "Kansas City", "MPLS/STP": "Minneapolis-St. Paul", "PHILA": "Philadelphia",
         "SAN FRAN": "San Francisco", "NEW JERSEY": "New Jersey", "ST LOUIS": "St. Louis"}


def tidy(s):
    s = re.sub(r"\s+", " ", s).strip()
    if s in NAMES:
        return NAMES[s]
    keep = {"APO/FPO", "AE", "AA", "AP", "NDC", "SCF", "ADC"}
    return " ".join(w if w in keep or w.isdigit() else w.title() for w in s.split(" "))


def read(code):
    """Rows of one list ('L005'), from a .zip or a plain text file in raw/labeling/."""
    lines = []
    for p in sorted(RAW.iterdir()):
        if not p.name.upper().startswith(code):
            continue
        if p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    lines += z.read(n).decode("latin-1").splitlines()
        elif p.suffix.lower() in (".txt", ".dat", ""):
            lines += p.read_text("latin-1").splitlines()
    return [ln.split("|") for ln in lines if ln.count("|") >= 16]


def in_effect(r, today):
    def d(s):
        return datetime.date(int(s[4:]), int(s[:2]), int(s[2:4])) if re.fullmatch(r"\d{8}", s) else None
    a, b = d(r[15]), d(r[16])
    return (a is None or a <= today) and (b is None or today <= b)


def main():
    if not RAW.exists():
        print("No raw/labeling/ folder; skipping the USPS sorting chain.")
        return
    today = datetime.date.today()
    lists = {c: [r for r in read(c) if in_effect(r, today)] for c in ("L005", "L004", "L601")}
    if not lists["L005"]:
        print("::warning::No USPS labeling list L005 in raw/labeling/; skipping the sorting chain.")
        return
    print("USPS labeling lists: " + ", ".join(f"{c} {len(v)} rows" for c, v in lists.items()))

    zip3 = {r["z"]: r for r in json.loads((DATA / "zip3.json").read_text())}
    facilities, index = [], {}

    def facility(level, name, st, z):
        key = (level, name, st, z)
        if key not in index:
            c = zip3.get(z[:3], {}).get("c")
            index[key] = len(facilities)
            facilities.append([level, tidy(name), st, z, *(c or [None, None])])
        return index[key]

    chain = defaultdict(lambda: [-1, -1, -1])
    for r in lists["L005"]:
        chain[r[2]][0] = facility("SCF", r[6], r[7], r[8])
    # L004 has one row per prefix, or one per mail class; use the general or First-Class row.
    best = {}
    for r in lists["L004"]:
        rank = {"": 0, "FCM": 1}.get(r[4])
        if rank is not None and r[5] and (r[2] not in best or rank < best[r[2]][0]):
            best[r[2]] = (rank, r)
    for z, (_, r) in best.items():
        chain[z][1] = facility("ADC", r[6], r[7], r[8])
    # L601: prefer the package hub (NDC / RPDC) over a local SCF row.
    hub = {}
    for r in lists["L601"]:
        rank = {"NDC": 0, "RPDC": 0}.get(r[5], 1)
        if r[2] not in hub or rank < hub[r[2]][0]:
            hub[r[2]] = (rank, r)
    for z, (_, r) in hub.items():
        chain[z][2] = facility(r[5] or "NDC", r[6], r[7], r[8])

    m = None
    for p in RAW.iterdir():
        if p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as zf:
                m = m or next((re.search(r"_(\d{4})(\d{2})(\d{2})_", n) for n in zf.namelist()
                               if re.search(r"_(\d{4})(\d{2})(\d{2})_", n)), None)
    date = f"{MONTHS[int(m[2]) - 1]} {int(m[3])}, {m[1]}" if m else ""

    # A facility on a ZIP prefix with no map shape (e.g. 192xx, Philadelphia NDC)
    # goes to the middle of the prefixes it serves instead.
    for k, f in enumerate(facilities):
        if f[4] is None:
            pts = [zip3[z]["c"] for z, c in chain.items() if k in c and zip3.get(z, {}).get("c")]
            if pts:
                f[4] = round(sum(p[0] for p in pts) / len(pts), 3)
                f[5] = round(sum(p[1] for p in pts) / len(pts), 3)
    missing = [f"{f[1]} {f[0]}" for f in facilities if f[4] is None]
    if missing:
        print(f"  ! no location for: {missing}")
    levels = Counter(f[0] for f in facilities)
    out = {"date": date, "f": facilities, "z": dict(sorted(chain.items()))}
    (DATA / "sort.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"  wrote sort.json: {len(chain)} ZIP prefixes; facilities {dict(levels)}; lists dated {date or '?'}")
    hubs = Counter(facilities[c[2]][1] + " " + facilities[c[2]][0] for c in chain.values() if c[2] >= 0)
    print(f"  package hubs by prefixes served: {hubs.most_common(30)}")

    compare(chain, facilities)


def compare(chain, facilities):
    """How official plants (SCF) line up with plant areas inferred from First-Class days."""
    try:
        import numpy as np
    except ImportError:
        return
    zfile, dfile = DATA / "zip3.json", DATA / "days_fcm.bin"
    meta = json.loads((DATA / "meta.json").read_text()) if (DATA / "meta.json").exists() else {}
    if not dfile.exists() or meta.get("demo"):
        print("  (no real First-Class days here, so no comparison with the inferred plant areas)")
        return
    z3 = json.loads(zfile.read_text())
    N = len(z3)
    D = np.fromfile(dfile, dtype=np.uint8).reshape(N, N)
    groups = defaultdict(list)
    for i, r in enumerate(z3):
        if r["z"] in chain:
            groups[(r.get("p", 0), D[i].tobytes(), D[:, i].tobytes())].append(r["z"])
    pure = sum(1 for g in groups.values() if len({chain[z][0] for z in g}) == 1)
    per_scf = Counter()
    for g in groups.values():
        for s in {chain[z][0] for z in g}:
            per_scf[s] += 1
    print(f"  inferred plant areas: {len(groups)}; inside a single official SCF: {pure} "
          f"({pure / max(len(groups), 1):.0%}); official SCFs: {len({c[0] for c in chain.values()})}; "
          f"SCFs split into 2+ inferred areas: {sum(1 for v in per_scf.values() if v > 1)}")
    mixed = [g for g in groups.values() if len({chain[z][0] for z in g}) > 1][:6]
    for g in mixed:
        print("    mixed area: " + ", ".join(f"{z}->{facilities[chain[z][0]][1]}" for z in g[:10]))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the map build over extras
        print(f"::warning::read_labeling.py failed: {e}")
    sys.exit(0)
