"""Build the print layout for a River REM run and export PDF, PNG and a QGIS project.

Runs under QGIS's own python (pipeline.make_print launches it), not rem_env:
    <QGIS.app>/Contents/MacOS/python3.12 print_layout.py <run>/print_spec.json

Writes <basename>.pdf / .png / .qgz and print_result.json into the run folder.
"""
import json
import math
import os
import sys

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsGradientColorRamp,
    QgsGradientFillSymbolLayer,
    QgsGradientStop,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemMap,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutItemShape,
    QgsLayoutMeasurement,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsLineSymbol,
    QgsPrintLayout,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSimpleFillSymbolLayer,
    QgsTextFormat,
)
from qgis.PyQt.QtCore import QPointF, Qt
from qgis.PyQt.QtGui import QColor, QFont, QFontDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import print_spec  # noqa: E402

MM = Qgis.LayoutUnit.Millimeters
INK = QColor("#1d2a2f")
MUTED = QColor("#55636a")
PT_PER_MM = 72 / 25.4
CAP_TO_EM = 1.45   # spec sizes are roughly cap heights; a font's point size is ~1.45x that


def font_family(preferred: str, fallback: str = "Helvetica") -> str:
    return preferred if preferred in QFontDatabase.families() else fallback


def text_format(family: str, style: str, size_mm: float, color: QColor = INK) -> QgsTextFormat:
    fmt = QgsTextFormat()
    fmt.setFont(QFont(family))
    if style in QFontDatabase.styles(family):
        fmt.setNamedStyle(style)
    fmt.setSize(size_mm * CAP_TO_EM * PT_PER_MM)
    fmt.setSizeUnit(Qgis.RenderUnit.Points)
    fmt.setColor(color)
    return fmt


def place(item, rect):
    x, y, w, h = rect
    item.attemptMove(QgsLayoutPoint(x, y, MM))
    item.attemptResize(QgsLayoutSize(w, h, MM))


def add_label(layout, text, fmt, rect, halign=Qt.AlignmentFlag.AlignLeft, valign=Qt.AlignmentFlag.AlignTop):
    label = QgsLayoutItemLabel(layout)
    label.setText(text)
    label.setTextFormat(fmt)
    label.setHAlign(halign)
    label.setVAlign(valign)
    label.setMarginX(0)
    label.setMarginY(0)
    layout.addLayoutItem(label)
    place(label, rect)
    return label


def add_rect(layout, rect, symbol):
    shape = QgsLayoutItemShape(layout)
    shape.setShapeType(QgsLayoutItemShape.Shape.Rectangle)
    shape.setSymbol(symbol)
    layout.addLayoutItem(shape)
    place(shape, rect)
    return shape


def nice_length(maximum: float) -> float:
    """Largest 1/1.5/2/2.5/3/5 x 10^k value <= maximum."""
    k = math.floor(math.log10(maximum))
    for m in (5, 3, 2.5, 2, 1.5, 1):
        if m * 10 ** k <= maximum:
            return m * 10 ** k
    return 10 ** k


def is_round(value: float) -> bool:
    """True for 1, 2, 2.5 or 5 times a power of ten (labels stay short)."""
    k = math.floor(math.log10(value))
    return any(math.isclose(value, m * 10 ** k) for m in (1, 2, 2.5, 5))


def segment_count(total: float, total_mm: float, min_seg_mm: float) -> int:
    """Most segments (5..2) that give round segment values and room for each label."""
    for n in (5, 4, 3, 2):
        if is_round(total / n) and total_mm / n >= min_seg_mm:
            return n
    return 1


def add_scalebar(layout, map_item, rect, scale, unit, fmt, height_mm, min_seg_mm):
    """Single-box scale bar sized to a round length that fits rect's width."""
    metres_per_unit = {"mi": 1609.344, "km": 1000.0}[unit]
    x, y, w, h = rect
    max_units = w * 0.80 * scale / 1000 / metres_per_unit   # leave room for the unit label
    total = nice_length(max_units)
    total_mm = total * metres_per_unit * 1000 / scale
    segments = segment_count(total, total_mm, min_seg_mm)
    bar = QgsLayoutItemScaleBar(layout)
    bar.setLinkedMap(map_item)
    bar.setStyle("Single Box")
    # QGIS converts map metres to miles/km itself; don't also set mapUnitsPerScaleBarUnit
    bar.setUnits(Qgis.DistanceUnit.Miles if unit == "mi" else Qgis.DistanceUnit.Kilometers)
    bar.setUnitLabel(unit)
    bar.setSegmentSizeMode(Qgis.ScaleBarSegmentSizeMode.Fixed)
    bar.setNumberOfSegmentsLeft(0)
    bar.setNumberOfSegments(segments)
    bar.setUnitsPerSegment(total / segments)
    bar.setHeight(height_mm)
    bar.setTextFormat(fmt)
    bar.setLabelBarSpace(height_mm * 0.6)
    bar.setBoxContentSpace(0)
    stroke = {"line_color": INK.name(), "line_width": f"{height_mm * 0.12:.3f}", "line_width_unit": "MM"}
    bar.setLineSymbol(QgsLineSymbol.createSimple(stroke))
    bar.setFillSymbol(QgsFillSymbol.createSimple({"color": INK.name(), "outline_style": "no"}))
    bar.setAlternateFillSymbol(QgsFillSymbol.createSimple({"color": "#ffffff", "outline_style": "no"}))
    bar.setBackgroundEnabled(False)
    bar.setFrameEnabled(False)
    layout.addLayoutItem(bar)
    bar.attemptMove(QgsLayoutPoint(x, y, MM))
    bar.update()
    return bar


