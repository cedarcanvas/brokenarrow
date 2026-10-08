"""Colour ramps for REMs: built-in colormaps, QGIS ramps, and custom colour stops.

Every ramp resolves to a matplotlib colormap (call it with 0..255 for RGBA in 0..1). Names:

    mako, cmo.deep, ...      seaborn / matplotlib / cmocean, as RiverREM accepts
    qgis:<name>              a ramp from the newest QGIS profile's style library
    file:<name>              a ramp from a QGIS style XML in ramps/ (e.g. a hub.qgis.org download)
    custom:<hex>-<hex>-...   evenly spaced colour stops, at least two

QGIS gradient ramps keep their stop positions; "discrete" ones become hard-edged bands.
"""
import glob
import os
import re
import sqlite3
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
RAMPS_DIR = os.path.join(HERE, "ramps")

# Built-ins shown in the app. Anything else RiverREM can resolve still works by name.
BUILTIN = {
    "RiverREM classics": ["mako", "mako_r", "rocket", "topo", "cmo.deep", "cmo.tempo", "cmo.ice", "Blues_r"],
    "Perceptual": ["viridis", "cividis", "magma", "inferno", "plasma", "flare", "crest", "icefire"],
    "Water & terrain": ["cmo.dense", "cmo.haline", "cmo.matter", "cmo.turbid", "cmo.speed", "cmo.topo",
                        "cmo.algae", "cmo.amp", "GnBu_r", "YlGnBu_r", "PuBu_r", "BuPu_r"],
    "Diverging": ["Spectral_r", "RdYlBu", "BrBG", "PuOr", "cmo.balance", "cmo.curl", "vlag", "coolwarm"],
}
HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
MAX_CUSTOM = 12


# ---------------------------------------------------------------- QGIS style XML

def _rgba(s: str) -> tuple | None:
    """QGIS colour string ("r,g,b,a" or "r,g,b,a,rgb:..." or "#rrggbb") -> (r, g, b) in 0..1."""
    s = s.strip()
    if s.startswith("#") and len(s) >= 7:
        return tuple(int(s[i:i + 2], 16) / 255 for i in (1, 3, 5))
    m = re.match(r"(\d+),(\d+),(\d+)", s)
    return tuple(int(v) / 255 for v in m.groups()) if m else None


def _props(el) -> dict:
    """A <colorramp>'s properties, from the QGIS 3.x <Option> map or the older <prop k= v=/> list."""
    out = {o.get("name"): o.get("value") for o in el.iter("Option") if o.get("name")}
    out.update({p.get("k"): p.get("v") for p in el.iter("prop") if p.get("k")})
    return out


def parse_colorramp(el) -> dict | None:
    """{"stops": [(pos, (r, g, b)), ...], "discrete": bool} for a <colorramp>, or None if unsupported."""
    kind, p = el.get("type"), _props(el)
    if kind == "gradient":
        c1, c2 = _rgba(p.get("color1", "")), _rgba(p.get("color2", ""))
        if not (c1 and c2):
            return None
        stops = [(0.0, c1)]
        # "pos;r,g,b,a[,rgb:...][;spec;dir]" joined by ":" (the rgb: part has a colon too, so match, don't split)
        for pos, color in re.findall(r"(\d*\.?\d+(?:e-?\d+)?);(\d+,\d+,\d+(?:,\d+)?)", p.get("stops", "")):
            stops.append((float(pos), _rgba(color)))
        stops.append((1.0, c2))
        return {"stops": sorted(stops, key=lambda s: s[0]), "discrete": p.get("discrete") == "1"}
    if kind == "preset":
        colors = [_rgba(p[k]) for k in sorted((k for k in p if re.fullmatch(r"preset_color_\d+", k)),
                                              key=lambda k: int(k.rsplit("_", 1)[1]))]
        colors = [c for c in colors if c]
        if len(colors) < 2:
            return None
        return {"stops": [(i / (len(colors) - 1), c) for i, c in enumerate(colors)], "discrete": True}
    if kind == "colorbrewer" and p.get("schemeName"):
        return {"brewer": p["schemeName"], "count": int(p.get("colors", 9) or 9)}
    return None   # cpt-city (needs its archive) and random ramps


def ramps_from_xml(text: str) -> dict:
    """{name: ramp} for every supported <colorramp> in a QGIS style XML document."""
    root = ET.fromstring(text)
    els = [root] if root.tag == "colorramp" else root.iter("colorramp")
    out = {}
    for el in els:
        r = parse_colorramp(el)
        if r and el.get("name"):
            out[el.get("name")] = r
    return out


