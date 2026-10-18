#!/usr/bin/env python
"""Turn the CAIC US avalanche accident spreadsheet into data for the map page.

Reads the "Data" sheet of the Colorado Avalanche Information Center's accident
workbook, tidies it, links each avalanche to its accident report where one is
published (raw/reports_caic.json and raw/reports_uac.json, from fetch_reports.py),
and writes it into the page between its <script id="accident-data"> tags, so the
page stays one self-contained file.

Usage:
    build_data.py [workbook.xlsx] [page.html]

Both arguments are optional. By default it reads the newest .xlsx in raw/ (by the
month and year in its name) and
writes tools/maps_site/avalanche-deaths/index.html. Each October CAIC publishes
an updated workbook: put it in raw/ and run this again.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from report_links import match

HERE = Path(__file__).resolve().parent
DEFAULT_PAGE = HERE.parent / "maps_site" / "avalanche-deaths" / "index.html"

# CAIC's 24 activity types, folded into 7 groups for the charts. Every type in
# the workbook must be listed here; the script stops if it finds a new one.
GROUPS = {
    "Backcountry tourer": [
        "Backcountry Tourer", "Hybrid Tourer", "Mechanized Guided Client", "Human-Powered Guided Client",
    ],
    "Snowmobiler": ["Snowmobiler", "Snowbiker"],
    "Climber": ["Climber"],
    "Ski area rider": ["Sidecountry Rider", "Inbounds Rider", "Hybrid Rider"],
    "Hiker & other recreation": ["Hiker", "Snowplayer", "Hunter", "Misc Recreation"],
    "At work": [
        "Ski Patroller", "Others at Work", "Highway Personnel", "Mechanized Guide", "Human-Powered Guide",
        "Miner", "Rescuer", "Ranger",
    ],
    "Resident or motorist": ["Resident", "Motorist"],
}
GROUP_OF = {activity: i for i, names in enumerate(GROUPS.values()) for activity in names}

# Older records spell places in capitals ("ARAPAHOE BASIN OB"). These short
# codes stay in capitals when the rest of the name is changed to Title Case.
KEEP_UPPER = {"OB", "NP", "RMNP", "USFS", "NF", "MP", "NE", "NW", "SE", "SW", "II", "III"}


def tidy_place(name: str) -> str:
    name = " ".join(str(name).split())
    if name != name.upper():
        return name  # newer records are already in normal case
    return re.sub(r"[A-Za-z']+", lambda m: m[0] if m[0] in KEEP_UPPER else m[0].capitalize(), name)


def edition(path: Path) -> tuple:
    """Sort key for CAIC file names like CAIC_Accident_Data_Oct_2025.xlsx: (year, month)."""
    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    m = re.search(r"_([A-Za-z]{3})[a-z]*_(\d{4})", path.stem)
    if not m:
        return (0, 0, path.name)
    month = m[1].lower()
    return (int(m[2]), months.index(month) + 1 if month in months else 0, path.name)


def tidy_text(value):
    if pd.isna(value):
        return None
    text = " ".join(str(value).split())
    return text or None


def report_links(df, ordered):
    """For each row (in page order), the accident reports published for it: [[source, url], ...]."""
    rows = [{
        "i": r.Index, "state": r.State.strip(), "date": r.Date.date(), "killed": int(r.Killed), "loc": str(r.Location),
        "lat": r.lat if r.lat != 0 and r.lon != 0 else None, "lon": r.lon if r.lat != 0 and r.lon != 0 else None,
        "desc": tidy_text(r.Description) or "",
    } for r in df.itertuples()]
    links = {r.Index: [] for r in df.itertuples()}
    for source in ("caic", "uac"):
        path = HERE / "raw" / f"reports_{source}.json"
        if not path.exists():
            print(f"No {path.name}; run fetch_reports.py to link {source.upper()} reports.", file=sys.stderr)
            continue
        reports = json.loads(path.read_text(encoding="utf-8"))
        for r in reports:
            r["date"] = date.fromisoformat(r["date"])
            r.setdefault("words", [])
            r.setdefault("inv", "")
            r.setdefault("lat", None)
            r.setdefault("lon", None)
        url = {r["id"]: r["url"] for r in reports}
        found = match([dict(row) for row in rows], reports)
        for i, rid in found.items():
            links[i].append([source, url[rid]])
        print(f"Reports:   {len(found)} rows linked to {len(set(found.values()))} of {len(reports)} {source.upper()} reports")
    return [links[i] or None for i in ordered]


def main() -> int:
    if len(sys.argv) > 3:
        print(__doc__, file=sys.stderr)
        return 1
    if len(sys.argv) > 1:
        workbook = Path(sys.argv[1])
    else:
        found = sorted((HERE / "raw").glob("*.xlsx"), key=edition)
        if not found:
            print("No .xlsx file in raw/. Pass the workbook path.", file=sys.stderr)
            return 1
        workbook = found[-1]
    page = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PAGE

    df = pd.read_excel(workbook, sheet_name="Data")
    unknown = sorted(set(df["PrimaryActivity"]) - set(GROUP_OF))
    if unknown:
        print(f"New activity types, add them to GROUPS: {unknown}", file=sys.stderr)
        return 2

    ordered = df.sort_values("Date", ascending=False)
    reports = report_links(df, list(ordered.index))
    rows = []
    for r, links in zip(ordered.itertuples(), reports):
        # Accidents without a recorded spot have lat = lon = 0.
        located = r.lat != 0 and r.lon != 0
        travel = tidy_text(r.TravelMode)
        rows.append([
            int(r.AvyYear),                       # season, named by its spring: 2025 = winter 2024-25
            r.Date.strftime("%Y-%m-%d"),
            tidy_place(r.Location),
            r.State.strip(),
            round(float(r.lat), 4) if located else None,
            round(float(r.lon), 4) if located else None,
            GROUP_OF[r.PrimaryActivity],
            r.PrimaryActivity,
            travel.capitalize() if travel else None,
            int(r.Killed),
            tidy_text(r.Description),
            links,                                # [[source, url], ...] or None
        ])

    data = {
        "source": f"Colorado Avalanche Information Center (CAIC), {workbook.name}",
        "fields": ["season", "date", "location", "state", "lat", "lon", "group", "activity", "travel",
                   "killed", "description", "reports"],
        "groups": list(GROUPS),
        "rows": rows,
    }
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    html = page.read_text(encoding="utf-8")
    pattern = re.compile(r'(<script id="accident-data" type="application/json">).*?(</script>)', re.S)
    if not pattern.search(html):
        print(f'{page} has no <script id="accident-data"> block.', file=sys.stderr)
        return 2
    page.write_text(pattern.sub(lambda m: m[1] + blob + m[2], html, count=1), encoding="utf-8")

    located = sum(1 for row in rows if row[4] is not None)
    print(f"Workbook:  {workbook.name}")
    print(f"Accidents: {len(rows)}  ({located} with map coordinates)")
    print(f"Killed:    {sum(row[9] for row in rows)}")
    print(f"Linked:    {sum(1 for row in rows if row[11])} accidents have a report link")
    first, last = min(row[0] for row in rows), max(row[0] for row in rows)
    print(f"Winters:   {first - 1}-{str(first)[2:]} to {last - 1}-{str(last)[2:]}")
    print(f"Wrote:     {page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
