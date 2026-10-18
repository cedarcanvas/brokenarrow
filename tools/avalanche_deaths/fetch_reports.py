#!/usr/bin/env python
"""Download the accident reports the map links to, into raw/ for build_data.py.

- raw/reports_caic.json: every report with at least one death in CAIC's US accident
  database (the list avalanche.org's accident page shows), from CAIC's public API.
- raw/reports_uac.json: the Utah Avalanche Center's fatality list. Its website blocks
  scripts, so open https://utahavalanchecenter.org/avalanches/fatalities in your browser,
  save it (File > Save Page As) as raw/uac_fatalities.html, and run this. Without that
  file the existing reports_uac.json is kept.

Usage:
    fetch_reports.py
"""
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
CAIC_API = "https://api.avalanche.state.co.us/api/v2/reports"
UAC_PAGE = RAW / "uac_fatalities.html"

# Hours behind UTC (standard time) for turning CAIC's timestamps into local dates.
UTC_OFFSET = {"AK": 9, "WA": 8, "OR": 8, "CA": 8, "NV": 8, "NH": 5, "NY": 5, "ME": 5, "VT": 5}
# The UAC's forecast regions, as place words that older, terse locations often use ("LOGAN CANYON").
UAC_REGION_WORDS = {"Logan": ["logan"], "Ogden": ["ogden"], "Provo": ["provo"], "Uintas": ["uinta", "uintas"],
                    "Skyline": ["skyline"], "Moab": ["moab"], "Abajos": ["abajo", "abajos"]}
# Capitalized words in a narrative are mostly place names; they help match old, terse locations.
PROPER = re.compile(r"\b[A-Z][A-Za-z']{2,}")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Ridgeline Maps avalanche map (ridgelinemaps.com)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read()), int(resp.headers.get("total-pages") or 1)


def fetch_caic():
    params = [
        ("r[observed_at_gteq]", "1950-09-01"), ("r[involvement_summary_killed_gteq]", "1"),
        ("r[is_locked_eq]", "false"), ("r[status_eq]", "approved"),
        ("r[type_in][]", "incident_report"), ("r[type_in][]", "accident_report"),
        ("r[sorts][]", "observed_at desc"), ("per", "100"),
    ]
    reports, page, pages = [], 1, 1
    while page <= pages:
        batch, pages = get_json(CAIC_API + "?" + urllib.parse.urlencode(params + [("page", str(page))]))
        reports += batch
        print(f"  CAIC page {page} of {pages}: {len(batch)} reports")
        page += 1

    out = []
    for r in reports:
        d = r.get("public_report_detail") or {}
        state = r.get("state") or ""
        when = datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00")) - timedelta(hours=UTC_OFFSET.get(state, 7))
        narrative = " ".join(str(d.get(f) or "") for f in ("accident_summary", "lead_up_events", "rescue_summary", "comments"))
        out.append({
            "id": r["id"],
            "url": r["related_report_links"]["external_canonical_report"],
            "type": r["type"],
            "state": state,
            "date": when.date().isoformat(),
            "killed": (r.get("involvement_summary") or {}).get("killed"),
            "loc": d.get("location") or r.get("landmark") or "",
            "lat": r.get("latitude"),
            "lon": r.get("longitude"),
            "inv": (d.get("involvement_summary") or "").strip(),
            "words": sorted({w.lower() for w in PROPER.findall(narrative)}),
        })
    return out


def parse_uac(page_html):
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S):
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        link = re.search(r'href="(?:https://utahavalanchecenter\.org)?(/avalanche/\d+)"', row)
        if len(cells) < 5 or not link or not re.match(r"\d+/\d+/\d{4}$", cells[0]):
            continue
        m, d, y = (int(v) for v in cells[0].split("/"))
        out.append({
            "id": link[1].rsplit("/", 1)[1],
            "url": "https://utahavalanchecenter.org" + link[1],
            "state": "ID" if cells[1] == "SE Idaho" else "UT",
            "date": f"{y:04d}-{m:02d}-{d:02d}",
            "killed": int(cells[4]) if cells[4].isdigit() else None,
            "loc": re.sub(r"^Accident:\s*", "", cells[2]),
            "region": cells[1],
            "words": UAC_REGION_WORDS.get(cells[1], []),
        })
    return out


def main() -> int:
    if len(sys.argv) > 1:
        print(__doc__, file=sys.stderr)
        return 1
    print("CAIC US accident database:")
    caic = fetch_caic()
    (RAW / "reports_caic.json").write_text(json.dumps(caic, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"  saved {len(caic)} fatal reports to raw/reports_caic.json")

    print("Utah Avalanche Center:")
    if UAC_PAGE.exists():
        uac = parse_uac(UAC_PAGE.read_text(encoding="utf-8", errors="replace"))
        if not uac:
            print(f"  {UAC_PAGE.name} has no fatality table. Save the page again from your browser.", file=sys.stderr)
            return 2
        (RAW / "reports_uac.json").write_text(json.dumps(uac, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"  saved {len(uac)} fatal accidents to raw/reports_uac.json")
    else:
        print(f"  no {UAC_PAGE.name}; keeping the existing raw/reports_uac.json (see the note at the top of this script)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