def qgis_style_db() -> str | None:
    """symbology-style.db of the newest QGIS profile (QGIS4 before QGIS3)."""
    base = os.path.expanduser("~/Library/Application Support/QGIS")
    for major in ("QGIS4", "QGIS3"):
        hits = glob.glob(os.path.join(base, major, "profiles", "default", "symbology-style.db"))
        if hits:
            return hits[0]
    return None


def qgis_ramps() -> dict:
    db = qgis_style_db()
    if not db:
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("SELECT name, xml FROM colorramp ORDER BY favorite DESC, name").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    out = {}
    for name, xml in rows:
        try:
            out.update({name: r for r in ramps_from_xml(xml).values()})
        except ET.ParseError:
            continue
    return out


def file_ramps() -> dict:
    """Ramps from QGIS style XML files dropped into ramps/ (one file can hold many ramps)."""
    out = {}
    for path in sorted(glob.glob(os.path.join(RAMPS_DIR, "*.xml"))):
        try:
            with open(path, encoding="utf-8") as f:
                out.update(ramps_from_xml(f.read()))
        except (OSError, ET.ParseError):
            continue
    return out


# ---------------------------------------------------------------- colormaps

def _from_stops(name: str, ramp: dict):
    from matplotlib.colors import LinearSegmentedColormap

    if "brewer" in ramp:
        return _named(ramp["brewer"])
    stops = ramp["stops"]
    if ramp.get("discrete"):
        # each stop's colour holds until the next stop: duplicate positions for hard edges
        hard = []
        for (p0, c0), (p1, _) in zip(stops, stops[1:]):
            hard += [(p0, c0), (max(p0, p1 - 1e-6), c0)]
        hard.append(stops[-1])
        stops = hard
    return LinearSegmentedColormap.from_list(name, stops, N=256)


def _named(name: str):
    from riverrem.RasterViz import RasterViz

    return RasterViz._get_cm_mpl(name)


def custom_colors(spec: str) -> list[str]:
    colors = [c for c in spec.split("-") if c]
    if not 2 <= len(colors) <= MAX_CUSTOM or not all(HEX.match(c) for c in colors):
        raise ValueError(f"custom ramps need 2 to {MAX_CUSTOM} hex colours, e.g. custom:0b1d3a-3f8fb0-f4f1de")
    return ["#" + HEX.match(c).group(1).lower() for c in colors]


def get_cmap(name: str):
    """matplotlib colormap for any ramp name this module knows (raises ValueError otherwise)."""
    if name.startswith("custom:"):
        colors = custom_colors(name[7:])
        return _from_stops(name, {"stops": [(i / (len(colors) - 1), c) for i, c in enumerate(colors)]})
    for prefix, source in (("qgis:", qgis_ramps), ("file:", file_ramps)):
        if name.startswith(prefix):
            ramps = source()
            if name[len(prefix):] not in ramps:
                raise ValueError(f"No QGIS ramp named {name[len(prefix):]!r}")
            return _from_stops(name, ramps[name[len(prefix):]])
    return _named(name)


def slug(name: str) -> str:
    """Short filename-safe form (custom ramps become custom-<hash> so names stay short)."""
    if name.startswith("custom:"):
        import hashlib

        return "custom-" + hashlib.sha1(name.encode()).hexdigest()[:6]
    return re.sub(r"[^A-Za-z0-9]+", "-", name.split(":", 1)[-1]).strip("-")


def catalog() -> list[dict]:
    """Ramp groups for the app's picker: [{"group": ..., "ramps": [{"id", "label"}]}]."""
    groups = [{"group": g, "ramps": [{"id": n, "label": n} for n in names]} for g, names in BUILTIN.items()]
    for prefix, label, source in (("file:", "Imported (QGIS XML)", file_ramps),
                                  ("qgis:", "QGIS style library", qgis_ramps)):
        names = list(source())
        if names:
            groups.append({"group": label, "ramps": [{"id": prefix + n, "label": n} for n in names]})
    return groups


def import_xml(filename: str, text: str) -> list[str]:
    """Save an uploaded QGIS style XML into ramps/; returns the ramp names it adds (ValueError if none)."""
    try:
        found = ramps_from_xml(text)
    except ET.ParseError as exc:
        raise ValueError(f"Not a QGIS style XML file ({exc})") from exc
    if not found:
        raise ValueError("No gradient, preset or ColorBrewer colour ramps in that file.")
    os.makedirs(RAMPS_DIR, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(filename))[0]).strip("_") or "ramps"
    with open(os.path.join(RAMPS_DIR, stem + ".xml"), "w", encoding="utf-8") as f:
        f.write(text)
    return ["file:" + n for n in found]
