# US avalanche deaths map

Data for the map at `tools/maps_site/avalanche-deaths/` (published at
https://maps.ridgelinemaps.com/avalanche-deaths/).

The map is one self-contained HTML page. The accident records live inside it, in the
`<script id="accident-data">` block, and `build_data.py` writes that block from the
Colorado Avalanche Information Center's accident workbook.

## Updating with a new CAIC workbook

CAIC publishes an updated workbook each fall.

1. Put the new `.xlsx` file in `raw/` (keep the CAIC file name, e.g.
   `CAIC_Accident_Data_Oct_2026.xlsx`; the page reads the month and year from it).
2. Run:

   ```
   pip install pandas openpyxl
   python tools/avalanche_deaths/build_data.py
   ```

   With no arguments it uses the newest `.xlsx` in `raw/` (by the month and year in its name) and
   prints a summary: accidents, deaths, how many have coordinates, and the winters covered.
3. If it stops with "New activity types", CAIC added a victim activity. Add it to one
   of the seven groups in `GROUPS` at the top of the script and run it again.

Everything on the page (totals, the opening paragraph, the timeline range, the "Last 10
winters" button) is computed from the data, so nothing else needs editing.

## About the data

- One row per avalanche accident in which at least one person died, winters 1950-51 on.
- `AvyYear` is the winter, named by the year it ends (2025 = winter 2024-25).
- Accidents without coordinates have `lat` and `lon` set to 0. They count toward their
  state's shading on the map but get no dot. CAIC has coordinates for some accidents
  from the 1999-2000 winter and for every accident since 2011-12.
- CAIC asks that you cite it whenever you use the data.
