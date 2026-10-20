# Ski Day Receipt

A one-page web tool that turns what a ski season cost into an itemized,
thermal-paper style receipt: cost per day, per run, per lift ride, per
vertical foot and per hour, plus what the same money could have done instead.

Open `index.html` in a browser. It is a single self-contained file; the only
thing it loads from the internet is jsPDF (for the PDF download) and two
Google Fonts. Entries are remembered in the browser's local storage.

- **Inputs:** resort, season, currency; resort days and backcountry (no-lift)
  days with runs, lift rides, vertical and hours; a season pass or day
  tickets; gear (each item split across the seasons it lasts); travel and
  extras paid every ski day, only on resort days, or once a season.
- **Pass picker:** adult prices for Epic, Ikon, Mountain Collective, Indy,
  Boyne and Loveland passes for 2023–24 through 2026–27, kept in
  `PASS_SEASONS`. Each season says which price it lists (spring launch or
  fall), because pass prices climb from spring to the December cutoff.
- **Day ticket auto-fill:** picking a resort fills in its 2025–26 adult
  one-day window price on a busy day, from `TICKETS` (about 85 North American
  resorts). Canadian prices are in CAD and converted at a fixed
  `CAD_PER_USD` rate. About a third come from the resorts' own pages or local
  news; the rest come from aggregators (OnTheSnow, Snowstash) and may be
  online prices, so they can run low.
- **GPS tracks:** drop GPX, TCX or KML files (one day or many). A lift ride is
  a climb of at least 30 m that is nearly straight (≥ 0.85 straight-line to
  path length) and fast (≥ 0.25 m of climb per second). A run ends at a lift
  ride or at a self-powered climb of 100 m or more, so skin laps count as
  runs and short bootpacks don't split a run. Days with runs but no lift
  rides count as backcountry days. FIT files are not read; export GPX.
- **What you could do instead:** counts of everyday things, a share of a big
  expense (a knee replacement, about $25,000 cash at a surgery center), and
  the stock-market value at retirement if the money were invested once or
  every year at a chosen return after inflation.
- **Outputs:** the on-screen receipt, a receipt-shaped PDF (80 mm wide), and a
  plain-text copy.

Prices change every season; update `PASS_SEASONS` and `TICKETS` near the top
of the script when new numbers come out.