def outline_layer(stroke_mm: float) -> QgsSimpleFillSymbolLayer:
    layer = QgsSimpleFillSymbolLayer(QColor(0, 0, 0, 0), Qt.BrushStyle.NoBrush, INK, Qt.PenStyle.SolidLine, stroke_mm)
    layer.setStrokeWidthUnit(Qgis.RenderUnit.Millimeters)
    return layer


def add_ticks(layout, labels, bar, tick_fmt, geo):
    """Tick marks and labels under the legend bar at (x_mm, text), dropping any that would collide.

    The last label (the ramp top) always wins over its neighbour.
    """
    x0, y, w, h = bar
    u = geo["unit"]
    label_w, min_gap = 5 * u, 4.4 * u   # min_gap: roughly the printed width of "1,000" plus a space
    kept = []
    for i, (x, text) in enumerate(labels):
        if kept and x - kept[-1][0] < min_gap:
            if i != len(labels) - 1:
                continue
            kept.pop()
        kept.append((x, text))
    tick_sym = QgsFillSymbol.createSimple({"color": INK.name(), "outline_style": "no"})
    for x, text in kept:
        tx = x0 + x
        add_rect(layout, (tx - geo["stroke"] / 2, y + h, geo["stroke"], 0.6 * u), tick_sym.clone())
        add_label(layout, text, tick_fmt, (tx - label_w / 2, y + h + 0.9 * u, label_w, 1.8 * u),
                  halign=Qt.AlignmentFlag.AlignHCenter)


def draw_smooth_legend(layout, legend, bar, tick_fmt, geo):
    x0, y, w, h = bar
    stops = legend["stops"]
    grad = QgsGradientFillSymbolLayer()
    grad.setGradientColorType(Qgis.GradientColorSource.ColorRamp)
    grad.setColorRamp(QgsGradientColorRamp(QColor(stops[0][1]), QColor(stops[-1][1]), False,
                                           [QgsGradientStop(p, QColor(c)) for p, c in stops[1:-1]]))
    grad.setGradientType(Qgis.GradientType.Linear)
    grad.setCoordinateMode(Qgis.SymbolCoordinateReference.Feature)
    grad.setReferencePoint1(QPointF(0, 0.5))
    grad.setReferencePoint2(QPointF(1, 0.5))
    add_rect(layout, bar, QgsFillSymbol([grad, outline_layer(geo["stroke"])]))
    labels = [(frac * w, text) for frac, text in legend["ticks"]]
    top = legend.get("top", legend.get("top_ft"))   # runs from before units were added use top_ft
    if top > float(legend["ticks"][-1][1].replace(",", "")):
        labels.append((w, f"{top:,.0f}+"))
    add_ticks(layout, labels, bar, tick_fmt, geo)


def draw_stepped_legend(layout, legend, bar, tick_fmt, geo):
    """One equal-width box per class, so thin classes near the river stay readable; labels at the breaks."""
    x0, y, w, h = bar
    classes = legend["classes"]
    bw = w / len(classes)
    for i, (_, _, color) in enumerate(classes):
        add_rect(layout, (x0 + i * bw, y, bw, h), QgsFillSymbol.createSimple({"color": color, "outline_style": "no"}))
    add_rect(layout, bar, QgsFillSymbol([outline_layer(geo["stroke"])]))
    labels = [(i * bw, f"{lo:,g}") for i, (lo, _, _) in enumerate(classes)]
    labels.append((w, f"{classes[-1][1]:,g}+"))
    add_ticks(layout, labels, bar, tick_fmt, geo)


