#!/usr/bin/env python3
"""Probe: can USPS processing-plant areas be inferred from the delivery days?

USPS sets delivery days plant to plant, so ZIP prefixes served by the same
plant should have the same days to (and from) everywhere. This groups the
prefixes whose rows and columns of days match, then prints what it found:
how many groups, how big, how compact on the map, and how the groups link
up at the fastest standard. It only prints; nothing on the map changes.

Usage (from tools/usps_transit_map/):  python build/probe_network.py
Never fails the build.
"""

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

APP = Path(__file__).resolve().parent.parent
DATA = APP / "web/data"


def km(a, b):
    (lon1, lat1), (lon2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def main():
    meta = json.loads((DATA / "meta.json").read_text())
    zip3 = json.loads((DATA / "zip3.json").read_text())
    topo = json.loads((DATA / "zcta.topo.json").read_text())
    N = len(zip3)
    counts = np.zeros(N, int)
    for g in topo["objects"]["zcta"]["geometries"]:
        counts[g["properties"]["i"]] += 1
    live = np.where(counts > 0)[0]
    hubs = json.loads((DATA / "hubs.json").read_text())["h"] if (DATA / "hubs.json").exists() else []
    plants = [h for h in hubs if h[1] == "P"]
    print(f"Probe: inferred plant network ({len(live)} prefixes with ZIP shapes, {len(plants)} plants on file)")

    for c in meta["classes"]:
        k = c["key"]
        if k not in ("fcm", "ga", "pm"):
            continue
        D = np.fromfile(DATA / f"days_{k}.bin", dtype=np.uint8).reshape(N, N)
        sub = D[np.ix_(live, live)]
        # Exact groups: same days to everyone AND from everyone.
        keys = {}
        group_of = {}
        for a, i in enumerate(live):
            key = (sub[a].tobytes(), sub[:, a].tobytes())
            group_of[i] = keys.setdefault(key, len(keys))
        G = len(keys)
        members = defaultdict(list)
        for i, g in group_of.items():
            members[g].append(i)
        sizes = Counter(len(m) for m in members.values())
        # Near groups: rows that differ from another row in under 2% of destinations.
        near = 0
        rows = sub.astype(np.int16)
        for a in range(len(live)):
            diff = (rows != rows[a]).mean(1)
            diff[a] = 1
            if diff.min() < 0.02:
                near += 1
        # Compactness: farthest member from the group's middle, in km.
        spread = []
        for m in members.values():
            pts = [zip3[i]["c"] for i in m if zip3[i].get("c")]
            if len(pts) < 2:
                continue
            mid = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            spread.append(max(km(mid, p) for p in pts))
        spread.sort()
        print(f"\n{c['label']} ({k}): {G} exact groups from {len(live)} prefixes")
        print(f"  group sizes (prefixes: groups): {dict(sorted(sizes.items()))}")
        print(f"  prefixes with a near-twin (<2% of destinations differ): {near}")
        if spread:
            q = lambda p: spread[min(len(spread) - 1, int(p * len(spread)))]
            print(f"  multi-prefix group radius km: median {q(.5):.0f}, 90th pct {q(.9):.0f}, max {spread[-1]:.0f}")
        big = sorted(members.values(), key=len, reverse=True)[:8]
        for m in big:
            print(f"  e.g. {len(m)} prefixes: " + ", ".join(f"{zip3[i]['z']}({zip3[i].get('s', '')[:12]})" for i in m[:12]))
        # Loose groups: a prefix joins the first group whose first member's days
        # (to and from everyone) differ in under t of the entries.
        order = sorted(range(len(live)), key=lambda a: -counts[live[a]])
        for t in (0.01, 0.03, 0.05, 0.10):
            reps_l, sizes_l, spread_l = [], [], []
            mem_l = []
            for a in order:
                for gi, r in enumerate(reps_l):
                    if (rows[a] != rows[r]).mean() < t and (rows[:, a] != rows[:, r]).mean() < t:
                        mem_l[gi].append(a)
                        break
                else:
                    reps_l.append(a)
                    mem_l.append([a])
            for m in mem_l:
                pts = [zip3[live[a]]["c"] for a in m if zip3[live[a]].get("c")]
                if len(pts) > 1:
                    mid = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                    spread_l.append(max(km(mid, p) for p in pts))
            spread_l.sort()
            sz = sorted((len(m) for m in mem_l), reverse=True)
            med = spread_l[len(spread_l) // 2] if spread_l else 0
            print(f"  within {t:.0%}: {len(mem_l)} groups; largest {sz[:6]}; singletons {sz.count(1)}; "
                  f"radius km median {med:.0f}, max {spread_l[-1] if spread_l else 0:.0f}")
            if t == 0.05:
                for m in sorted(mem_l, key=len, reverse=True)[:5]:
                    print("    e.g. " + ", ".join(zip3[live[a]]["z"] for a in m[:15]))
        # Links between groups at the fastest standard in this class.
        reps = {g: m[0] for g, m in members.items()}
        gl = sorted(reps)
        R = D[np.ix_([reps[g] for g in gl], [reps[g] for g in gl])]
        vals = R[R > 0]
        fastest = int(vals.min()) if vals.size else 0
        adj = (R == fastest)
        np.fill_diagonal(adj, False)
        deg = adj.sum(1)
        print(f"  fastest standard between different groups: {fastest} days; "
              f"links at that speed: {int(adj.sum() // 2)}; neighbours per group: "
              f"median {int(np.median(deg))}, max {int(deg.max())}, none {int((deg == 0).sum())}")
        days_hist = Counter(int(v) for v in R[np.triu_indices(len(gl), 1)] if v)
        print(f"  group-to-group days: {dict(sorted(days_hist.items()))}")
        # How the groups line up with plants on file (nearest plant to each group's middle).
        if plants:
            near_plant = Counter()
            for m in members.values():
                pts = [zip3[i]["c"] for i in m if zip3[i].get("c")]
                if not pts:
                    continue
                mid = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                best = min(plants, key=lambda h: km(mid, (h[4], h[5])))
                near_plant[best[0]] += 1
            print(f"  distinct nearest plants: {len(near_plant)} for {G} groups; "
                  f"plants shared by 2+ groups: {sum(1 for v in near_plant.values() if v > 1)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a probe never breaks the build
        print(f"::warning::probe_network.py failed: {e}")
    sys.exit(0)
