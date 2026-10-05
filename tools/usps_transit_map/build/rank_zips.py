#!/usr/bin/env python3
"""Rank ZIP prefixes by USPS delivery days and write web/data/rank.json.

For every mail class and every 3-digit ZIP prefix:
  send  average USPS delivery days from that prefix to every ZIP code in the
        country (each destination ZIP counts once), and the share reached
        within a "fast" number of days (3 for letters, 2 for Priority Mail...)
  recv  the same, the other way: average days for mail from every ZIP code
        in the country to reach that prefix
Lists the 5 fastest and 5 slowest prefixes both ways, twice: for the whole
country, and for the lower 48 only (counting only lower-48 ZIP codes at both
ends, so long trips to Alaska, Hawaii and the territories don't weigh in).

Reads what build_data.py wrote (meta.json, zip3.json, zcta.topo.json,
days_*.bin) and, if present, zipnames.json for city names.

Usage (from tools/usps_transit_map/):  python build/rank_zips.py
Never fails the build.
"""

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

APP = Path(__file__).resolve().parent.parent
DATA = APP / "web/data"
TOP = 5
# "Fast" cut-off per class, for the share-of-ZIPs figure: about the quickest
# common USPS target for that class.
FAST = {"fcm": 3, "ga": 3, "pm": 2, "mkt": 5, "per": 4}


def prefix_names(zip3, counts):
    """'802' -> 'Denver, CO': the most common post office city in the prefix."""
    names = {}
    zn = DATA / "zipnames.json"
    if zn.exists():
        z = json.loads(zn.read_text())
        by3 = {}
        for code, idx in z["z"].items():
            by3.setdefault(code[:3], Counter())[idx] += 1
        for k, cnt in by3.items():
            c, st = z["c"][cnt.most_common(1)[0][0]]
            names[k] = f"{c}, {st}" if st else c
    return [names.get(r["z"], r.get("s") or "") for r in zip3]


def main():
    meta = json.loads((DATA / "meta.json").read_text())
    zip3 = json.loads((DATA / "zip3.json").read_text())
    topo = json.loads((DATA / "zcta.topo.json").read_text())
    N = len(zip3)
    counts = np.zeros(N)
    for g in topo["objects"]["zcta"]["geometries"]:
        counts[g["properties"]["i"]] += 1
    names = prefix_names(zip3, counts)
    lower48 = np.array([r.get("p", 0) == 0 for r in zip3])
    has = counts > 0

    out = {"demo": bool(meta.get("demo")), "classes": {}}
    for c in meta["classes"]:
        k = c["key"]
        days = np.fromfile(DATA / f"days_{k}.bin", dtype=np.uint8).reshape(N, N).astype(float)
        ok = days > 0
        fast = FAST.get(k, 3)

        def view(cnt):
            """Rankings counting only the ZIP codes in cnt (all, or lower 48)."""
            w = cnt[None, :] * ok         # weight of each destination ZIP
            send = (days * w).sum(1) / np.maximum(w.sum(1), 1)
            sendf = ((days <= fast) * w).sum(1) / np.maximum(w.sum(1), 1)
            wr = cnt[:, None] * ok        # weight of each origin ZIP
            recv = (days * wr).sum(0) / np.maximum(wr.sum(0), 1)
            recvf = ((days <= fast) * wr).sum(0) / np.maximum(wr.sum(0), 1)
            vs, vr = (cnt > 0) & (w.sum(1) > 0), (cnt > 0) & (wr.sum(0) > 0)
            nat = float((send[vs] * cnt[vs]).sum() / cnt[vs].sum())

            def pick(vals, share, valid, slow):
                idx = np.where(valid)[0]
                # Fastest: fewest average days, then most ZIPs reached fast.
                key = (-vals[idx], share[idx]) if slow else (vals[idx], -share[idx])
                order = idx[np.lexsort(key[::-1])][:TOP]
                return [[zip3[i]["z"], names[i], round(float(vals[i]), 2), round(float(share[i]) * 100, 1)]
                        for i in order]

            return {"national": round(nat, 2),
                    "send": {"fast": pick(send, sendf, vs, False), "slow": pick(send, sendf, vs, True)},
                    "recv": {"fast": pick(recv, recvf, vr, False), "slow": pick(recv, recvf, vr, True)}}

        r = out["classes"][k] = {"label": c["label"], "fast_days": fast,
                                 "all": view(counts), "l48": view(counts * lower48)}
        for v, vname in (("all", "all U.S."), ("l48", "lower 48 only")):
            print(f"\n{c['label']} ({vname}): national average {r[v]['national']:.2f} days")
            for way, title in (("send", "mailing FROM"), ("recv", "receiving AT")):
                for lst in ("fast", "slow"):
                    rows = "; ".join(f"{z}xx {n} {d:.2f}d ({p:.0f}% in {fast}d)" for z, n, d, p in r[v][way][lst])
                    print(f"  {lst}est {title}: {rows}")

    (DATA / "rank.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"\nwrote rank.json ({(DATA / 'rank.json').stat().st_size / 1e3:.0f} KB)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the map build over extras
        print(f"::warning::rank_zips.py failed: {e}")
    sys.exit(0)
