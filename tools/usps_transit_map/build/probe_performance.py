#!/usr/bin/env python3
"""Probe USPS on-time performance sources and print what they contain.

A one-off helper: the USPS sites can't be opened from the development
sandbox, so this runs in the GitHub Action and prints the structure of
each source (tables, column names, sample rows, links) to the log. The
output is used to write the real parser. It never fails the build.
"""

import io
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "raw" / "perf"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
}
BASE = "https://about.usps.com/what/performance/service-performance/"
PRODUCTS = ["single-piece-first-class-mail", "presort-first-class-mail", "marketing-mail",
            "periodicals", "package-services"]


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers.get_content_type(), r.geturl()


def show_tables(name, body):
    try:
        tables = pd.read_html(io.StringIO(body.decode("utf-8", errors="replace")))
    except ValueError:
        tables = []
    print(f"  {name}: {len(tables)} HTML tables")
    for i, t in enumerate(tables[:6]):
        cols = [" | ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
        print(f"   table {i}: {t.shape[0]} rows x {t.shape[1]} cols")
        print(f"     columns: {cols}")
        with pd.option_context("display.width", 250, "display.max_columns", 20):
            print("     " + t.head(4).to_string().replace("\n", "\n     "))


def try_url(url, save_as=None):
    try:
        body, ctype, final = get(url)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"- {url}: {e}")
        return None
    print(f"+ {url} -> {final} ({ctype}, {len(body):,} bytes)")
    if save_as:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / save_as).write_bytes(body)
    return body, ctype


def main():
    print("=== District <-> 3-digit ZIP list ===")
    r = try_url(BASE + "zip-3-by-area-district.htm", "zip3_district.html")
    if r and r[1] == "text/html":
        show_tables("zip-3-by-area-district", r[0])

    print("\n=== PostalPro area/district ZIP assignment files ===")
    for page in ("https://postalpro.usps.com/address-quality/city-state-product",
                 "https://postalpro.usps.com/ZIP_Locale_Detail"):
        r = try_url(page)
        if r:
            html = r[0].decode("utf-8", errors="replace")
            for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
                t = re.sub(r"<[^>]+>|\s+", " ", text).strip()
                if re.search(r"area|dist|locale|\.zip|\.txt|\.xls", href + t, re.I):
                    print(f"   {t[:80]!r} -> {href}")

    print("\n=== Quarterly service variance reports (newest first) ===")
    found_q = None
    for fy in (2026, 2025):
        for q in (4, 3, 2, 1):
            for prod in PRODUCTS:
                for ext in ("html", "htm", "pdf"):
                    url = f"{BASE}fy{fy}-q{q}-{prod}-service-variance.{ext}"
                    r = try_url(url, f"fy{fy}-q{q}-{prod}.{ext}")
                    if r:
                        found_q = found_q or (fy, q)
                        if r[1] == "text/html":
                            show_tables(f"FY{fy} Q{q} {prod}", r[0])
                        break
            if found_q:
                break
        if found_q:
            break
    print(f"\nNewest quarter found: {found_q}")

    print("\n=== Service performance landing page links ===")
    r = try_url(BASE)
    if r:
        html = r[0].decode("utf-8", errors="replace")
        links = sorted(set(re.findall(r'href="([^"]+)"', html)))
        for l in links:
            if re.search(r"variance|perform|district|xls|csv|fy20", l, re.I):
                print("   ", l)

    print("\n=== spm.usps.com dashboard ===")
    r = try_url("https://spm.usps.com/")
    if r:
        html = r[0].decode("utf-8", errors="replace")
        print("   scripts:", re.findall(r'<script[^>]+src="([^"]+)"', html)[:20])
        for m in sorted(set(re.findall(r'["\'](/?[A-Za-z0-9_\-/]*(?:api|json|data|download)[A-Za-z0-9_\-/.?=]*)["\']', html, re.I)))[:40]:
            print("   candidate:", m)
        for src in re.findall(r'<script[^>]+src="([^"]+)"', html)[:8]:
            url = src if src.startswith("http") else "https://spm.usps.com/" + src.lstrip("/")
            s = try_url(url)
            if s:
                js = s[0].decode("utf-8", errors="replace")
                hits = sorted(set(re.findall(r'["\'`](https?://[^"\'`]+|/[A-Za-z0-9_\-/]*(?:api|Api|API)[A-Za-z0-9_\-/.]*)["\'`]', js)))
                print(f"   {src}: {len(hits)} url-ish strings:", hits[:40])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the build over the probe
        print(f"probe failed: {e}")
    sys.exit(0)
