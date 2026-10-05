// USPS Delivery Days map.
// Hover a ZIP code: every other ZIP is colored by USPS delivery days from it.
// Data comes from web/data/, made by build/build_data.py.
(() => {
  "use strict";

  // ---------- day buckets (one color each) ----------
  const BUCKETS = [
    { label: "1 day", v: "--d1" },
    { label: "2 days", v: "--d2" },
    { label: "3 days", v: "--d3" },
    { label: "4 days", v: "--d4" },
    { label: "5 days", v: "--d5" },
    { label: "6–9 days", v: "--d6" },
    { label: "10+ days", v: "--d7" },
    { label: "No standard", v: "--none" },
  ];
  const NONE = 7;
  const bucketOf = (d) => (d === 0 ? NONE : d <= 5 ? d - 1 : d <= 9 ? 5 : 6);
  // With a mailing day picked, colors mean arrival days: days 1-6 after mailing
  // each get their own color (one weekday each), then "a week or more".
  const bkt = (v) => (S.mailDay == null ? bucketOf(v) : v === 0 ? NONE : v <= 6 ? v - 1 : 6);
  const daysText = (d) => (d === 0 ? "No USPS standard" : d === 1 ? "1 day" : `${d} days`);
  // Mailing day. USPS counts delivery days after the day mail is accepted and
  // skips Sundays and federal holidays, so the weekday you mail on changes how
  // many calendar days the trip takes. Index 0 = Monday ... 6 = Sunday.
  const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  function arrival(wd, d) {
    // Mail dropped on Sunday is accepted Monday.
    let cur = wd === 6 ? 0 : wd, cal = wd === 6 ? 1 : 0, n = 0;
    while (n < d) { cur = (cur + 1) % 7; cal++; if (cur !== 6) n++; }
    return { cal, wd: cur };
  }
  // USPS days -> calendar days, one lookup table per mailing day.
  const CAL = WEEKDAYS.map((_, wd) => Uint8Array.from({ length: 256 }, (_, d) => (d ? Math.min(255, arrival(wd, d).cal) : 0)));
  const SHORT = { fcm: "Letters", ga: "Ground Adv.", pm: "Priority", mkt: "Marketing", per: "Periodicals" };

  // ---------- map layout ----------
  // Everything is drawn in a fixed "virtual" space, then scaled to fit the screen.
  // Alaska, Hawaii and the territories in a column of boxes on the left, the
  // lower 48 filling the rest.
  const VW = 1000, VH = 600;
  const PANELS = [
    { name: "Lower 48", short: "", rot: [96, 0], box: [[252, 10], [990, 590]] },
    { name: "Alaska", short: "AK", rot: [152, 0], box: [[10, 10], [240, 222]] },
    { name: "Hawaii", short: "HI", rot: [157, 0], box: [[10, 232], [240, 340]] },
    { name: "Puerto Rico & USVI", short: "PR · VI", rot: [66, 0], box: [[10, 350], [240, 440]] },
    { name: "Guam & N. Mariana Is.", short: "GU · MP", rot: [-145, 0], box: [[10, 450], [240, 524]] },
    { name: "American Samoa", short: "AS", rot: [170, 0], box: [[10, 534], [240, 590]] },
  ];
  const PAD = 8;

  // ---------- state ----------
  const S = {
    meta: null, zip3: [], N: 0,
    feats: [],        // per ZIP: {z, i, p, path, bbox}
    byZip: new Map(), // "80202" -> feature
    zip3Paths: [],    // per prefix: Path2D of all its ZIPs
    zip3Count: [],    // per prefix: number of ZIP shapes
    meshZip3: null, meshZcta: null, meshState: null,
    grid: null,
    cls: "fcm", days: {},
    mailDay: null,    // null = plain USPS days; 0-6 = mailed Mon-Sun (calendar days)
    origin: null,     // prefix index of the origin
    originZip: null,  // feature
    pinned: false,
    hover: null,      // feature under the pointer
    t: d3.zoomIdentity,
    fit: { k: 1, x: 0, y: 0 },
    colors: {},
    hubs: null, showHubs: true, hoverHub: null,
    view: "zip",      // "zip" = ZIP areas colored by days; "net" = inferred plant areas; "usps" = official plants (SCF)
    net: null,        // current network {nodes, nodeOf, mesh}; S.nets caches one per grouping
    nets: {},
    sort: null,       // official USPS sorting chain per prefix (data/sort.json), if present
  };

  const canvas = document.getElementById("map");
  const ctx = canvas.getContext("2d");
  const tip = document.getElementById("tip");
  const $ = (id) => document.getElementById(id);

  // ---------- loading ----------
  async function load() {
    const [meta, zip3, topo, states] = await Promise.all([
      fetch("data/meta.json").then(r => r.json()),
      fetch("data/zip3.json").then(r => r.json()),
      fetch("data/zcta.topo.json").then(r => r.json()),
      fetch("data/states.topo.json").then(r => r.json()),
    ]);
    const cities = await fetch("cities.json").then(r => (r.ok ? r.json() : [])).catch(() => []);
    S.meta = meta; S.zip3 = zip3; S.N = zip3.length;
    // Optional: USPS on-time results (made by build/fetch_performance.py).
    S.perf = await fetch("data/perf.json").then(r => (r.ok ? r.json() : null)).catch(() => null);
    $("demo").hidden = !meta.demo;
    $("vintage").textContent = `Data: ${meta.vintage} · built ${meta.built}`;

    const hash = readHash();
    const avail = meta.classes.map(c => c.key);
    S.cls = avail.includes(hash.cls) ? hash.cls : avail[0];
    buildClassButtons();
    if (hash.day != null) { S.mailDay = hash.day; $("mailday").value = String(hash.day); }
    buildGeometry(topo, states);
    buildCities(cities);
    readColors();
    resize();
    await setClass(S.cls);
    $("loading").remove();
    buildCityIndex();
    fetch("data/zipnames.json").then(r => (r.ok ? r.json() : null)).then((n) => {
      if (!n) return;
      S.names = n; buildCityIndex(); legend();
    }).catch(() => {});
    // Optional: official USPS sorting chain (made by build/read_labeling.py).
    fetch("data/sort.json").then(r => (r.ok ? r.json() : null)).then((d) => {
      if (d && d.f) { buildSort(d); legend(); }
    }).catch(() => {});
    // Optional: fastest / slowest ZIP prefixes (made by build/rank_zips.py).
    fetch("data/rank.json").then(r => (r.ok ? r.json() : null)).then((r) => {
      if (r && r.classes) { S.rank = r; rankPanel(); }
    }).catch(() => {});
    // Optional: USPS mail processing plants (made by build/fetch_hubs.py).
    fetch("data/hubs.json").then(r => (r.ok ? r.json() : null)).then((h) => {
      if (!h || !h.h || !h.h.length) return;
      buildHubs(h); legend(); draw();
    }).catch(() => {});
    if (hash.zip && S.byZip.has(hash.zip)) pinZip(S.byZip.get(hash.zip));
    if (hash.to && S.originZip && S.byZip.has(hash.to)) setDest(S.byZip.get(hash.to));
    status();
  }

  function buildClassButtons() {
    const box = $("classes");
    for (const c of S.meta.classes) {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.dataset.k = c.key;
      b.innerHTML = c.key === "fcm" ? `Letters <small>First-Class</small>`
        : c.key === "ga" || c.key === "pm" ? `${c.label} <small>pkg</small>` : c.label;
      b.title = c.label;
      b.addEventListener("click", () => setClass(c.key));
      box.appendChild(b);
    }
  }

  async function setClass(k) {
    S.cls = k;
    for (const b of $("classes").children) b.setAttribute("aria-checked", b.dataset.k === k);
    $("className").textContent = S.meta.classes.find(c => c.key === k).label;
    unitsText();
    if (!S.days[k]) {
      const buf = await fetch(`data/days_${k}.bin`).then(r => r.arrayBuffer());
      S.days[k] = new Uint8Array(buf);
    }
    writeHash(); draw(); legend(); showTip(); status(); rankPanel();
  }

  // ---------- geometry ----------
  function buildGeometry(topo, states) {
    const projs = PANELS.map(() => null);
    const obj = topo.objects.zcta;
    const all = topojson.feature(topo, obj).features;
    // d3 treats a ring wound the "wrong" way as covering the rest of the globe.
    // No ZIP is bigger than a hemisphere, so flip any shape that claims to be.
    for (const f of all) if (f.geometry && d3.geoArea(f) > 2 * Math.PI) rewind(f.geometry);

    // Fit each panel's projection to its box.
    PANELS.forEach((P, p) => {
      const fc = { type: "FeatureCollection", features: all.filter(f => f.properties.p === p) };
      const proj = d3.geoEqualEarth().rotate(P.rot).precision(0.2);
      const [[x0, y0], [x1, y1]] = P.box;
      if (fc.features.length) proj.fitExtent([[x0 + PAD, y0 + PAD + (p ? 10 : 0)], [x1 - PAD, y1 - PAD]], fc);
      projs[p] = proj;
    });
    S.projs = projs;

    S.topo = topo;
    S.geomsByI = Array.from({ length: S.N }, () => []);
    for (const g of obj.geometries) S.geomsByI[g.properties.i].push(g);
    S.zip3Paths = Array.from({ length: S.N }, () => new Path2D());
    S.zip3Count = new Uint32Array(S.N);
    for (const f of all) {
      const { z, i, p } = f.properties;
      const gp = d3.geoPath(projs[p]);
      const path = new Path2D();
      gp.context(path)(f);
      S.zip3Paths[i].addPath(path);
      S.zip3Count[i]++;
      const feat = { z, i, p, path, bbox: gp.bounds(f) };
      S.feats.push(feat);
      S.byZip.set(z, feat);
    }

    // Lines: ZIP-prefix borders, ZIP borders, state borders. Drawn per panel.
    const meshPath = (topoObj, filter, projOf) => {
      const out = new Path2D();
      PANELS.forEach((_, p) => {
        const m = topojson.mesh(topoObj.t, topoObj.o, (a, b) => projOf(a) === p && filter(a, b));
        d3.geoPath(projs[p]).context(out)(m);
      });
      return out;
    };
    const Z = { t: topo, o: obj };
    const zp = (a) => a.properties.p;
    S.meshZip3 = meshPath(Z, (a, b) => a.properties.i !== b.properties.i, zp);
    S.meshZcta = meshPath(Z, (a, b) => a !== b && a.properties.i === b.properties.i, zp);
    const stateP = {
      Alaska: 1, Hawaii: 2, "Puerto Rico": 3, "United States Virgin Islands": 3,
      Guam: 4, "Commonwealth of the Northern Mariana Islands": 4, "American Samoa": 5,
    };
    S.meshState = meshPath({ t: states, o: states.objects.states },
      (a, b) => a !== b, (a) => stateP[a.properties.name] ?? 0);
    // All land, drawn under the ZIP shapes: what shows through is land that no
    // Census ZIP code area covers (forests, deserts, ranges, open water inside states).
    S.landPath = new Path2D();
    for (const f of topojson.feature(states, states.objects.states).features) {
      if (f.geometry && d3.geoArea(f) > 2 * Math.PI) rewind(f.geometry);
      d3.geoPath(projs[stateP[f.properties.name] ?? 0]).context(S.landPath)(f);
    }

    // A coarse grid of ZIP bounding boxes, for quick hover lookups.
    const G = 64, gw = VW / G, gh = VH / G;
    const grid = Array.from({ length: G * G }, () => []);
    S.feats.forEach((f, idx) => {
      const [[x0, y0], [x1, y1]] = f.bbox;
      if (!isFinite(x0)) return;
      for (let gx = Math.max(0, Math.floor(x0 / gw)); gx <= Math.min(G - 1, Math.floor(x1 / gw)); gx++)
        for (let gy = Math.max(0, Math.floor(y0 / gh)); gy <= Math.min(G - 1, Math.floor(y1 / gh)); gy++)
          grid[gy * G + gx].push(idx);
    });
    S.grid = { G, gw, gh, cells: grid };
    S.hitCtx = document.createElement("canvas").getContext("2d");
  }

  function rewind(g) {
    const polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
    for (const poly of polys)
      if (d3.geoArea({ type: "Polygon", coordinates: poly }) > 2 * Math.PI) for (const ring of poly) ring.reverse();
  }

  // ---------- city labels ----------
  // Which map box a point belongs in (same rules as build_data.panel_for).
  function panelFor(lon, lat) {
    if (lat > 50 && (lon < -129 || lon > 170)) return 1;
    if (lon > -179 && lon < -150 && lat > 15 && lat < 30) return 2;
    if (lon > -69 && lon < -63 && lat > 16 && lat < 20) return 3;
    if (lon > 140 && lat > 10 && lat < 25) return 4;
    if (lat < -5 && lon < -160) return 5;
    return 0;
  }

  // cities.json rows: [name, lon, lat, tier, population], biggest first.
  function buildCities(rows) {
    S.cities = [];
    for (const [name, lon, lat, tier, pop] of rows) {
      const p = panelFor(lon, lat);
      const xy = S.projs[p] && S.projs[p]([lon, lat]);
      if (!xy) continue;
      const [[x0, y0], [x1, y1]] = PANELS[p].box;
      if (xy[0] < x0 || xy[0] > x1 || xy[1] < y0 || xy[1] > y1) continue;
      S.cities.push({ name, tier, pop, p, x: xy[0], y: xy[1] });
    }
  }

  // Tier 1 (major cities) on the national view; regional cities as you zoom in.
  function drawCities(placed = []) {
    if (!S.cities || !S.cities.length) return placed;
    const maxTier = S.t.k < 1.8 ? 1 : S.t.k < 5 ? 2 : 3;
    const named = [], C = S.colors;
    // Small screens: only the biggest metros on the national view.
    const minPop = S.t.k < 1.8 && S.w < 700 ? 2500000 : 0;
    const small = S.w < 700;
    ctx.lineJoin = "round";
    ctx.textBaseline = "middle";
    for (const c of S.cities) {
      if (c.tier > maxTier || c.pop < minPop) continue;
      // Skip labels in inset boxes that are too small on screen to hold them.
      if (c.p && (PANELS[c.p].box[1][0] - PANELS[c.p].box[0][0]) * scale() < 150) continue;
      const [sx, sy] = toScreen(c.x, c.y);
      if (sx < -40 || sy < -10 || sx > S.w + 40 || sy > S.h + 10) continue;
      // Twin cities with the same name (Kansas City MO/KS): label only one.
      if (named.some(q => q[0] === c.name && Math.abs(q[1] - sx) + Math.abs(q[2] - sy) < 60)) continue;
      const big = c.tier === 1;
      ctx.font = `${big ? 600 : 500} ${(big ? 12 : 11) - (small ? 1 : 0)}px system-ui, -apple-system, 'Segoe UI', sans-serif`;
      const w = ctx.measureText(c.name).width, h = big ? 13 : 12;
      // Label to the right of the dot; try the left side if that's taken.
      // Labels in an inset box stay inside that box.
      const pb = c.p ? [...toScreen(...PANELS[c.p].box[0]), ...toScreen(...PANELS[c.p].box[1])] : null;
      let box = null;
      for (const lx of [sx + 5, sx - 5 - w]) {
        const b = [lx - 2, sy - h / 2 - 1, lx + w + 2, sy + h / 2 + 1];
        if (b[0] < 0 || b[2] > S.w) continue;
        if (pb && (b[0] < pb[0] || b[2] > pb[2] || b[1] < pb[1] || b[3] > pb[3])) continue;
        if (!placed.some(q => b[0] < q[2] && b[2] > q[0] && b[1] < q[3] && b[3] > q[1])) { box = b; break; }
      }
      if (!box) continue;
      placed.push(box, [sx - 3, sy - 3, sx + 3, sy + 3]);
      named.push([c.name, sx, sy]);
      ctx.beginPath(); ctx.arc(sx, sy, big ? 2.6 : 2, 0, 2 * Math.PI);
      ctx.fillStyle = C.ink; ctx.fill();
      ctx.lineWidth = 1.2; ctx.strokeStyle = C.halo; ctx.stroke();
      ctx.lineWidth = 3; ctx.strokeStyle = C.halo; ctx.strokeText(c.name, box[0] + 2, sy);
      ctx.fillStyle = C.ink; ctx.fillText(c.name, box[0] + 2, sy);
    }
    ctx.textBaseline = "alphabetic";
    return placed;
  }

  // ZIP prefix labels ("816xx") when zoomed in, placed at each prefix's center
  // and skipped where they would cover a city or plant label.
  function drawPrefixes(placed) {
    if (S.t.k < 2.5) return;
    const C = S.colors;
    ctx.font = "600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.lineJoin = "round";
    for (let i = 0; i < S.N; i++) {
      const z = S.zip3[i];
      if (!S.zip3Count[i] || !z.c) continue;
      if (z.p && (PANELS[z.p].box[1][0] - PANELS[z.p].box[0][0]) * scale() < 150) continue;
      const xy = S.projs[z.p] && S.projs[z.p](z.c);
      if (!xy) continue;
      const [sx, sy] = toScreen(xy[0], xy[1]);
      if (sx < -20 || sy < -10 || sx > S.w + 20 || sy > S.h + 10) continue;
      const text = `${z.z}xx`, w = ctx.measureText(text).width;
      const b = [sx - w / 2 - 2, sy - 7, sx + w / 2 + 2, sy + 7];
      if (placed.some(q => b[0] < q[2] && b[2] > q[0] && b[1] < q[3] && b[3] > q[1])) continue;
      placed.push(b);
      ctx.lineWidth = 3; ctx.strokeStyle = C.halo; ctx.strokeText(text, sx, sy);
      ctx.fillStyle = C.ink2; ctx.fillText(text, sx, sy);
    }
    ctx.textAlign = "start"; ctx.textBaseline = "alphabetic";
  }

  // ---------- postal hubs ----------
  // data/hubs.json rows: [name, kind, city, ST, lon, lat]. Kind "P" = processing
  // plant (sorts the mail for an area), "N" = network hub for packages.
  function buildHubs(h) {
    S.hubInfo = { date: h.date };
    S.hubs = [];
    for (const [name, kind, city, st, lon, lat] of h.h) {
      const p = panelFor(lon, lat);
      const xy = S.projs[p] && S.projs[p]([lon, lat]);
      if (!xy) continue;
      S.hubs.push({ name, kind, city, st, lon, lat, p, x: xy[0], y: xy[1] });
    }
    // Draw network hubs last so their bigger markers sit on top.
    S.hubs.sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "N" ? 1 : -1));
    $("hubkey").hidden = false;
    $("hubDate").textContent = h.date ? ` USPS list dated ${h.date}.` : "";
  }

  // Nearest processing plant to a ZIP (USPS doesn't publish which plant serves
  // which ZIP, so this is "nearest", not "assigned"). Miles as the crow flies.
  function nearestHub(f) {
    if (!S.hubs || !f) return null;
    if (f._hub === undefined) {
      const [[x0, y0], [x1, y1]] = f.bbox;
      const ll = S.projs[f.p].invert([(x0 + x1) / 2, (y0 + y1) / 2]);
      let best = null, bd = Infinity;
      for (const h of S.hubs) {
        if (h.kind !== "P") continue;
        const dd = d3.geoDistance(ll, [h.lon, h.lat]);
        if (dd < bd) { bd = dd; best = h; }
      }
      f._hub = best ? { h: best, mi: Math.round(bd * 3958.8) } : null;
    }
    return f._hub;
  }
  const hubText = (n) => n ? `${n.h.name}, ${n.h.st} · ${n.mi < 1 ? "under 1" : n.mi} mi` : "";

  // Markers: small squares for plants, larger diamonds for network hubs. Names
  // show when zoomed in (hubs first, then plants), or for the hovered marker.
  // Returns the label boxes so city labels can avoid them.
  function drawHubs() {
    const placed = [];
    if (!S.hubs || !S.showHubs) return placed;
    const C = S.colors, k = S.t.k;
    const near = S.originZip && S.pinned ? nearestHub(S.originZip) : null;
    const labels = [];
    for (const h of S.hubs) {
      if (h.p && (PANELS[h.p].box[1][0] - PANELS[h.p].box[0][0]) * scale() < 60) continue;
      const [sx, sy] = toScreen(h.x, h.y);
      if (sx < -10 || sy < -10 || sx > S.w + 10 || sy > S.h + 10) continue;
      const r = (h.kind === "N" ? 5 : 3.5) + (k >= 3 ? 0.5 : 0);
      ctx.beginPath();
      if (h.kind === "N") { ctx.moveTo(sx, sy - r); ctx.lineTo(sx + r, sy); ctx.lineTo(sx, sy + r); ctx.lineTo(sx - r, sy); ctx.closePath(); }
      else ctx.rect(sx - r, sy - r, 2 * r, 2 * r);
      ctx.fillStyle = C.hub; ctx.fill();
      ctx.lineWidth = 1.5; ctx.strokeStyle = "#fff"; ctx.stroke();
      placed.push([sx - r, sy - r, sx + r, sy + r]);
      const show = h === S.hoverHub || (near && h === near.h) || (h.kind === "N" ? k >= 2.5 : k >= 4);
      if (show) labels.push([h, sx, sy, r]);
    }
    // Hovered and "nearest" labels first so they always win a spot.
    labels.sort((a, b) => (b[0] === S.hoverHub) - (a[0] === S.hoverHub) || (near && (b[0] === near.h) - (a[0] === near.h)));
    ctx.textBaseline = "middle"; ctx.lineJoin = "round";
    ctx.font = "italic 500 11px system-ui, -apple-system, 'Segoe UI', sans-serif";
    for (const [h, sx, sy, r] of labels) {
      const w = ctx.measureText(h.name).width, must = h === S.hoverHub || (near && h === near.h);
      let box = null;
      for (const lx of [sx + r + 4, sx - r - 4 - w]) {
        const b = [lx - 2, sy - 7, lx + w + 2, sy + 7];
        if (b[0] < 0 || b[2] > S.w) continue;
        if (must || !placed.some(q => b[0] < q[2] && b[2] > q[0] && b[1] < q[3] && b[3] > q[1])) { box = b; break; }
      }
      if (!box) continue;
      placed.push(box);
      ctx.lineWidth = 3; ctx.strokeStyle = C.halo; ctx.strokeText(h.name, box[0] + 2, sy);
      ctx.fillStyle = C.ink; ctx.fillText(h.name, box[0] + 2, sy);
    }
    ctx.textBaseline = "alphabetic";
    return placed;
  }

  const KIND_NAMES = { P: "Mail processing plant", N: "Network distribution center (packages)" };
  const hubTitle = (h) => `${h.name} — ${KIND_NAMES[h.kind]}, ${h.city}, ${h.st}`;

  // The hub marker under the pointer (screen pixels), if any.
  function hubAt(sx, sy) {
    if (!S.hubs || !S.showHubs) return null;
    let best = null, bd = 49;
    for (const h of S.hubs) {
      const [hx, hy] = toScreen(h.x, h.y);
      const dd = (hx - sx) ** 2 + (hy - sy) ** 2;
      if (dd < bd) { bd = dd; best = h; }
    }
    return best;
  }

  // ---------- ZIP -> city names ----------
  // data/zipnames.json (USPS ZIP Locale Detail) gives each ZIP its post office city.
  // If it's missing, fall back to the nearest labelled city: "near Denver".
  function cityOf(f) {
    if (!f) return "";
    const N = S.names;
    if (N && N.z[f.z] != null) { const [c, st] = N.c[N.z[f.z]]; return st ? `${c}, ${st}` : c; }
    if (!S.cities || !S.cities.length) return S.zip3[f.i].s;
    const cx = (f.bbox[0][0] + f.bbox[1][0]) / 2, cy = (f.bbox[0][1] + f.bbox[1][1]) / 2;
    let best = null, bd = Infinity;
    for (const c of S.cities) {
      if (c.p !== f.p || c.tier > 2) continue;
      const dd = (c.x - cx) ** 2 + (c.y - cy) ** 2;
      if (dd < bd) { bd = dd; best = c; }
    }
    return best ? `near ${best.name}` : S.zip3[f.i].s;
  }

  // Search list for the From / To boxes: "City, ST" -> ZIPs (from USPS names when
  // loaded), plus the labelled map cities (their ZIP is the one under the dot).
  function buildCityIndex() {
    const idx = new Map();
    const add = (label, z) => {
      const k = label.toLowerCase();
      if (!idx.has(k)) idx.set(k, { label, zips: [] });
      if (z && !idx.get(k).zips.includes(z)) idx.get(k).zips.push(z);
    };
    if (S.names) {
      for (const [z, i] of Object.entries(S.names.z)) {
        if (!S.byZip.has(z)) continue;
        const [c, st] = S.names.c[i];
        add(st ? `${c}, ${st}` : c, z);
      }
    }
    for (const c of S.cities || []) {
      const f = featureAt(c.x, c.y);
      if (f) add(c.name, f.z);
    }
    S.cityIndex = [...idx.values()].map(v => ({ ...v, zips: v.zips.sort() }));
  }

  function suggest(input) {
    const q = input.value.trim().toLowerCase();
    const dl = $("zipcities");
    dl.innerHTML = "";
    if (q.length < 2 || /^\d+$/.test(q) || !S.cityIndex) return;
    const starts = [], has = [];
    for (const e of S.cityIndex) {
      const l = e.label.toLowerCase();
      if (l.startsWith(q)) starts.push(e); else if (l.includes(q)) has.push(e);
      if (starts.length >= 10) break;
    }
    for (const e of [...starts, ...has].slice(0, 10)) {
      const o = document.createElement("option");
      o.value = e.label;
      o.label = e.zips.length > 1 ? `${e.zips[0]} + ${e.zips.length - 1} more ZIPs` : e.zips[0];
      dl.appendChild(o);
    }
  }

  function featureAt(vx, vy) {
    const { G, gw, gh, cells } = S.grid;
    const gx = Math.floor(vx / gw), gy = Math.floor(vy / gh);
    if (gx < 0 || gy < 0 || gx >= G || gy >= G) return null;
    for (const idx of cells[gy * G + gx]) {
      const f = S.feats[idx];
      const [[x0, y0], [x1, y1]] = f.bbox;
      if (vx < x0 || vx > x1 || vy < y0 || vy > y1) continue;
      if (S.hitCtx.isPointInPath(f.path, vx, vy, "evenodd")) return f;
    }
    return null;
  }

  // The days row for the current origin, converted to calendar days when a mailing day is set.
  function currentRow() {
    if (S.origin == null || !S.days[S.cls]) return null;
    const row = S.days[S.cls].subarray(S.origin * S.N, (S.origin + 1) * S.N);
    if (S.mailDay == null) return row;
    const t = CAL[S.mailDay];
    return row.map((d) => t[d]);
  }

  // ---------- drawing ----------
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const g = (v) => cs.getPropertyValue(v).trim();
    S.colors = {
      bucket: BUCKETS.map(b => g(b.v)),
      bg: g("--map-bg"), land: g("--land"), ink: g("--ink"), ink2: g("--ink-2"), muted: g("--muted"),
      zip3: g("--line-zip3"), zcta: g("--line-zcta"), state: g("--line-state"), halo: g("--halo"),
      hair: g("--hair"), dest: g("--dest"), hub: g("--hub"),
      empty: g("--empty"), emptyLine: g("--empty-line"), stateHalo: g("--line-state-halo"),
    };
  }

  // Diagonal hatching for land with no ZIP code (cached per theme).
  function hatchPattern() {
    if (!S.hatch || S.hatch.color !== S.colors.emptyLine) {
      const c = document.createElement("canvas"), d = 6 * (window.devicePixelRatio || 1);
      c.width = c.height = d;
      const g = c.getContext("2d");
      g.strokeStyle = S.colors.emptyLine; g.lineWidth = d / 6;
      g.beginPath(); g.moveTo(0, d); g.lineTo(d, 0); g.moveTo(-d / 2, d / 2); g.lineTo(d / 2, -d / 2);
      g.moveTo(d / 2, d * 1.5); g.lineTo(d * 1.5, d / 2); g.stroke();
      S.hatch = { color: S.colors.emptyLine, pat: ctx.createPattern(c, "repeat"), d };
    }
    return S.hatch.pat;
  }

  function resize() {
    const r = canvas.getBoundingClientRect();
    S.dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(r.width * S.dpr);
    canvas.height = Math.round(r.height * S.dpr);
    S.w = r.width; S.h = r.height;
    const k = Math.min(r.width / VW, r.height / VH);
    S.fit = { k, x: (r.width - VW * k) / 2, y: (r.height - VH * k) / 2 };
    draw();
  }

  // Screen <-> virtual coordinates.
  const scale = () => S.t.k * S.fit.k;
  const toVirtual = (sx, sy) => [(sx - S.t.x - S.t.k * S.fit.x) / scale(), (sy - S.t.y - S.t.k * S.fit.y) / scale()];

  let raf = 0;
  function draw() {
    S.rowCache = null;
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; render(); });
  }

  function render() {
    if (!S.feats.length) return;
    const C = S.colors, k = scale(), d = S.dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(d * k, 0, 0, d * k, d * (S.t.x + S.t.k * S.fit.x), d * (S.t.y + S.t.k * S.fit.y));

    // Inset frames
    ctx.lineWidth = 1 / k;
    ctx.strokeStyle = C.hair;
    PANELS.forEach((P, p) => {
      if (!p) return;
      const [[x0, y0], [x1, y1]] = P.box;
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    });

    // Land with no ZIP code: light hatching, so it doesn't read as a delivery color.
    ctx.fillStyle = C.empty; ctx.fill(S.landPath);
    const hatch = hatchPattern();
    hatch.setTransform(new DOMMatrix().scale(1 / (d * k)));
    ctx.fillStyle = hatch; ctx.fill(S.landPath);

    // Fills: one per ZIP prefix
    const row = S.rowCache || (S.rowCache = currentRow());
    for (let i = 0; i < S.N; i++) {
      if (!S.zip3Count[i]) continue;
      const dim = row && S.focus != null && bkt(row[i]) !== S.focus;
      // Spotlight: fade other groups over a solid base, so the "no ZIP code"
      // hatching underneath never shows through.
      if (dim) { ctx.fillStyle = C.land; ctx.fill(S.zip3Paths[i], "evenodd"); ctx.globalAlpha = 0.15; }
      ctx.fillStyle = row && !isNet() ? C.bucket[bkt(row[i])] : C.land;
      ctx.fill(S.zip3Paths[i], "evenodd");
      ctx.globalAlpha = 1;
    }

    // Borders
    if (S.t.k >= 4) {
      ctx.lineWidth = 0.5 / k; ctx.strokeStyle = C.zcta; ctx.stroke(S.meshZcta);
    }
    ctx.lineWidth = Math.min(1.2, 0.35 * Math.sqrt(S.t.k)) / k;
    ctx.strokeStyle = C.zip3; ctx.stroke(S.meshZip3);
    if (isNet() && S.net) { ctx.lineWidth = 1.4 / k; ctx.strokeStyle = C.ink2; ctx.stroke(S.net.mesh); }
    ctx.lineWidth = 2.6 / k; ctx.strokeStyle = C.stateHalo; ctx.stroke(S.meshState);
    ctx.lineWidth = 1.1 / k; ctx.strokeStyle = C.state; ctx.stroke(S.meshState);

    // Origin prefix outline (halo + ink), then the origin ZIP and hovered ZIP.
    if (S.origin != null) {
      if (isNet() && S.net) outline(nodeOutline(S.net.nodeOf[S.origin]), 3, 1.6);
      else outline(prefixOutline(S.origin), 2.6, 1.3);
    }
    if (S.originZip && S.pinned) outline(S.originZip.path, 2.2, 1, true);
    if (S.hover && S.hover !== S.originZip && S.hover !== S.dest) outline(S.hover.path, 2, 1);
    if (S.dest) outline(S.dest.path, 3, 1.8, false, C.dest);

    // City labels and panel labels (screen-sized text)
    ctx.setTransform(d, 0, 0, d, 0, 0);
    if (isNet()) drawNetwork();
    drawPrefixes(drawCities(isNet() ? [] : drawHubs()));
    if (S.dest) {
      const [[x0, y0], [x1, y1]] = S.dest.bbox;
      const [mx, my] = toScreen((x0 + x1) / 2, (y0 + y1) / 2);
      ctx.beginPath(); ctx.arc(mx, my, 6, 0, 2 * Math.PI);
      ctx.fillStyle = C.dest; ctx.fill();
      ctx.lineWidth = 2.5; ctx.strokeStyle = C.halo; ctx.stroke();
    }
    ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";
    ctx.fillStyle = C.muted;
    PANELS.forEach((P, p) => {
      if (!p) return;
      const [x, y] = toScreen(P.box[0][0] + 4, P.box[0][1] + 3);
      const roomy = (P.box[1][0] - P.box[0][0]) * scale() > 150;
      if (x > -100 && y > -20 && x < S.w && y < S.h) ctx.fillText(roomy ? P.name : P.short, x, y + 9);
    });
  }

  // ---------- network view ----------
  // USPS sets delivery days plant to plant, so ZIP prefixes served by the same
  // processing plant have identical First-Class days to and from everywhere.
  // Grouping identical prefixes gives the plants' service areas ("nodes").
  // Lines between nodes are colored by days. Inferred, not actual truck routes.
  function buildNetwork(mode) {
    const D = S.days.fcm, N = S.N;
    const hash = (get) => { let h = 2166136261; for (let j = 0; j < N; j++) { h ^= get(j); h = Math.imul(h, 16777619); } return h >>> 0; };
    const byKey = new Map(), nodeOf = new Int32Array(N).fill(-1), nodes = [];
    for (let i = 0; i < N; i++) {
      if (!S.zip3Count[i]) continue;
      const z = S.zip3[i];
      const c = mode === "usps" && S.sort ? S.sort.z[z.z] : null;
      const key = mode === "usps" ? `${z.p}|${c ? c[0] : "x" + i}`
        : `${z.p}|${hash(j => D[i * N + j])}|${hash(j => D[j * N + i])}`;
      let k = byKey.get(key);
      if (k === undefined) { k = nodes.length; byKey.set(key, k); nodes.push({ members: [], n: 0, x: 0, y: 0, lon: 0, lat: 0, p: z.p }); }
      nodeOf[i] = k;
      const nd = nodes[k], w = S.zip3Count[i];
      nd.members.push(i);
      if (z.c) {
        const xy = S.projs[z.p](z.c);
        if (xy) { nd.x += xy[0] * w; nd.y += xy[1] * w; nd.lon += z.c[0] * w; nd.lat += z.c[1] * w; nd.w = (nd.w || 0) + w; }
      }
      nd.n += w;
    }
    for (const nd of nodes) {
      const w = nd.w || 1;
      nd.x /= w; nd.y /= w; nd.lon /= w; nd.lat /= w;
      nd.rep = nd.members.reduce((a, b) => (S.zip3Count[b] > S.zip3Count[a] ? b : a));
    }
    // Lines between plant areas, drawn per panel.
    const mesh = new Path2D(), obj = S.topo.objects.zcta;
    PANELS.forEach((_, p) => {
      const m = topojson.mesh(S.topo, obj, (a, b) => a.properties.p === p && a !== b &&
        nodeOf[a.properties.i] !== nodeOf[b.properties.i]);
      d3.geoPath(S.projs[p]).context(mesh)(m);
    });
    return { mode, nodes, nodeOf, mesh, outlines: new Map() };
  }

  // Outer edge of one plant area (cached).
  function nodeOutline(k) {
    const O = S.net.outlines;
    if (!O.has(k)) {
      const nd = S.net.nodes[k], geoms = nd.members.flatMap(i => S.geomsByI[i]), out = new Path2D();
      if (geoms.length) {
        const m = topojson.mesh(S.topo, { type: "GeometryCollection", geometries: geoms }, (a, b) => a === b);
        d3.geoPath(S.projs[nd.p]).context(out)(m);
      }
      O.set(k, out);
    }
    return O.get(k);
  }

  // Nearest processing plant to a plant area, for its name.
  function nodePlant(nd) {
    if (!S.hubs) return null;
    if (nd.plant === undefined) {
      let best = null, bd = Infinity;
      for (const h of S.hubs) {
        if (h.kind !== "P") continue;
        const dd = d3.geoDistance([nd.lon, nd.lat], [h.lon, h.lat]);
        if (dd < bd) { bd = dd; best = h; }
      }
      nd.plant = best;
    }
    return nd.plant;
  }

  // Lines from the origin's plant area to every other one, colored by days
  // (slowest drawn first so the fast ones sit on top). With no origin, only
  // the fastest links in the class, which outline the regional networks.
  function drawNetwork() {
    if (!S.net || !S.days[S.cls]) return;
    const C = S.colors, { nodes, nodeOf } = S.net, N = S.N, D = S.days[S.cls];
    const scr = nodes.map(nd => toScreen(nd.x, nd.y));
    const curve = (a, b) => {
      const [x1, y1] = scr[a], [x2, y2] = scr[b], mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
      const dx = x2 - x1, dy = y2 - y1, bend = 0.12;
      ctx.moveTo(x1, y1); ctx.quadraticCurveTo(mx - dy * bend, my + dx * bend, x2, y2);
    };
    const line = (pairs, color, w, alpha = 1) => {
      if (!pairs.length) return;
      ctx.beginPath(); pairs.forEach(([a, b]) => curve(a, b));
      // Dark casing so the palest (fastest) colors still show on light land.
      if (alpha === 1) { ctx.lineWidth = w + 1.4; ctx.strokeStyle = C.ink; ctx.globalAlpha = 0.35; ctx.stroke(); }
      ctx.globalAlpha = alpha; ctx.lineWidth = w; ctx.strokeStyle = color; ctx.stroke();
      ctx.globalAlpha = 1;
    };
    ctx.lineCap = "round";
    const o = S.origin != null ? nodeOf[S.origin] : -1;
    if (o >= 0) {
      const row = S.rowCache || (S.rowCache = currentRow());
      const groups = BUCKETS.map(() => []);
      nodes.forEach((nd, k) => {
        if (k === o) return;
        const b = bkt(row[nd.rep]);
        if (S.focus != null && b !== S.focus) return;
        groups[b].push([o, k]);
      });
      for (let b = BUCKETS.length - 1; b >= 0; b--) line(groups[b], C.bucket[b], b === NONE ? 0.6 : 1.3);
    } else {
      const F = S.net.fast || (S.net.fast = {});
      if (!F[S.cls]) {
        let fastest = 255;
        for (const a of nodes) for (const b of nodes) { const v = D[a.rep * N + b.rep]; if (a !== b && v && v < fastest) fastest = v; }
        const pairs = [];
        nodes.forEach((a, i) => nodes.forEach((b, j) => {
          if (j > i && a.p === b.p && D[a.rep * N + b.rep] === fastest && D[b.rep * N + a.rep] === fastest) pairs.push([i, j]);
        }));
        F[S.cls] = { fastest, pairs };
      }
      line(F[S.cls].pairs, C.ink2, 0.8, 0.55);
    }
    // Plant-area dots, sized by ZIP codes served.
    nodes.forEach((nd, k) => {
      const [x, y] = scr[k];
      if (x < -20 || y < -20 || x > S.w + 20 || y > S.h + 20) return;
      const r = 2 + Math.sqrt(nd.n) / 3.2;
      ctx.beginPath(); ctx.arc(x, y, k === o ? r + 2 : r, 0, 2 * Math.PI);
      ctx.fillStyle = k === o ? C.dest : C.ink; ctx.globalAlpha = k === o ? 1 : 0.75; ctx.fill();
      ctx.globalAlpha = 1; ctx.lineWidth = 1.2; ctx.strokeStyle = C.halo; ctx.stroke();
    });
    if (o >= 0) drawChain(scr[o]);
  }

  const isNet = () => S.view !== "zip";

  // ---------- official sorting chain (USPS labeling lists) ----------
  // data/sort.json: f = facilities [level, name, ST, zip, lon, lat]; z = prefix -> [SCF, ADC, hub].
  function buildSort(d) {
    const f = d.f.map(([level, name, st, zip, lon, lat]) => {
      const p = lon == null ? -1 : panelFor(lon, lat);
      const xy = p >= 0 && S.projs[p] ? S.projs[p]([lon, lat]) : null;
      return { level, name, st, zip, p, x: xy && xy[0], y: xy && xy[1] };
    });
    S.sort = { date: d.date, f, z: d.z };
    $("viewbtns").querySelector('[data-v="usps"]').hidden = false;
  }
  const chainOf = (i) => {
    const c = S.sort && S.sort.z[S.zip3[i].z];
    return c ? c.map(k => (k >= 0 ? S.sort.f[k] : null)) : null;
  };
  const LEVEL_NAMES = { SCF: "plant", ADC: "regional center", NDC: "package hub (NDC)", RPDC: "package hub (RPDC)" };
  function chainText(i) {
    const c = chainOf(i);
    if (!c) return "";
    const [scf, adc, hub] = c;
    return [scf && `Plant: ${scf.name}`, adc && `regional center: ${adc.name}`,
            hub && `packages: ${hub.name} ${hub.level}`].filter(Boolean).join(" → ");
  }

  // The origin's chain on the map: area -> SCF -> ADC -> package hub, as one magenta path.
  function drawChain(fromXY) {
    const c = S.origin != null && chainOf(S.origin);
    if (!c) return;
    const C = S.colors, pts = [{ x: fromXY[0], y: fromXY[1], tags: [] }];
    c.forEach((f) => {
      if (!f || f.x == null) return;
      const [x, y] = toScreen(f.x, f.y), last = pts[pts.length - 1];
      if (Math.hypot(last.x - x, last.y - y) < 4) last.tags.push(f.level);
      else pts.push({ x, y, tags: [f.level], name: f.name });
      if (!last.name && Math.hypot(last.x - x, last.y - y) < 4) last.name = f.name;
    });
    ctx.save();
    ctx.setLineDash([7, 4]); ctx.lineCap = "round";
    ctx.beginPath(); pts.forEach((q, j) => (j ? ctx.lineTo(q.x, q.y) : ctx.moveTo(q.x, q.y)));
    ctx.lineWidth = 5; ctx.strokeStyle = C.halo; ctx.stroke();
    ctx.lineWidth = 2.5; ctx.strokeStyle = C.dest; ctx.stroke();
    ctx.restore();
    ctx.font = "600 11px system-ui, -apple-system, 'Segoe UI', sans-serif";
    ctx.textBaseline = "middle"; ctx.lineJoin = "round";
    pts.forEach((q) => {
      if (!q.tags.length) return;
      ctx.beginPath(); ctx.rect(q.x - 5, q.y - 5, 10, 10);
      ctx.fillStyle = C.dest; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = C.halo; ctx.stroke();
      const t = `${q.name || ""} ${q.tags.join(" · ")}`.trim();
      ctx.lineWidth = 3.5; ctx.strokeStyle = C.halo; ctx.strokeText(t, q.x + 9, q.y);
      ctx.fillStyle = C.ink; ctx.fillText(t, q.x + 9, q.y);
    });
    ctx.textBaseline = "alphabetic";
  }
  async function setView(v) {
    if (v !== "zip") {
      if (!S.days.fcm) S.days.fcm = new Uint8Array(await fetch("data/days_fcm.bin").then(r => r.arrayBuffer()));
      S.net = S.nets[v] || (S.nets[v] = buildNetwork(v));
    }
    S.view = v;
    for (const b of document.querySelectorAll("#viewbtns button")) b.setAttribute("aria-checked", b.dataset.v === v);
    $("netNote").hidden = v === "zip";
    if (v !== "zip") netNote();
    draw(); legend();
  }
  document.querySelectorAll("#viewbtns button").forEach(b => b.addEventListener("click", () => setView(b.dataset.v)));

  function netNote() {
    const n = S.net, nd = S.origin != null ? n.nodes[n.nodeOf[S.origin]] : null;
    if (n.mode === "usps") {
      const f = nd && chainOf(S.origin);
      $("netNote").innerHTML = nd
        ? `<b>${f && f[0] ? esc(f[0].name + ", " + f[0].st) : S.zip3[S.origin].z + "xx"} plant (SCF):</b> serves ${nd.members.length}
           ZIP prefix${nd.members.length > 1 ? "es" : ""} (${nd.members.slice(0, 8).map(i => S.zip3[i].z + "xx").join(", ")}${nd.members.length > 8 ? "…" : ""}),
           ${nd.n.toLocaleString()} ZIP codes. ${chainText(S.origin)}. Lines show days to every other plant.`
        : `<b>USPS plants.</b> Each dot is one of USPS's ${n.nodes.length} local plants (SCFs), from the official labeling
           lists${S.sort.date ? ` dated ${S.sort.date}` : ""}, placed at the middle of the ZIP prefixes it serves. Hover a ZIP to see
           its sorting chain (the magenta line: plant → regional center → package hub) and days to every other plant.`;
      return;
    }
    const plant = nd && nodePlant(nd);
    $("netNote").innerHTML = nd
      ? `<b>Plant area of ${S.zip3[S.origin].z}xx:</b> ${nd.members.length} ZIP prefix${nd.members.length > 1 ? "es" : ""}
         (${nd.members.slice(0, 8).map(i => S.zip3[i].z + "xx").join(", ")}${nd.members.length > 8 ? "…" : ""}),
         ${nd.n.toLocaleString()} ZIP codes${plant ? `. Nearest plant: ${esc(plant.name)}, ${plant.st}` : ""}.
         Lines show days to every other plant area.`
      : `<b>Network view.</b> Each dot is a plant area: ZIP prefixes with identical First-Class delivery days to and from
         everywhere, so most likely sorted at the same plant (${n.nodes.length} areas). Lines join areas that reach each
         other in the fewest days. Hover a ZIP to see its plant area's links. Inferred from USPS targets; not actual truck routes.`;
  }

  // Outer edge of one ZIP prefix (cached): arcs used by only one of its ZIPs.
  const outlineCache = new Map();
  function prefixOutline(i) {
    if (!outlineCache.has(i)) {
      const geoms = S.geomsByI[i], out = new Path2D();
      if (geoms.length) {
        const m = topojson.mesh(S.topo, { type: "GeometryCollection", geometries: geoms }, (a, b) => a === b);
        d3.geoPath(S.projs[geoms[0].properties.p]).context(out)(m);
      }
      outlineCache.set(i, out);
    }
    return outlineCache.get(i);
  }

  function outline(path, haloW, inkW, fillMark, color) {
    const k = scale();
    ctx.lineJoin = "round";
    ctx.lineWidth = haloW / k; ctx.strokeStyle = S.colors.halo; ctx.stroke(path);
    ctx.lineWidth = inkW / k; ctx.strokeStyle = color || S.colors.ink; ctx.stroke(path);
    if (fillMark) { ctx.fillStyle = S.colors.ink; ctx.globalAlpha = 0.25; ctx.fill(path, "evenodd"); ctx.globalAlpha = 1; }
  }
  const toScreen = (vx, vy) => [vx * scale() + S.t.x + S.t.k * S.fit.x, vy * scale() + S.t.y + S.t.k * S.fit.y];

  function unitsText() {
    $("units").textContent = S.mailDay == null
      ? "chance it arrives in…"
      : `chance it arrives on… if mailed ${WEEKDAY_NAMES[S.mailDay]}`;
  }

  // Legend labels. With a mailing day picked, buckets are arrival days:
  // calendar day n after mailing on weekday m lands on weekday (m + n) % 7.
  function bucketLabel(j) {
    const m = S.mailDay;
    if (m == null || j === NONE) return BUCKETS[j].label;
    if (j < 6) return WEEKDAY_NAMES[(m + j + 1) % 7];
    return `Next ${WEEKDAY_NAMES[m]} or later`;
  }

  function setMailDay(v) {
    S.mailDay = v === "" || v == null ? null : +v;
    $("mailday").value = S.mailDay == null ? "" : String(S.mailDay);
    unitsText(); writeHash(); draw(); legend(); showTip(); status();
  }
  $("mailday").addEventListener("change", (e) => setMailDay(e.target.value));

  // ---------- on-time performance ----------
  // USPS reports how much mail actually arrived on time, per district (or area),
  // per quarter. d = USPS target days for the pair, used to pick the 2-day or
  // 3-to-5-day column for First-Class Mail.
  function perfFor(zip3code, d) {
    const P = S.perf, c = P && P.classes[S.cls];
    if (!c) return null;
    const dist = P.zip3[zip3code];
    if (!dist) return null;
    const regionKey = c.level === "area" ? P.districts[dist]?.area : dist;
    const rec = regionKey && c.regions[regionKey];
    if (!rec) return null;
    let k = "all";
    if (c.by_std) {
      k = d <= 1 ? "1" : d === 2 ? "2" : "3";
      if (rec.on[k] == null && d != null) return null;
    }
    const name = c.level === "area" ? `${P.areas[regionKey] || regionKey} area` : `${P.districts[dist].name} district`;
    return { name, on: rec.on, w: rec.w, k, nation: c.nation };
  }
  const pct = (v) => (v == null ? "—" : `${Math.round(v)}%`);
  const STD_NAME = { 1: "overnight", 2: "2-day", 3: "3–5-day", all: "" };

  const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // One cumulative meter: on time, then within +1 day, then +3 days, on a 0-100% track,
  // with a tick at the national on-time figure. Numbers are always printed beside it.
  function meterRow(label, p, emphasize) {
    const on = p.on[p.k], w = p.w[p.k] || [], nat = p.nation[p.k];
    const w1 = w[0] ?? on, w3 = w[2] ?? w1;
    const seg = (v) => `${Math.max(0, v).toFixed(1)}%`;
    return `<div class="mrow${emphasize ? " em" : ""}">
      <div class="mlab">${esc(label)}</div>
      <div class="mval"><b>${pct(on)}</b><span class="muted">${w[0] != null ? ` ${pct(w1)} · ${pct(w3)}` : ""}</span></div>
      <div class="meter" role="img" aria-label="${esc(label)}: ${pct(on)} on time${w[0] != null ? `, ${pct(w1)} within 1 extra day, ${pct(w3)} within 3` : ""}${nat != null ? `; national ${pct(nat)}` : ""}">
        <span class="m0" style="width:${seg(on)}"></span><span class="m1" style="width:${seg(w1 - on)}"></span><span class="m3" style="width:${seg(w3 - w1)}"></span>
        ${nat != null ? `<i class="nat" style="left:${seg(nat)}" title="National: ${pct(nat)}"></i>` : ""}
      </div>
    </div>`;
  }

  // The current destination being compared: the hovered ZIP while pinned, else the typed To ZIP.
  const target = () => (S.pinned && S.originZip ? (S.hover && S.hover !== S.originZip ? S.hover : S.dest) : null);

  function perfPanel() {
    const box = $("perf");
    const P = S.perf;
    if (!P) { box.hidden = true; return; }
    box.hidden = false;
    const c = P.classes[S.cls];
    const cls = S.meta.classes.find(x => x.key === S.cls).label;
    const head = `<div class="ph"><h3>How often it's on time</h3></div>`;
    if (!c) { box.innerHTML = head + `<p class="muted">USPS doesn't publish on-time results by region for ${cls}.</p>`; return; }
    if (S.origin == null) { box.innerHTML = head + `<p class="muted">Hover a ZIP to see USPS's on-time record for its ${c.level}.</p>`; return; }
    const z3 = S.zip3[S.origin].z, t = target();
    const d = t ? S.days[S.cls][S.origin * S.N + t.i] : 0;
    const pairK = c.by_std && d ? (d <= 1 ? "1" : d === 2 ? "2" : "3") : "all";
    const keys = c.by_std ? ["2", "3"] : ["all"];
    const from = perfFor(z3, 3);
    let rows = "";
    if (from) {
      rows += `<div class="msub">From ${esc(from.name)}</div>`;
      for (const k of keys) {
        const p = perfFor(z3, k === "2" ? 2 : 3);
        if (p) rows += meterRow(c.by_std ? `${STD_NAME[k]} mail` : "All mail", p, t && k === pairK);
      }
    }
    if (t && d) {
      const to = perfFor(S.zip3[t.i].z, d);
      if (to && (!from || to.name !== from.name)) {
        rows += `<div class="msub">To ${esc(to.name)}</div>` + meterRow(c.by_std ? `${STD_NAME[to.k]} mail` : "All mail", to, true);
      }
    }
    if (!rows) { box.innerHTML = head + `<p class="muted">No USPS results for this ${c.level}.</p>`; return; }
    box.innerHTML = head + `<p class="muted small">${esc(c.label)} · USPS results, ${P.quarter}</p>${rows}
      <div class="mkey"><span><i class="k0"></i>On time</span><span><i class="k1"></i>+1 day</span><span><i class="k3"></i>+3 days</span><span><i class="kn"></i>National</span></div>
      <p class="muted small"><a href="${c.source}">USPS report</a></p>`;
  }

  // ---------- side panel ----------
  // Chance that a piece from the origin arrives in each legend group, if mailed
  // to a random ZIP code. Each destination contributes its USPS target, spread out
  // by how USPS actually did in that destination's district: share on time, then
  // +1, +2, +3 days late (anything later counted as 4 days late). Without on-time
  // data (Priority Mail, Ground Advantage) it is just the share of ZIP codes.
  function chances() {
    const raw = S.days[S.cls].subarray(S.origin * S.N, (S.origin + 1) * S.N);
    const p = new Float64Array(BUCKETS.length);
    let total = 0, measured = 0, withStd = 0;
    const cal = S.mailDay == null ? null : CAL[S.mailDay];
    const add = (dd, w) => { p[bkt(cal ? cal[Math.min(dd, 255)] : dd)] += w; };
    for (let j = 0; j < S.N; j++) {
      const n = S.zip3Count[j], d = raw[j];
      if (!n) continue;
      total += n;
      if (!d) { p[NONE] += n; continue; }
      withStd += n;
      const r = perfFor(S.zip3[j].z, d);
      const on = r && r.on[r.k];
      if (on == null) { add(d, n); continue; }
      measured += n;
      const w = r.w[r.k] || [];
      let prev = 0;
      [on, w[0], w[1], w[2], 100].forEach((c, late) => {
        c = Math.max(prev, c ?? prev);
        if (c > prev) add(d + late, n * (c - prev) / 100);
        prev = c;
      });
    }
    for (let j = 0; j < p.length; j++) p[j] = total ? p[j] / total : 0;
    return { p, measured: withStd ? measured / withStd : 0 };
  }
  const chanceText = (v) => (v <= 0 ? "0%" : v < 0.005 ? "<1%" : `${Math.round(v * 100)}%`);

  function legend() {
    S.rowCache = null;
    const ul = $("legend");
    const counts = new Array(BUCKETS.length).fill(0);
    const row = S.rowCache || (S.rowCache = currentRow());
    if (row) for (let i = 0; i < S.N; i++) counts[bkt(row[i])] += S.zip3Count[i];
    const ch = row ? chances() : null;
    const max = ch ? Math.max(1e-9, ...ch.p) : 1;
    let cum = 0;
    $("legendNote").textContent = !row
      ? "Pick a starting ZIP to see the chance your mail arrives in each number of days."
      : ch.measured > 0.5
        ? `Chance a piece mailed from ${S.originZip.z} to a random ZIP code arrives in that many days: USPS targets, adjusted by USPS's own on-time results for each destination district (${S.perf.quarter}). Hover a row for the number of ZIP codes and to spotlight them on the map.`
        : `USPS publishes no on-time results for this mail class, so this is the share of ZIP codes at each USPS target. Hover a row for the number of ZIP codes and to spotlight them on the map.`;
    const t = target();
    const hoverB = row && (t || S.hover) ? bkt(row[(t || S.hover).i]) : -1;
    ul.innerHTML = "";
    BUCKETS.forEach((b, j) => {
      // USPS doesn't count Sundays, so nothing is scheduled to arrive on one.
      if (S.mailDay != null && j < 6 && (S.mailDay + j + 1) % 7 === 6) return;
      const li = document.createElement("li");
      if (row && !counts[j] && !(ch && ch.p[j] >= 0.005)) li.className = "zero";
      if (j === hoverB) li.classList.add("hit");
      if (j === S.focus) li.classList.add("focus");
      const pj = ch ? ch.p[j] : 0;
      if (j !== NONE) cum += pj;
      li.innerHTML = `<span class="sw" style="background:${S.colors.bucket[j]}"></span>
        <span class="lab">${bucketLabel(j)}</span>
        <span class="bar">${row ? `<i style="width:${(100 * pj / max).toFixed(1)}%"></i>` : ""}</span>
        <span class="n">${row ? `<span class="pc">${chanceText(pj)}</span><span class="ct">${counts[j].toLocaleString()} ZIPs</span>` : ""}</span>`;
      if (row) li.title = j === NONE
        ? `${counts[j].toLocaleString()} ZIP codes have no USPS target from here`
        : `${counts[j].toLocaleString()} ZIP codes have this USPS target` +
          (j < 6 ? ` · ${chanceText(cum)} chance it arrives ${S.mailDay == null ? "within" : "by"} ${bucketLabel(j)}` : "");
      // Hover a legend row to spotlight those ZIPs on the map.
      li.onmouseenter = () => { S.focus = j; draw(); };
      li.onmouseleave = () => { S.focus = null; draw(); };
      ul.appendChild(li);
    });

    const o = S.origin == null ? null : S.zip3[S.origin];
    const oz = S.originZip;
    $("originZip").textContent = oz ? oz.z : "—";
    $("originMeta").textContent = o
      ? `${cityOf(oz)} · ${o.s || "—"} · prefix ${o.z}xx${S.pinned ? " · pinned" : ""}`
      : "Hover over the map, or click a ZIP to pin it.";
    $("unpin").hidden = !S.pinned;
    const nh = nearestHub(oz), ct = oz && chainText(oz.i);
    $("originHub").hidden = !(nh || ct);
    if (ct) $("originHub").innerHTML = `<span class="hubmark"></span>USPS sorting: ${esc(ct)}`;
    else if (nh) $("originHub").innerHTML = `<span class="hubmark"></span>Nearest mail plant: ${esc(hubText(nh))}`;
    perfPanel();
    readout();
    if (isNet() && S.net) netNote();
  }

  // Hover / trip readout in the right margin (replaces a floating tooltip).
  function readout() {
    const box = $("readout");
    const a = S.originZip, t = target();
    const label = S.meta.classes.find(c => c.key === S.cls).label;
    if (!a) {
      box.innerHTML = `<p class="muted">Hover over the map to pick a starting ZIP, or type ZIPs or cities above.</p>`;
      return;
    }
    if (!S.pinned) {
      box.innerHTML = `<span class="muted cap">From</span>
        <div class="route">${a.z} <span class="city">${esc(cityOf(a))}</span></div>
        <p class="muted">Click to pin this ZIP, then hover anywhere to compare.</p>`;
      return;
    }
    if (!t) {
      box.innerHTML = `<span class="muted cap">From</span>
        <div class="route">${a.z} <span class="city">${esc(cityOf(a))}</span></div>
        <p class="muted">Hover a ZIP, or type a To ZIP or city above.</p>`;
      return;
    }
    const d = S.days[S.cls] ? S.days[S.cls][a.i * S.N + t.i] : 0;
    let value, cap, sw;
    if (!d) { value = "No standard"; cap = "USPS lists no target for this pair"; sw = S.colors.bucket[NONE]; }
    else if (S.mailDay != null) {
      const r = arrival(S.mailDay, d);
      value = WEEKDAY_NAMES[r.wd] + (r.cal >= 7 ? (r.cal >= 14 ? ` (in ${Math.floor(r.cal / 7)} wks)` : " next week") : "");
      cap = `arrives, if mailed ${WEEKDAY_NAMES[S.mailDay]} · ${r.cal} days after mailing`;
      sw = S.colors.bucket[bkt(r.cal)];
    } else { value = daysText(d); cap = "USPS target (delivery days, no Sundays or holidays)"; sw = S.colors.bucket[bucketOf(d)]; }
    const isTrip = t === S.dest;
    box.innerHTML = `${isTrip ? `<button class="clear" id="clearTrip">Clear</button>` : ""}
      <span class="muted cap">${isTrip ? "Trip" : "Hovered"} · ${esc(label)}</span>
      <div class="route">${a.z} → <span class="to">${t.z}</span></div>
      <div class="cities">${esc(cityOf(a))} → ${esc(cityOf(t))}</div>
      ${nearestHub(a) && nearestHub(t) ? `<div class="plants"><span class="hubmark"></span>Nearest plants: ${esc(nearestHub(a).h.name)} → ${esc(nearestHub(t).h.name)}</div>` : ""}
      <div class="hero"><span class="sw" style="background:${sw}"></span>${esc(value)}</div>
      <div class="muted">${esc(cap)}${S.mailDay == null && d ? ` · pick “Mailed on” for the weekday` : ""}</div>`;
    if (isTrip) $("clearTrip").onclick = () => setDest(null);
  }

  function showTip() { /* hover details now live in the side panel */ }

  function status() {
    const c = S.meta.classes.find(c => c.key === S.cls);
    const o = S.originZip;
    const when = S.mailDay == null ? "" : `, mailed on a ${WEEKDAY_NAMES[S.mailDay]}`;
    $("status").textContent = o
      ? `${c.label}${when} from ${o.z} (${S.zip3[o.i].s}). ${S.pinned ? "Pinned. Hover other ZIPs to see days." : "Click to pin."}`
      : `${S.meta.n_zcta.toLocaleString()} ZIP codes in ${S.N} prefixes. Hover over the map to choose an origin.`;
  }

  // ---------- interaction ----------
  function setOrigin(f) {
    const changed = S.originZip !== f;
    S.originZip = f;
    S.origin = f ? f.i : null;
    if (changed) { legend(); status(); }
  }

  // ---------- fastest / slowest ZIP prefixes ----------
  // Average USPS days to (or from) every ZIP code in the country, per prefix.
  // Click a row to start from that prefix.
  function rankPanel() {
    const box = $("rank"), R = S.rank && S.rank.classes[S.cls];
    box.hidden = !(R && R.all);
    if (box.hidden) return;
    const way = S.rankWay || "send", area = S.rankArea || "l48";
    const V = R[area], d = V[way], fast = R.fast_days, l48 = area === "l48";
    const row = ([z, name, days, share]) => `<li><button type="button" data-z="${z}">
        <span class="rz">${z}xx</span><span class="rn">${esc(name)}</span>
        <span class="rd">${days.toFixed(2)} days<small>${Math.round(share)}% in ${fast} days</small></span></button></li>`;
    const seg = (key, cur, opts, label) => `<div class="seg" role="radiogroup" aria-label="${label}">${
      opts.map(([v, t]) => `<button type="button" role="radio" data-${key}="${v}" aria-checked="${cur === v}">${t}</button>`).join("")}</div>`;
    box.innerHTML = `<h3>Fastest and slowest ZIP prefixes</h3>
      ${seg("w", way, [["send", "Mailing from"], ["recv", "Receiving at"]], "Direction")}
      ${seg("a", area, [["l48", "Lower 48"], ["all", "All U.S."]], "Area")}
      <p class="muted">${esc(R.label)}: average USPS days ${way === "send" ? "to" : "from"} every ZIP code
        ${l48 ? "in the lower 48 (Alaska, Hawaii and the territories left out at both ends)" : "in the country"}.
        Average ${V.national.toFixed(2)} days.</p>
      <h4>Fastest</h4><ol>${d.fast.map(row).join("")}</ol>
      <h4>Slowest</h4><ol>${d.slow.map(row).join("")}</ol>
      <p class="muted">Click a row to start from there.</p>`;
    box.querySelectorAll("[data-w]").forEach(b => b.onclick = () => { S.rankWay = b.dataset.w; rankPanel(); });
    box.querySelectorAll("[data-a]").forEach(b => b.onclick = () => { S.rankArea = b.dataset.a; rankPanel(); });
    box.querySelectorAll("li button").forEach(b => b.onclick = () => {
      const f = S.feats.find(f => S.zip3[f.i].z === b.dataset.z);
      if (f) { if (S.pinned) setDest(null); pinZip(f, true); }
    });
  }

  function pinZip(f, zoomTo) {
    $("fromZip").value = f.z;
    S.pinned = true;
    setOrigin(f);
    legend(); status(); writeHash(); draw();
    if (zoomTo) zoomToFeature(f);
  }

  function unpin() {
    S.pinned = false;
    S.dest = null;
    $("fromZip").value = ""; $("toZip").value = "";
    setOrigin(S.hover);
    legend(); status(); writeHash(); draw(); showTip();
  }

  function onMove(ev) {
    const r = canvas.getBoundingClientRect();
    const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
    const f = featureAt(...toVirtual(sx, sy));
    const hh = hubAt(sx, sy);
    if (hh !== S.hoverHub) { S.hoverHub = hh; canvas.title = hh ? hubTitle(hh) : ""; draw(); }
    if (f !== S.hover) {
      S.hover = f;
      if (!S.pinned && f) setOrigin(f);
      legend(); draw();
    }
    showTip(sx, sy);
  }

  canvas.addEventListener("pointermove", (ev) => { if (ev.pointerType === "mouse" || ev.buttons === 0) onMove(ev); });
  canvas.addEventListener("pointerleave", () => {
    S.hover = null; S.hoverHub = null; tip.hidden = true;
    if (!S.pinned) setOrigin(null);
    legend(); draw();
  });
  canvas.addEventListener("click", (ev) => {
    if (ev.defaultPrevented) return;
    onMove(ev);
    const f = S.hover;
    if (!f) return;
    if (S.pinned && S.originZip === f) unpin();
    else if (!S.pinned || ev.pointerType === "mouse") pinZip(f);
    showTip();
  });
  $("unpin").addEventListener("click", unpin);
  $("showHubs").addEventListener("change", (e) => { S.showHubs = e.target.checked; S.hoverHub = null; draw(); });
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.pinned) unpin(); });

  // Zoom and pan
  const zoom = d3.zoom()
    .scaleExtent([1, 60])
    .translateExtent([[-200, -200], [5000, 5000]])
    .on("zoom", (e) => { S.t = e.transform; tip.hidden = true; draw(); });
  d3.select(canvas).call(zoom).on("dblclick.zoom", null);
  const zoomBy = (f) => d3.select(canvas).transition().duration(250).call(zoom.scaleBy, f);
  $("zin").onclick = () => zoomBy(2);
  $("zout").onclick = () => zoomBy(0.5);
  $("zreset").onclick = () => d3.select(canvas).transition().duration(350).call(zoom.transform, d3.zoomIdentity);

  function zoomToFeature(f) {
    const [[x0, y0], [x1, y1]] = S.zip3Bounds ? S.zip3Bounds(f.i) : f.bbox;
    const fk = S.fit.k;
    const k = Math.max(1, Math.min(6, 0.35 / Math.max((x1 - x0) * fk / S.w, (y1 - y0) * fk / S.h)));
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    const t = d3.zoomIdentity.translate(S.w / 2 - k * (cx * fk + S.fit.x), S.h / 2 - k * (cy * fk + S.fit.y)).scale(k);
    d3.select(canvas).transition().duration(600).call(zoom.transform, t);
  }
  S.zip3Bounds = (i) => {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const f of S.feats) if (f.i === i) {
      x0 = Math.min(x0, f.bbox[0][0]); y0 = Math.min(y0, f.bbox[0][1]);
      x1 = Math.max(x1, f.bbox[1][0]); y1 = Math.max(y1, f.bbox[1][1]);
    }
    return [[x0, y0], [x1, y1]];
  };

  // ---------- From / To ZIP boxes ----------
  // Find the drawn ZIP for what was typed. A 3-digit prefix, or a ZIP with no
  // shape (PO-box only), falls back to a ZIP in the same prefix: same days.
  function resolveZip(v) {
    v = (v || "").trim();
    const digits = v.match(/\b(\d{5}|\d{3})\b/);
    if (!digits && v) {
      const k = v.toLowerCase().replace(/\s+\d.*$/, "");
      const e = S.cityIndex && (S.cityIndex.find(x => x.label.toLowerCase() === k) ||
        S.cityIndex.find(x => x.label.toLowerCase().startsWith(k)));
      if (e) return { f: S.byZip.get(e.zips[0]), note: e.zips.length > 1 ? `${e.label}: using ${e.zips[0]} (one of ${e.zips.length} ZIPs).` : "" };
      return { f: null, note: `No ZIP or city found for “${v}”.` };
    }
    if (!digits) return { f: null, note: "" };
    v = digits[1];
    let f = S.byZip.get(v.padStart(5, "0"));
    if (f) return { f, note: "" };
    f = S.feats.find(x => x.z.startsWith(v.slice(0, 3)));
    if (!f) return { f: null, note: `No ZIP found for “${v}”.` };
    return { f, note: v.length === 5 ? `${v} has no map shape (PO-box only?); using ${f.z}, same delivery days.` : "" };
  }

  function setDest(f, zoom) {
    S.dest = f || null;
    $("toZip").value = f ? f.z : "";
    writeHash(); legend(); draw();
    if (zoom && f && S.originZip) zoomToPair(S.originZip, f);
  }

  for (const id of ["fromZip", "toZip"]) $(id).addEventListener("input", (e) => suggest(e.target));

  $("trip").addEventListener("submit", (e) => {
    e.preventDefault();
    const from = resolveZip($("fromZip").value), to = resolveZip($("toZip").value);
    const notes = [from.note, to.note].filter(Boolean);
    if (from.f) { S.hover = from.f; pinZip(from.f, !to.f); $("fromZip").value = from.f.z; }
    if (to.f) {
      if (!from.f && !S.originZip) notes.push("Type a From ZIP too, or click the map to pick one.");
      setDest(to.f, true);
    } else if (!$("toZip").value.trim()) setDest(null);
    if (notes.length) $("status").textContent = notes.join(" ");
  });

  function zoomToPair(a, b) {
    if (a.p !== b.p) { zoomToFeature(b); return; } // different map boxes: just show the destination
    const x0 = Math.min(a.bbox[0][0], b.bbox[0][0]), y0 = Math.min(a.bbox[0][1], b.bbox[0][1]);
    const x1 = Math.max(a.bbox[1][0], b.bbox[1][0]), y1 = Math.max(a.bbox[1][1], b.bbox[1][1]);
    const fk = S.fit.k;
    const k = Math.max(1, Math.min(6, 0.7 / Math.max((x1 - x0) * fk / S.w, (y1 - y0) * fk / S.h)));
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    const t = d3.zoomIdentity.translate(S.w / 2 - k * (cx * fk + S.fit.x), S.h / 2 - k * (cy * fk + S.fit.y)).scale(k);
    d3.select(canvas).transition().duration(600).call(zoom.transform, t);
  }

  // URL hash: #fcm/80202
  function readHash() {
    // #fcm/80202/fri  (ZIP and mailing day are optional; #fcm//fri works too)
    // #fcm/80302/fri/10001  (class / from ZIP / mailing day / to ZIP; all but the class optional)
    const [cls, zip, day, to] = location.hash.replace(/^#/, "").split("/");
    const wd = WEEKDAYS.findIndex(w => w.toLowerCase() === (day || "").toLowerCase());
    return { cls, zip, day: wd >= 0 ? wd : null, to };
  }
  function writeHash() {
    const zip = S.pinned && S.originZip ? S.originZip.z : "";
    const day = S.mailDay == null ? "" : WEEKDAYS[S.mailDay].toLowerCase();
    const to = S.dest && zip ? S.dest.z : "";
    const h = `#${S.cls}${zip || day || to ? "/" + zip : ""}${day || to ? "/" + day : ""}${to ? "/" + to : ""}`;
    if (location.hash !== h) history.replaceState(null, "", h);
  }

  window.addEventListener("resize", () => { resize(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readColors(); legend(); draw(); });

  load().catch((err) => {
    console.error(err);
    $("loading").textContent = "Could not load the map data. Run build/build_data.py first (see README).";
  });
})();
