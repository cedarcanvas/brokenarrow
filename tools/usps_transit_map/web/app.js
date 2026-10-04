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
  // Lower 48 on top, the inset boxes in a row underneath.
  const VW = 1000, VH = 660;
  const PANELS = [
    { name: "Lower 48", short: "", rot: [96, 0], box: [[10, 10], [990, 540]] },
    { name: "Alaska", short: "AK", rot: [152, 0], box: [[10, 556], [250, 650]] },
    { name: "Hawaii", short: "HI", rot: [157, 0], box: [[262, 556], [442, 650]] },
    { name: "Puerto Rico & USVI", short: "PR · VI", rot: [66, 0], box: [[454, 556], [634, 650]] },
    { name: "Guam & N. Mariana Is.", short: "GU · MP", rot: [-145, 0], box: [[646, 556], [816, 650]] },
    { name: "American Samoa", short: "AS", rot: [170, 0], box: [[828, 556], [990, 650]] },
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
    readColors();
    resize();
    await setClass(S.cls);
    $("loading").remove();
    if (hash.zip && S.byZip.has(hash.zip)) pinZip(S.byZip.get(hash.zip));
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
    writeHash(); draw(); legend(); showTip(); status();
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
      hair: g("--hair"),
    };
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

    // Fills: one per ZIP prefix
    const row = S.rowCache || (S.rowCache = currentRow());
    for (let i = 0; i < S.N; i++) {
      if (!S.zip3Count[i]) continue;
      ctx.fillStyle = row ? C.bucket[bkt(row[i])] : C.land;
      ctx.fill(S.zip3Paths[i], "evenodd");
    }

    // Borders
    if (S.t.k >= 4) {
      ctx.lineWidth = 0.5 / k; ctx.strokeStyle = C.zcta; ctx.stroke(S.meshZcta);
    }
    ctx.lineWidth = Math.min(1.2, 0.35 * Math.sqrt(S.t.k)) / k;
    ctx.strokeStyle = C.zip3; ctx.stroke(S.meshZip3);
    ctx.lineWidth = 0.9 / k; ctx.strokeStyle = C.state; ctx.stroke(S.meshState);

    // Origin prefix outline (halo + ink), then the origin ZIP and hovered ZIP.
    if (S.origin != null) outline(prefixOutline(S.origin), 2.6, 1.3);
    if (S.originZip && S.pinned) outline(S.originZip.path, 2.2, 1, true);
    if (S.hover && S.hover !== S.originZip) outline(S.hover.path, 2, 1);

    // Panel labels (screen-sized text)
    ctx.setTransform(d, 0, 0, d, 0, 0);
    ctx.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";
    ctx.fillStyle = C.muted;
    PANELS.forEach((P, p) => {
      if (!p) return;
      const [x, y] = toScreen(P.box[0][0] + 4, P.box[0][1] + 3);
      const roomy = (P.box[1][0] - P.box[0][0]) * scale() > 150;
      if (x > -100 && y > -20 && x < S.w && y < S.h) ctx.fillText(roomy ? P.name : P.short, x, y + 9);
    });
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

  function outline(path, haloW, inkW, fillMark) {
    const k = scale();
    ctx.lineJoin = "round";
    ctx.lineWidth = haloW / k; ctx.strokeStyle = S.colors.halo; ctx.stroke(path);
    ctx.lineWidth = inkW / k; ctx.strokeStyle = S.colors.ink; ctx.stroke(path);
    if (fillMark) { ctx.fillStyle = S.colors.ink; ctx.globalAlpha = 0.25; ctx.fill(path, "evenodd"); ctx.globalAlpha = 1; }
  }
  const toScreen = (vx, vy) => [vx * scale() + S.t.x + S.t.k * S.fit.x, vy * scale() + S.t.y + S.t.k * S.fit.y];

  function unitsText() {
    $("units").textContent = S.mailDay == null
      ? "USPS delivery days"
      : `arrives on… if mailed ${WEEKDAY_NAMES[S.mailDay]}`;
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

  function perfLine(p) {
    const w1 = p.w[p.k] && p.w[p.k][0];
    return `<b>${pct(p.on[p.k])}</b> on time${w1 != null ? ` · ${pct(w1)} within 1 extra day` : ""}`;
  }

  function perfPanel() {
    const box = $("perf");
    const P = S.perf;
    if (!P) { box.hidden = true; return; }
    box.hidden = false;
    const c = P.classes[S.cls];
    const cls = S.meta.classes.find(x => x.key === S.cls).label;
    if (!c) {
      box.innerHTML = `<h3>How often it's on time</h3>
        <p class="muted">USPS doesn't publish on-time results by region for ${cls}.</p>`;
      return;
    }
    if (S.origin == null) {
      box.innerHTML = `<h3>How often it's on time</h3>
        <p class="muted">Hover a ZIP to see USPS's on-time record for its ${c.level}.</p>`;
      return;
    }
    const z3 = S.zip3[S.origin].z;
    const keys = c.by_std ? ["2", "3"] : ["all"];
    const rows = keys.map((k) => {
      const p = perfFor(z3, k === "2" ? 2 : 3);
      if (!p) return "";
      const w = p.w[p.k] || [];
      return `<tr><th>${c.by_std ? STD_NAME[k] + " mail" : "All"}</th><td>${pct(p.on[p.k])}</td>
        <td>${pct(w[0])}</td><td>${pct(w[2])}</td></tr>`;
    }).join("");
    const p0 = perfFor(z3, 3);
    box.innerHTML = p0 ? `<h3>How often it's on time</h3>
      <p class="muted">${c.label}, <span class="nw">${p0.name}</span>, ${P.quarter}</p>
      <table class="perf-t"><thead><tr><th></th><th>On time</th><th>+1 day</th><th>+3 days</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="muted small">Nationally: ${keys.map(k => `${c.by_std ? STD_NAME[k] + " " : ""}${pct(c.nation[k])}`).join(", ")} on time.
      <a href="${c.source}">USPS report</a></p>`
      : `<h3>How often it's on time</h3><p class="muted">No USPS results for this ${c.level}.</p>`;
  }

  // ---------- side panel ----------
  function legend() {
    S.rowCache = null;
    const ul = $("legend");
    const counts = new Array(BUCKETS.length).fill(0);
    const row = S.rowCache || (S.rowCache = currentRow());
    if (row) for (let i = 0; i < S.N; i++) counts[bkt(row[i])] += S.zip3Count[i];
    const hoverB = row && S.hover ? bkt(row[S.hover.i]) : -1;
    ul.innerHTML = "";
    BUCKETS.forEach((b, j) => {
      // USPS doesn't count Sundays, so nothing is scheduled to arrive on one.
      if (S.mailDay != null && j < 6 && (S.mailDay + j + 1) % 7 === 6) return;
      const li = document.createElement("li");
      if (row && !counts[j]) li.className = "zero";
      if (j === hoverB) li.classList.add("hit");
      li.innerHTML = `<span class="sw" style="background:${S.colors.bucket[j]}"></span>
        <span>${bucketLabel(j)}</span><span class="n">${row ? counts[j].toLocaleString() : ""}</span>`;
      ul.appendChild(li);
    });

    const o = S.origin == null ? null : S.zip3[S.origin];
    const oz = S.originZip;
    $("originZip").textContent = oz ? oz.z : "—";
    $("originMeta").textContent = o
      ? `${o.s || "—"} · prefix ${o.z}xx${S.pinned ? " · pinned" : ""}`
      : "Hover over the map, or click a ZIP to pin it.";
    $("unpin").hidden = !S.pinned;
    perfPanel();
  }

  function showTip(sx, sy) {
    if (sx != null) S.tipXY = [sx, sy];
    const f = S.hover;
    if (!f || !S.tipXY) { tip.hidden = true; return; }
    const z3 = S.zip3[f.i];
    let html;
    if (S.pinned && S.originZip) {
      const d = S.days[S.cls][S.origin * S.N + f.i];
      const label = S.meta.classes.find(c => c.key === S.cls).label;
      let line, note = label;
      if (S.mailDay != null && d) {
        const a = arrival(S.mailDay, d);
        const later = a.cal >= 7 ? ` (${a.cal >= 14 ? "in " + Math.floor(a.cal / 7) + " weeks" : "next week"})` : "";
        line = `<span class="sw" style="background:${S.colors.bucket[bkt(a.cal)]}"></span>
          Mailed ${WEEKDAY_NAMES[S.mailDay]} → arrives ${WEEKDAY_NAMES[a.wd]}${later}`;
        note = `${label} · ${daysText(d)} by USPS count, ${a.cal} days after mailing`;
      } else {
        line = `<span class="sw" style="background:${S.colors.bucket[bucketOf(d)]}"></span>${daysText(d)}`;
      }
      // On-time record of the sending and receiving districts, if USPS publishes one.
      let perfHtml = "";
      if (S.perf && d) {
        const from = perfFor(S.zip3[S.origin].z, d), to = perfFor(z3.z, d);
        if (from) perfHtml += `<div class="perf-tip">From ${from.name}: ${perfLine(from)}</div>`;
        if (to && (!from || to.name !== from.name)) perfHtml += `<div class="perf-tip">To ${to.name}: ${perfLine(to)}</div>`;
        if (!S.perf.classes[S.cls]) perfHtml = `<div class="perf-tip muted">No public on-time data for this service</div>`;
      }
      html = `<b>${S.originZip.z}</b> → <b>${f.z}</b> <span class="muted">${z3.s}</span>
        <div class="days">${line}</div>
        ${perfHtml}
        <span class="muted">${note}</span>`;
    } else {
      html = `From <b>${f.z}</b> <span class="muted">${z3.s} · prefix ${z3.z}xx</span>
        <div class="muted">Click to pin this origin</div>`;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const [x, y] = S.tipXY;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    const clamp = (v, max) => Math.max(4, Math.min(v, max - 4));
    tip.style.left = `${clamp(x + 14 + tw > S.w ? x - tw - 14 : x + 14, S.w - tw)}px`;
    tip.style.top = `${clamp(y + 14 + th > S.h ? y - th - 14 : y + 14, S.h - th)}px`;
  }

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

  function pinZip(f, zoomTo) {
    S.pinned = true;
    setOrigin(f);
    legend(); status(); writeHash(); draw();
    if (zoomTo) zoomToFeature(f);
  }

  function unpin() {
    S.pinned = false;
    setOrigin(S.hover);
    legend(); status(); writeHash(); draw(); showTip();
  }

  function onMove(ev) {
    const r = canvas.getBoundingClientRect();
    const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
    const f = featureAt(...toVirtual(sx, sy));
    if (f !== S.hover) {
      S.hover = f;
      if (!S.pinned && f) setOrigin(f);
      legend(); draw();
    }
    showTip(sx, sy);
  }

  canvas.addEventListener("pointermove", (ev) => { if (ev.pointerType === "mouse" || ev.buttons === 0) onMove(ev); });
  canvas.addEventListener("pointerleave", () => {
    S.hover = null; tip.hidden = true;
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

  // ZIP search
  $("search").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("zip").value.trim();
    let f = S.byZip.get(v.padStart(5, "0"));
    if (!f && /^\d{3}$/.test(v)) f = S.feats.find(x => x.z.startsWith(v));
    if (!f && /^\d{5}$/.test(v)) f = S.feats.find(x => x.z.startsWith(v.slice(0, 3)));
    if (!f) { $("status").textContent = `No ZIP shape found for “${v}”. PO-box-only ZIPs have no shape; try a nearby ZIP.`; return; }
    if (f.z !== v.padStart(5, "0") && v.length === 5) $("status").textContent = `${v} has no shape; using ${f.z} in the same prefix.`;
    S.hover = f;
    pinZip(f, true);
  });

  // URL hash: #fcm/80202
  function readHash() {
    // #fcm/80202/fri  (ZIP and mailing day are optional; #fcm//fri works too)
    const [cls, zip, day] = location.hash.replace(/^#/, "").split("/");
    const wd = WEEKDAYS.findIndex(w => w.toLowerCase() === (day || "").toLowerCase());
    return { cls, zip, day: wd >= 0 ? wd : null };
  }
  function writeHash() {
    const zip = S.pinned && S.originZip ? S.originZip.z : "";
    const day = S.mailDay == null ? "" : WEEKDAYS[S.mailDay].toLowerCase();
    const h = `#${S.cls}${zip || day ? "/" + zip : ""}${day ? "/" + day : ""}`;
    if (location.hash !== h) history.replaceState(null, "", h);
  }

  window.addEventListener("resize", () => { resize(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readColors(); legend(); draw(); });

  load().catch((err) => {
    console.error(err);
    $("loading").textContent = "Could not load the map data. Run build/build_data.py first (see README).";
  });
})();
