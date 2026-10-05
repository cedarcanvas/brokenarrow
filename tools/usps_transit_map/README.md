# USPS Delivery Days map

A web map you can host. Hover over any ZIP code and every other ZIP code is
colored by how many days USPS expects mail to take from it: 1 day, 2 days,
3 days and so on. Switch between letters (First-Class Mail), Ground
Advantage, Priority Mail, Marketing Mail and Periodicals. Click a ZIP to pin
it, then hover other ZIPs to read exact days.

- **Projection:** Equal Earth (areas are true). Alaska, Hawaii, Puerto Rico &
  USVI, Guam & N. Mariana Is. and American Samoa sit in boxes below the
  lower 48, each at its own scale.
- **Hosting:** plain static files in `web/`, published by GitHub Pages through
  `.github/workflows/usps-transit-map.yml`. No server needed. The map lives
  at `/usps/`; the site root is a short list of maps from `tools/maps_site/`.
  For a custom domain (e.g. `maps.ridgelinemaps.com`): add a DNS CNAME record
  `maps` → `cedarcanvas.github.io`, then enter the domain under repo
  Settings → Pages → Custom domain and turn on Enforce HTTPS.
- **From / To boxes:** type two ZIP codes to get a trip card with the delivery
  days, the arrival weekday (when "Mailed on" is set) and the on-time record.
- **ZIP city names:** `build/fetch_zipnames.py` reads USPS's ZIP Code Locale
  Detail spreadsheet in the build and writes `web/data/zipnames.json`. The
  From / To boxes also accept city names. Without that file, ZIPs show the
  nearest labelled city ("near Denver").
- **Mail plants:** `build/fetch_hubs.py` reads USPS's Facility File in the
  build and writes `web/data/hubs.json`: processing plants (squares) and
  package network hubs (diamonds). The side panel shows the nearest plant to
  each ZIP. USPS doesn't publish which plant serves which ZIP, so this is the
  closest one, not necessarily the one your mail goes through. A checkbox
  under the legend hides them.
- **Network view:** the "Network" switch (bottom left of the map) groups ZIP
  prefixes whose First-Class days to and from everywhere are identical, most
  likely the area one processing plant serves, and draws each group as a
  dot. Hover a ZIP to see lines to every other plant area, colored by days;
  with nothing selected, lines join the areas with the fastest link. It is
  inferred from the published targets, not real truck routes.
  `build/probe_network.py` prints how cleanly the groups form in each build.
- **USPS sorting chain:** `build/read_labeling.py` reads the USPS labeling
  lists in `raw/labeling/` (L005 plant, L004 regional center, L601 package
  hub; from FAST, see the README there) and writes `web/data/sort.json`. The
  side panel shows each ZIP's chain, and the "USPS plants" map view groups
  ZIPs by their official plant and draws the chain as a magenta line. Upload
  newer lists to `raw/labeling/` to refresh it.
- **Side panel:** on-time meters at the top, then the hover/trip readout
  (it replaces the floating tooltip), then the legend with a distribution bar
  per class. Hover a legend row to spotlight those ZIPs.
- **City labels:** major cities on the full map, regional cities as you zoom
  in. They come from `web/cities.json`, made by `build/make_cities.py` from
  Natural Earth (public domain).
- **Colours:** ColorBrewer GnBu, 7 classes, light green (fast) to dark blue (slow),
  with grey for "no standard".
- **Sharing:** the address keeps the view, e.g. `…/#pm/80302/fri/10001` opens
  Priority Mail from 80302 to 10001, mailed on a Friday.

## Data sources