def build(spec: dict) -> dict:
    frame, legend = spec["frame"], spec["legend"]
    geo = print_spec.layout(frame["page"], frame["landscape"])
    u = geo["unit"]
    title_family = font_family("High Alpine")
    body_family = font_family("Neue Frutiger World")

    project = QgsProject.instance()
    crs = QgsCoordinateReferenceSystem(f"EPSG:{frame['epsg']}")
    project.setCrs(crs)
    layer = QgsRasterLayer(spec["viz_tif"], "River REM")
    if not layer.isValid():
        raise RuntimeError(f"Could not open {spec['viz_tif']}")
    project.addMapLayer(layer)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(spec["title"])
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(*geo["page"], MM))

    # --- map: rotated so the river runs along the page, at the frame's nice scale
    map_item = QgsLayoutItemMap(layout)
    map_item.setCrs(crs)
    map_item.setLayers([layer])
    map_item.setKeepLayerSet(True)
    layout.addLayoutItem(map_item)
    place(map_item, geo["map"])
    cx, cy = frame["center"]
    hw, hh = frame["width_m"] / 2, frame["height_m"] / 2
    map_item.setExtent(QgsRectangle(cx - hw, cy - hh, cx + hw, cy + hh))
    map_item.setMapRotation(frame["rotation"])
    map_item.setScale(frame["scale"])
    map_item.setFrameEnabled(True)
    map_item.setFrameStrokeColor(INK)
    map_item.setFrameStrokeWidth(QgsLayoutMeasurement(geo["stroke"], MM))
    map_item.setBackgroundColor(QColor("white"))

    # --- title block
    t = geo["type"]
    add_label(layout, spec["title"], text_format(title_family, "Regular", t["title"]), geo["title"],
              valign=Qt.AlignmentFlag.AlignBottom)
    if spec.get("subtitle"):
        add_label(layout, spec["subtitle"], text_format(body_family, "Book", t["subtitle"], MUTED), geo["subtitle"])

    # --- legend: colour ramp in feet (smooth gradient, or one box per class for stepped ramps)
    lx, ly, lw, lh = geo["legend"]
    add_label(layout, f"Height above river ({legend.get('unit_name', 'feet')})",
              text_format(body_family, "Medium", t["label"]), (lx, ly, lw, 2.4 * u))
    bar = (lx, ly + 3.4 * u, lw, 1.7 * u)
    tick_fmt = text_format(body_family, "Book", t["small"])
    if legend.get("mode") == "stepped":
        draw_stepped_legend(layout, legend, bar, tick_fmt, geo)
    else:
        draw_smooth_legend(layout, legend, bar, tick_fmt, geo)

    # --- scale bars (miles above km), north arrow, scale text, credits
    sx, sy, sw, sh = geo["scalebar"]
    sb_fmt = text_format(body_family, "Book", t["small"])
    add_scalebar(layout, map_item, (sx, sy, sw, sh / 2), frame["scale"], "mi", sb_fmt, 0.9 * u, 4.5 * u)
    add_scalebar(layout, map_item, (sx, sy + sh / 2, sw, sh / 2), frame["scale"], "km", sb_fmt, 0.9 * u, 4.5 * u)

    north = QgsLayoutItemPicture(layout)
    north.setPicturePath(os.path.join(HERE, "static", "north_arrow.svg"))
    north.setResizeMode(QgsLayoutItemPicture.ResizeMode.Zoom)
    north.setLinkedMap(map_item)
    north.setNorthMode(QgsLayoutItemPicture.NorthMode.TrueNorth)
    layout.addLayoutItem(north)
    place(north, geo["north"])

    add_label(layout, f"Scale 1:{frame['scale']:,}", text_format(body_family, "Medium", t["label"]),
              geo["scale_text"], halign=Qt.AlignmentFlag.AlignRight)
    add_label(layout, spec["credits"], text_format(body_family, "Book", t["credits"], MUTED),
              geo["credits"], valign=Qt.AlignmentFlag.AlignBottom)

    # --- export
    out_dir, base = spec["out_dir"], spec["basename"]
    exporter = QgsLayoutExporter(layout)
    pdf_settings = QgsLayoutExporter.PdfExportSettings()
    pdf_settings.dpi = spec.get("dpi", 300)
    pdf_settings.rasterizeWholeImage = False
    pdf_settings.appendGeoreference = True
    # real text with embedded (subset) fonts, not outlines: selectable, smaller, editable in Illustrator
    pdf_settings.textRenderFormat = Qgis.TextRenderFormat.AlwaysText
    pdf_path = os.path.join(out_dir, f"{base}.pdf")
    if exporter.exportToPdf(pdf_path, pdf_settings) != QgsLayoutExporter.ExportResult.Success:
        raise RuntimeError(f"PDF export failed: {exporter.errorMessage()}")

    img_settings = QgsLayoutExporter.ImageExportSettings()
    img_settings.dpi = spec.get("dpi", 300)
    png_path = os.path.join(out_dir, f"{base}.png")
    if exporter.exportToImage(png_path, img_settings) != QgsLayoutExporter.ExportResult.Success:
        raise RuntimeError(f"PNG export failed: {exporter.errorMessage()}")

    project.layoutManager().addLayout(layout)
    qgz_path = os.path.join(out_dir, f"{base}.qgz")
    if not project.write(qgz_path):
        raise RuntimeError(f"Could not save {qgz_path}")

    fonts = {"title": title_family, "body": body_family}
    print(f"fonts: title={title_family}, body={body_family}")
    print(f"wrote {os.path.basename(pdf_path)}, {os.path.basename(png_path)}, {os.path.basename(qgz_path)}")
    return {"pdf": os.path.basename(pdf_path), "png": os.path.basename(png_path),
            "qgz": os.path.basename(qgz_path), "fonts": fonts}


def main() -> int:
    with open(sys.argv[1]) as f:
        spec = json.load(f)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        result = build(spec)
    finally:
        app.exitQgis()
    with open(os.path.join(spec["out_dir"], "print_result.json"), "w") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
