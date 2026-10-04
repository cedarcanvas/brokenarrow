#!/usr/bin/env python3
"""Find and download the USPS service-standard files from PostalPro.

Opens the PostalPro service-standards page, collects the download links it
lists (and links on PostalPro pages it points to), keeps the ones that look
like current 3-digit service-standard files, and saves them in raw/usps/.
Every link is printed with the reason it was kept or skipped, so the GitHub
Action log shows exactly what happened.

Usage (from tools/usps_transit_map/):
  python build/fetch_usps.py               # find and download
  python build/fetch_usps.py --dry-run     # only list what it would download
  python build/fetch_usps.py --url https://postalpro.usps.com/.../file.zip
  python build/fetch_usps.py --include-5digit

Uses only the Python standard library. Always exits 0 so the build can fall
back to demo days if USPS changes its page or blocks the request.
"""

import argparse
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

APP = Path(__file__).resolve().parent.parent

START_PAGES = [
    "https://postalpro.usps.com/operations/service-standards",
    "https://postalpro.usps.com/service-standards",
]
FILE_EXT = (".zip", ".txt", ".csv", ".xlsx", ".xls")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Words that mean "not the file we want" (checked against link text + URL).
SKIP_WORDS = {
    "proposed": "a proposed (not yet in effect) file",
    "preview": "a preview of future standards",
    "layout": "a file-layout document, not data",
    "user guide": "a guide, not data",
    "guide": "a guide, not data",
    "map": "a map, not data",
    "intl": "international mail",
    "international": "international mail",
    "change": "a list of changes, not the full table",
    "destination entry": "destination-entry (drop-ship) standards, not origin-destination",
    "3d base": "3-digit base file - the Combined files already include it",
    "pfc": "product not shown on the map (PFC)",
    "pkg": "Package Services - not shown on the map",
}
FIVE_DIGIT = ("5-digit", "5 digit", "5digit", "five-digit", "suppl", "supplement", "zip5", "5dig")
WANT_WORDS = ("service standard", "svc std", "ssd", "standard", "orig", "dest",
              "first-class", "first class", "fcm", "priority", "ground", "marketing", "periodical",
              "package", "mkt", "per")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = [dict(attrs).get("title") or ""]

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
        if tag == "a":
            self._href = None


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get_content_type(), r.geturl()


def page_links(url):
    body, ctype, final = get(url)
    p = Links()
    p.feed(body.decode("utf-8", errors="replace"))
    return [(urllib.parse.urljoin(final, h), t) for h, t in p.links]


def is_file(url):
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(FILE_EXT) or "/file/" in path or "download" in path


def quarter(text):
    """'FY2027 Q1' style label found in text, as a sortable tuple and a label."""
    m = re.search(r"FY\s?'?(\d{2,4})[\s_-]*Q(?:tr|uarter)?\s?([1-4])", text, re.I) \
        or re.search(r"Q([1-4])[\s_-]*FY\s?'?(\d{2,4})", text, re.I)
    if not m:
        return None
    a, b = m.groups()
    yr, q = (a, b) if m.re.pattern.startswith("FY") else (b, a)
    yr = int(yr) + (2000 if len(yr) == 2 else 0)
    return (yr, int(q)), f"FY{yr} Q{q}"


def effective_date(text):
    """'..._10012026_...' (MMDDYYYY) -> sortable tuple and 'Effective Oct 1, 2026'."""
    m = re.search(r"(?<!\d)(0[1-9]|1[0-2])([0-2]\d|3[01])(20\d\d)(?!\d)", text)
    if not m:
        return None
    mm, dd, yy = (int(x) for x in m.groups())
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return (yy, mm, dd), f"Effective {months[mm - 1]} {dd}, {yy}"


def judge(url, text, include5):
    hay = f"{text} {urllib.parse.unquote(url)}".lower().replace("_", " ")
    if hay.strip().endswith(".pdf") or ".pdf" in urllib.parse.urlparse(url).path.lower():
        return False, "a PDF"
    for w, why in SKIP_WORDS.items():
        if re.search(rf"(^|[^a-z]){re.escape(w)}([^a-z]|$)", hay):
            return False, why
    if not include5 and any(w in hay for w in FIVE_DIGIT):
        return False, "5-digit supplemental file (use --include-5digit to keep)"
    if not any(w in hay for w in WANT_WORDS):
        return False, "doesn't look like a service-standard file"
    return True, "looks like a service-standard data file"


