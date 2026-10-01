"""Print page geometry shared by pipeline.py (rem_env) and print_layout.py (QGIS python).

Standard library only, so both interpreters can import it. All sizes are millimetres.
"""

INCH = 25.4
MM_PER_PT = 25.4 / 72

# portrait width x height
PAGES = {
    "letter": (8.5 * INCH, 11 * INCH),
    "11x17": (11 * INCH, 17 * INCH),
    "18x24": (18 * INCH, 24 * INCH),
    "24x36": (24 * INCH, 36 * INCH),
}
PAGE_LABELS = {"letter": "Letter (8.5×11 in)", "11x17": "Tabloid (11×17 in)",
               "18x24": "18×24 in", "24x36": "24×36 in"}

# Nice map scales (denominators), ascending. The frame uses the smallest one that fits.
NICE_SCALES = sorted({int(m * 10 ** k) for k in range(2, 7)
                      for m in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5)} | {24000, 62500})


def page_size(page: str, landscape: bool) -> tuple[float, float]:
    w, h = PAGES[page]
    return (h, w) if landscape else (w, h)


def layout(page: str, landscape: bool) -> dict:
    """Rectangles (x, y, w, h from the top-left, mm) for every layout element, plus type sizes in mm.

    Everything scales with the page's short side, so Letter and 24x36 look the same, just bigger.
    """
    pw, ph = page_size(page, landscape)
    u = min(pw, ph) / 100           # layout unit
    m = 4 * u                       # outer margin
    title_band = 14 * u
    footer_band = 16 * u
    map_rect = (m, m + title_band, pw - 2 * m, ph - 2 * m - title_band - footer_band)
    fy = map_rect[1] + map_rect[3] + 2.5 * u      # top of footer content
    fw = pw - 2 * m
    return {
        "page": (pw, ph),
        "unit": u,
        "map": map_rect,
        "title": (m, m, fw, 8 * u),
        "subtitle": (m, m + 8.6 * u, fw, 3.6 * u),
        "legend": (m, fy, min(34 * u, fw * 0.36), 9 * u),
        "scalebar": (m + fw * 0.42, fy, fw * 0.30, 9 * u),
        "north": (pw - m - 5 * u, fy, 5 * u, 7.5 * u),
        "scale_text": (pw - m - fw * 0.24, fy + 0.4 * u, fw * 0.24 - 6.5 * u, 3 * u),
        "credits": (m, ph - m - 1.8 * u, fw, 1.8 * u),
        # type sizes, as cap-ish heights in mm (converted to points in the layout script)
        "type": {"title": 6.0 * u, "subtitle": 2.2 * u, "label": 1.35 * u, "small": 1.05 * u,
                 "credits": 0.9 * u},
        "stroke": 0.12 * u,
    }


def map_size(page: str, landscape: bool) -> tuple[float, float]:
    """Width and height of the map frame in mm."""
    _, _, w, h = layout(page, landscape)["map"]
    return w, h


def nice_scale(minimum: float) -> int:
    """Smallest nice scale denominator >= minimum."""
    for s in NICE_SCALES:
        if s >= minimum:
            return s
    return int(minimum)