| What | Source | Notes |
|---|---|---|
| Delivery days | [USPS PostalPro – Service Standards](https://postalpro.usps.com/service-standards) | Free download. Days between every pair of 3-digit ZIP prefixes, per mail class. Updated every quarter. |
| ZIP shapes | [Census 2020 ZCTAs, `cb_2020_us_zcta520_500k.zip`](https://www2.census.gov/geo/tiger/GENZ2020/shp/) | Public domain. ~33,800 shapes. PO-box-only ZIPs have no shape. |
| Mail plants | [USPS PostalPro – Facility File](https://postalpro.usps.com/service-hubs-and-facilities/facilityfile) | Every USPS facility with type and ZIP. The current file is dated Feb 2019, so some plants may have changed. |
| State lines | [`us-atlas`](https://github.com/topojson/us-atlas) npm package (Census data) | ISC licence. |

**Limits worth knowing** (also shown on the page):

- Days are USPS *targets*, not guarantees.
- Days are looked up by 3-digit prefix, so all ZIPs in a prefix share a color.
  Since 2025 USPS adds one day for some ZIPs more than 50 miles from their
  regional plant, so a few ZIPs may really be a day slower than shown.
- Since 1 Oct 2026, Ground Advantage to/from Alaska, Hawaii and the territories
  is 10+ days (surface transport).

## Updating the data (automatic)

The GitHub Action runs `build/fetch_usps.py` on every build and once a month.
It opens the PostalPro service-standards page
(https://postalpro.usps.com/operations/service-standards), finds the download
links, and keeps the newest **Combined Service Standard Directory** file for
each mail class on the map (`_FCM`, `_GAH`/`_GAL` Ground Advantage, `_PRI`,
`_MKT`, `_PER`). Those list days for every 5-digit origin and destination ZIP;
the build folds them to 3-digit prefixes using the most common value. Files
go in `raw/usps/` with the effective date in `vintage.txt`. The build log lists
every link it found and why each was kept or skipped.

Files you commit to `raw/usps/` yourself always win over the automatic
download. If the download finds nothing (USPS changed the page or blocked the
request), the build falls back to **made-up demo days** and the page shows a
yellow "Demo data" banner. To fix that, either:

- commit the files by hand (see `raw/usps/README.md`), or
- point the script at one link: `python build/fetch_usps.py --url <link>`.

Run `python build/fetch_usps.py --dry-run` to see what it would download.

## On-time performance (how long mail really takes)

`build/fetch_performance.py` runs after the main build. It downloads USPS's
newest quarterly service performance reports from
https://about.usps.com/what/performance/service-performance/ and writes
`web/data/perf.json`. It also reads USPS's list of which 3-digit ZIP prefixes
belong to each district. The page then shows, for the sending and receiving
district, the share of mail delivered on time and within 1 and 3 extra days.

- **Letters:** Single-Piece First-Class Mail, by district and target (2-day or 3–5-day).
- **Marketing Mail:** end-to-end, by district.
- **Periodicals:** by area. USPS reports only 4 areas for Periodicals.
- **Priority Mail and Ground Advantage:** USPS publishes no regional results,
  so the page says so.

If the reports can't be read, the step only warns and the map works without them.

## Building on your own computer

You need Python 3.10+ and Node 18+.

```bash
cd tools/usps_transit_map
pip install -r build/requirements.txt        # geopandas etc.
(cd build && npm install)                    # mapshaper + us-atlas
curl -o raw/census/cb_2020_us_zcta520_500k.zip \
  https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip
python build/build_data.py                   # or add --demo-days to preview
cd web && python -m http.server 8000         # open http://localhost:8000
```

`build_data.py` prints what it found in each USPS file (which columns it used
for origin, destination, days and mail class) and warns about gaps. If it
can't work out the columns, add `raw/usps/columns.json`, for example:

```json
{ "origin": "ORIG_ZIP3", "dest": "DEST_ZIP3", "days": "SVC_STD_DAYS", "class": "MAIL_CLASS" }
```

Other keys: `"delimiter": "|"`, `"names": [...]` (for files with no header
row), `"file_classes": {"file.txt": "pm"}` and `"class_values": {"3": "fcm"}`.
Class keys are `fcm`, `ga`, `pm`, `mkt`, `per`. Files with 5-digit ZIPs are
fine too: the build uses the most common value for each prefix pair.

`build/make_demo_zcta.py` makes rough fake ZIP shapes from ZIP center points,
for testing without the Census download. Don't publish those.

## How it works

- `build/build_data.py` simplifies the ZIP shapes with mapshaper to TopoJSON
  (~6 MB, ~1.4 MB gzipped). It writes one ~900 × 900 table of days per mail
  class as raw bytes (`days_<class>.bin`, 0.8 MB each, loaded only when you pick
  that class).
- `web/app.js` uses D3 to draw on a `<canvas>`. Every ZIP is projected once
  into a `Path2D`. A repaint fills one combined shape per 3-digit prefix
  (~900 fills), so hovering stays fast. Hover lookups use a grid of bounding
  boxes plus `isPointInPath`.
- D3 v7.9.0 and topojson-client 3.1.0 are copied into `web/vendor/`, so the
  site has no outside dependencies.