def safe_name(url, text):
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    if not name.lower().endswith(FILE_EXT):
        name = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:80] or "usps_file"
    return name


def download(url, name, out):
    data, ctype, _ = get(url, timeout=300)
    if ctype == "text/html" or data[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        print(f"    ! {url} returned a web page, not a file - skipped")
        return None
    if not Path(name).suffix:
        name += ".zip" if data[:2] == b"PK" else ".txt"
    dest = out / name
    dest.write_bytes(data)
    print(f"    saved {dest}  ({len(data) / 1e6:.1f} MB)")
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=APP / "raw" / "usps", type=Path)
    ap.add_argument("--start", action="append", help="page to start from (default: PostalPro service standards)")
    ap.add_argument("--url", action="append", help="download this exact file link (skips the page search)")
    ap.add_argument("--include-5digit", action="store_true", help="also download 5-digit supplemental files")
    ap.add_argument("--dry-run", action="store_true", help="list what would be downloaded, download nothing")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    candidates = []  # (url, text)
    if args.url:
        candidates = [(u, "") for u in args.url]
    else:
        seen_pages, files = set(), {}
        for start in args.start or START_PAGES:
            try:
                links = page_links(start)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f"! could not open {start}: {e}")
                continue
            print(f"Opened {start}: {len(links)} links")
            seen_pages.add(start)
            sub = []
            for u, t in links:
                host = urllib.parse.urlparse(u).netloc
                if is_file(u):
                    files.setdefault(u, t)
                elif (host.endswith("usps.com") or host == urllib.parse.urlparse(start).netloc) and u not in seen_pages and re.search(
                        r"service.?standard|ssd|svc.?std|orig.?svc", f"{u} {t}", re.I):
                    sub.append(u)
            for u in dict.fromkeys(sub):
                if u in seen_pages:
                    continue
                seen_pages.add(u)
                time.sleep(1)
                try:
                    more = page_links(u)
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    print(f"  ! could not open {u}: {e}")
                    continue
                n = 0
                for u2, t2 in more:
                    if is_file(u2):
                        files.setdefault(u2, t2); n += 1
                print(f"  followed {u}: {n} file links")
            if files:
                break
        if not files:
            print("::warning::No USPS download links found. The page may have changed or blocked the "
                  "request. Download the files by hand into raw/usps/ (see raw/usps/README.md), "
                  "or run fetch_usps.py --url <link>.")
            return
        print(f"\n{len(files)} file links found:")
        for u, t in files.items():
            ok, why = judge(u, t, args.include_5digit)
            print(f"  [{'keep' if ok else 'skip'}] {t or '(no text)'}  <{u}>  - {why}")
            if ok:
                candidates.append((u, t))

        # Several quarters listed? Keep only the newest one.
        qs = {u: quarter(f"{t} {urllib.parse.unquote(u)}") for u, t in candidates}
        if not any(qs.values()):  # PostalPro names files by effective date instead
            qs = {u: effective_date(f"{t} {urllib.parse.unquote(u)}") for u, t in candidates}
        dated = [q for q in qs.values() if q]
        if dated:
            newest = max(dated)[0]
            label = max(dated)[1]
            dropped = [u for u, q in qs.items() if q and q[0] != newest]
            candidates = [(u, t) for u, t in candidates if not qs[u] or qs[u][0] == newest]
            print(f"\nNewest data listed: {label}" + (f" (skipping {len(dropped)} older files)" if dropped else ""))
            if not args.dry_run:
                (args.out / "vintage.txt").write_text(label + "\n")

    if not candidates:
        print("::warning::Found links, but none looked like current 3-digit service-standard files. "
              "Check the list above; rerun with --url <link> to pick one.")
        return
    if args.dry_run:
        print("\nDry run - would download:")
        for u, t in candidates:
            print(f"  {t or u}")
        return

    print("\nDownloading:")
    got = 0
    for u, t in candidates:
        try:
            if download(u, safe_name(u, t), args.out):
                got += 1
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"    ! {u}: {e}")
        time.sleep(1)
    print(f"{got} of {len(candidates)} files downloaded into {args.out}/")
    if not got:
        print("::warning::No USPS files could be downloaded; the build will use demo days.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the build over the download step
        print(f"::warning::fetch_usps.py failed: {e}")
    sys.exit(0)
