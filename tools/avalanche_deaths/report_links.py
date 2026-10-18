"""Match published accident reports to rows of the CAIC accident workbook.

A report and a row are taken to be the same avalanche when they are in the same state on the
same date (a day apart is fine), the number killed agrees, and the place names, coordinates or
the report's own narrative line up. Date and deaths alone are trusted only when that was the
state's only fatal avalanche that day. A second pass allows dates up to a week apart, and one
report covering several rows (older records sometimes list each victim on a separate row),
as long as the place matches, or the rows are all of that state's deaths that day, and nothing
else competes for them.
"""
import collections
import math
import re

STOP = set("""the of a an and at in on near nr north south east west n s e w ne nw se sw mt mount mtn mtns
              peak pk area above below side accident""".split())


def words(text):
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " ")) if t not in STOP and len(t) > 1}


def compact(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def km(a, b):
    if a.get("lat") is None or b.get("lat") is None:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def match(rows, reports):
    """Link reports to rows.

    rows:    dicts with i, state, date, killed, loc, lat, lon, desc
    reports: dicts with id, state, date, killed, loc, words (place words from the report), lat, lon, inv
    Returns {row i: report id}.
    """
    for s in rows:
        s["w"] = words(s["loc"])
    for r in reports:
        r["w"] = words(r["loc"])
    by_state = collections.defaultdict(list)
    for s in rows:
        by_state[s["state"]].append(s)
    day_rows = collections.Counter((s["state"], s["date"]) for s in rows)
    day_reports = collections.Counter((r["state"], r["date"]) for r in reports)

    def evidence(r, s):
        a, b = r["w"], s["w"]
        jac = len(a & b) / len(a | b) if a and b else 0  # shared place words
        if not jac and any(len(w) >= 5 and w in compact(s["loc"]) for w in a | {compact(r["loc"])}):
            jac = 0.25  # the same name written as one word or two ("Chokecherry", "CHOKE CHERRY")
        narr = len(b & set(r["words"])) / len(b) if b else 0  # row's place words found in the report
        dist = km(r, s)
        same_text = bool(s["desc"]) and s["desc"].lower().rstrip(".") == (r["inv"] or "").lower().rstrip(".")
        return jac, narr, dist, same_text

    pairs = []
    for r in reports:
        for s in by_state.get(r["state"], []):
            dd = abs((s["date"] - r["date"]).days)
            if dd > 1:
                continue
            jac, narr, dist, same_text = evidence(r, s)
            score = (3 if dd == 0 else 1) + 4 * jac + 2 * narr + (3 if same_text else 0)
            score += 2 if r["killed"] == s["killed"] else -2 if r["killed"] else 0
            if dist is not None:
                score += 3 if dist < 3 else 1.5 if dist < 15 else -1 if dist > 60 else 0
            place = jac > 0 or narr > 0 or same_text or (dist is not None and dist < 15)
            pairs.append((score, place, dd, r, s))

    links, used = {}, set()
    for score, place, dd, r, s in sorted(pairs, key=lambda p: -p[0]):
        if score < 4.5 or r["id"] in used or s["i"] in links:
            continue
        if not place and not (dd == 0 and day_rows[(s["state"], s["date"])] == 1 and day_reports[(r["state"], r["date"])] == 1):
            continue
        links[s["i"]] = r["id"]
        used.add(r["id"])

    # Second pass: a report that covers several rows from the same day and place, or a date up to a week off.
    linked_killed = collections.Counter()
    for i, rid in links.items():
        linked_killed[rid] += next(s["killed"] for s in rows if s["i"] == i)
    for r in reports:
        spare = (r["killed"] or 0) - linked_killed[r["id"]]
        if r["id"] in used and spare <= 0:
            continue
        cands = []
        for s in rows:
            if s["i"] in links:
                continue
            dd = abs((s["date"] - r["date"]).days)
            jac, narr, _, _ = evidence(r, s)
            if dd <= 7 and (s["state"] == r["state"] or jac >= 0.5) and (jac > 0 or narr > 0):
                cands.append((dd, s))
        same_day = [s for dd, s in cands if dd == 0 and s["state"] == r["state"]]
        whole_day = [s for s in by_state.get(r["state"], []) if s["date"] == r["date"]]
        if (len(whole_day) > 1 and day_reports[(r["state"], r["date"])] == 1 and r["id"] not in used
                and not any(s["i"] in links for s in whole_day) and sum(s["killed"] for s in whole_day) == r["killed"]):
            for s in whole_day:  # the only report that day accounts for all of the state's deaths that day
                links[s["i"]] = r["id"]
            used.add(r["id"])
            continue
        if r["id"] in used or len(same_day) > 1:
            # Extra victims of a report already linked, or one report for several same-day rows.
            if same_day and sum(s["killed"] for s in same_day) == (spare if r["id"] in used else r["killed"]):
                for s in same_day:
                    links[s["i"]] = r["id"]
                used.add(r["id"])
            continue
        single = [s for dd, s in cands if s["killed"] == r["killed"]]
        if len(single) == 1:
            links[single[0]["i"]] = r["id"]
            used.add(r["id"])
    return links
