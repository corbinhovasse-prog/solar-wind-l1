"use strict";
/* Dashboard for the solar-wind-l1 pipeline. Everything is drawn from data/latest.json,
   which the pipeline rebuilds every 30 minutes. */

const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const SRC = [["SOLAR1", "SOLAR-1", "--series-1"], ["IMAP", "IMAP", "--series-2"], ["ACE", "ACE", "--series-3"]];
const NAME = { SOLAR1: "SOLAR-1", IMAP: "IMAP", ACE: "ACE" };
const colorOf = k => css((SRC.find(s => s[0] === k) || [0, 0, "--text-secondary"])[2]);
const fmt = (v, d = 0) => v == null || !isFinite(v) ? "—"
  : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const when = iso => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const charts = {};
function draw(id, cfg) { if (charts[id]) charts[id].destroy(); charts[id] = new Chart($(id), cfg); return charts[id]; }

function seg(el, items, current, onPick) {
  el.innerHTML = items.map(([k, label, off]) =>
    `<button data-k="${k}" aria-pressed="${k === current}" ${off ? "disabled title='not available yet'" : ""}>${label}</button>`).join("");
  el.onclick = e => {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    [...el.children].forEach(x => x.setAttribute("aria-pressed", x === b));
    onPick(b.dataset.k);
  };
}

function duration(sec) {
  if (sec < 90) return `${fmt(sec, sec < 10 ? 1 : 0)} s`;
  const min = sec / 60;
  if (min < 90) return `${fmt(min, 0)} min`;
  const h = min / 60;
  if (h < 48) return `${fmt(h, 1)} h`;
  const d = h / 24;
  if (d < 730) return `${fmt(d, 0)} days`;
  return `${fmt(d / 365.25, 1)} years`;
}
const km = v => v >= 1e6 ? `${fmt(v / 1e6, v >= 1e8 ? 0 : 2)}M km` : `${fmt(v / 1e3, 0)}k km`;

/* ---------- chart scaffolding ---------- */
const tooltip = () => ({ backgroundColor: css("--surface-1"), titleColor: css("--text-primary"), bodyColor: css("--text-secondary"),
  borderColor: css("--border"), borderWidth: 1, padding: 10 });
const axisTime = () => ({ type: "time", grid: { color: css("--grid") }, ticks: { color: css("--text-muted"), maxTicksLimit: 8 }, border: { display: false } });
const axisLin = (title, extra = {}) => ({ type: "linear", title: { display: !!title, text: title, color: css("--text-muted") },
  grid: { color: css("--grid") }, ticks: { color: css("--text-muted") }, border: { display: false }, ...extra });
const baseOpts = yTitle => ({ responsive: true, maintainAspectRatio: false, animation: false,
  interaction: { mode: "index", intersect: false }, plugins: { legend: { display: false }, tooltip: tooltip() },
  scales: { x: axisTime(), y: axisLin(yTitle) } });

/* Reference lines (dashed, labelled), event bands, and a tint below zero for southward Bz. */
Chart.register({
  id: "refs",
  beforeDatasetsDraw(chart, _, o) {
    const { ctx, chartArea: a, scales: { x, y } } = chart;
    if (!x || !y) return;
    ctx.save();
    if (o.southFill) {
      const y0 = y.getPixelForValue(0);
      if (y0 < a.bottom) { ctx.fillStyle = css("--south-fill"); ctx.fillRect(a.left, Math.max(y0, a.top), a.right - a.left, a.bottom - Math.max(y0, a.top)); }
    }
    for (const b of o.bands || []) {
      const x0 = x.getPixelForValue(Date.parse(b.start)), x1 = x.getPixelForValue(Date.parse(b.end));
      if (x1 < a.left || x0 > a.right) continue;
      const L = Math.max(x0, a.left), R = Math.min(Math.max(x1, x0 + 4), a.right);
      ctx.fillStyle = b.on ? css("--band-strong") : b.minor ? css("--grid") : css("--band");
      ctx.fillRect(L, a.top, R - L, a.bottom - a.top);
      if (b.label) {
        ctx.fillStyle = css("--text-primary"); ctx.font = "700 11px system-ui"; ctx.textAlign = "center";
        ctx.fillText(b.label, (L + R) / 2, a.top + 12);
      }
    }
    ctx.restore();
  },
  afterDatasetsDraw(chart, _, o) {
    const { ctx, chartArea: a, scales: { y } } = chart;
    if (!y) return;
    ctx.save();
    for (const r of o.lines || []) {
      const py = y.getPixelForValue(r.y);
      if (py < a.top || py > a.bottom) continue;
      ctx.strokeStyle = css("--text-muted"); ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = css("--text-muted"); ctx.font = "11.5px system-ui"; ctx.textAlign = "right";
      ctx.fillText(r.label, a.right - 4, py - 4 < a.top + 10 ? py + 13 : py - 4);
    }
    ctx.restore();
  }
});

/* ---------- metrics and ranges ---------- */
const ABOUT = {
  speed: ["How fast the solar wind is moving: a stream of charged particles (mostly protons) blowing off the Sun in every direction.",
          "Slow wind: 300–400 km/s. Fast streams from coronal holes: 600–800. Shocks from solar eruptions can exceed 1,000.",
          "Faster wind carries more energy and gives less warning: at 800 km/s the wind crosses from L1 to Earth in about 30 minutes."],
  density:["How many protons are packed into each cubic centimetre of the wind. Space is close to empty: a sugar cube's worth of air on Earth holds about 10 billion billion molecules.",
          "Usually 3–10 per cm³. Jumps to 20–50 often mark the dense pile-up in front of a solar eruption.",
          "Denser wind pushes harder on Earth's magnetic shield, and a sudden jump is one of the clearest signs a shock has arrived."],
  bz:     ["The north–south part of the magnetic field the solar wind carries with it, in nanotesla (nT). Earth's own field at the surface is about 50,000 nT; the wind's is about 5.",
          "Swings between about −5 and +5 nT. Below −10 nT for hours is storm territory.",
          "The single most important number for space weather. South (negative) links up with Earth's field and lets energy in; north mostly bounces off."],
  pdyn:   ["Ram pressure: the push of the wind on Earth's magnetic shield, from its density and speed (pressure grows with speed squared).",
          "Usually 1–3 nanopascals (nPa). Above 5–10 nPa squeezes the shield noticeably closer to Earth.",
          "A pressure jump compresses the shield suddenly, which ground magnetometers see as a sharp step called a sudden commencement."],
  newell: ["Coupling: one number combining speed, field strength and field direction to estimate how much solar wind energy can get into Earth's magnetic field (Newell et al., 2007).",
          "Zero when the field points north. Single digits on quiet days; 20 and above during strong driving.",
          "The best single predictor of geomagnetic activity. Below, it is compared with Kp to see how much of Earth's response it explains."],
  kp:     ["The planetary K-index: how disturbed Earth's magnetic field is, measured by a network of ground magnetometers, on a 0–9 scale every 3 hours.",
          "0–2 quiet, 3–4 unsettled to active, 5 and above a geomagnetic storm (G1 to G5 on NOAA's scale).",
          "This is the effect, measured at Earth. Everything else on this page is the cause, measured a million miles upstream."],
};

const METRICS = {
  speed:  { label: "Speed", unit: "km/s", live: "speed", perSource: true, omni: "v", d: 0,
            refs: [{ y: 400, label: "typical ~400" }, { y: 600, label: "fast stream" }], share: [v => v > 500, "of the time faster than 500 km/s"] },
  density:{ label: "Density", unit: "protons /cm³", live: "density", perSource: true, omni: "n", d: 1,
            refs: [{ y: 5, label: "typical ~5" }], share: [v => v > 10, "of the time above 10 /cm³"] },
  bz:     { label: "Bz", unit: "nT", live: "bz", perSource: true, omni: "bz", d: 1, southFill: true,
            refs: [{ y: 0, label: "north above · south below" }, { y: -10, label: "strongly south" }], share: [v => v < 0, "of the time southward"] },
  pdyn:   { label: "Ram pressure", unit: "nPa", live: "pdyn", omni: "pdyn", d: 2,
            refs: [{ y: 2, label: "typical ~2" }], share: [v => v > 4, "of the time above 4 nPa"] },
  newell: { label: "Coupling", unit: "relative units", live: "newell", omni: "newell", d: 1,
            refs: [], share: null },
  kp:     { label: "Kp", unit: "0–9 scale", kp: true, omni: "kp", d: 1,
            refs: [{ y: 5, label: "storm threshold (G1)" }], share: [v => v >= 5, "of windows at storm level"] },
};
const RANGES = [["24h", "24 h", 24], ["3d", "3 days", 72], ["7d", "7 days", 168], ["30d", "30 days", 720],
                ["1y", "1 yr", null, 1], ["2y", "2 yrs", null, 2], ["3y", "3 yrs", null, 3], ["5y", "5 yrs", null, 5],
                ["10y", "10 yrs", null, 10], ["all", "Since 2000", null, 99]];

function quantile(arr, q) {
  const a = arr.filter(v => v != null && isFinite(v)).sort((x, y) => x - y);
  if (!a.length) return null;
  const i = (a.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
  return a[lo] + (a[hi] - a[lo]) * (i - lo);
}

/* ---------- section: now ---------- */
function renderNow(D) {
  const L = D.latest, H = D.history;
  const ago = Math.round((Date.now() - Date.parse(D.generated_at)) / 60000);
  $("meta").textContent = `Pipeline last ran ${ago < 90 ? ago + " min" : Math.round(ago / 60) + " h"} ago · ` +
    `latest reading ${when(L.time)} · plasma from ${NAME[L.plasma_source] || "—"}, magnetic field from ${NAME[L.mag_source] || "—"} · ` +
    `archive since ${new Date(H.start).toLocaleDateString()} (${H.rows.toLocaleString()} spacecraft-minutes)`;

  let level = "quiet", word = "Quiet";
  if (L.kp >= 5) { level = "storm"; word = "Storm conditions"; }
  else if ((L.bz <= -10 && L.speed >= 500) || L.kp >= 4) { level = "active"; word = "Active"; }
  else if (L.bz < 0 && L.bz_south_pct_24h > 60) { level = "active"; word = "Unsettled"; }
  $("status-chip").className = `chip ${level}`;
  $("status-chip").textContent = word;
  const faster = L.speed != null && L.speed_median_7d != null
    ? (L.speed > L.speed_median_7d * 1.1 ? "faster than" : L.speed < L.speed_median_7d * 0.9 ? "slower than" : "close to") : null;
  const parts = [];
  if (L.speed != null) parts.push(`Wind at ${fmt(L.speed)} km/s${faster ? `, ${faster} this week's typical ${fmt(L.speed_median_7d)}` : ""}.`);
  if (L.bz_south_pct_24h != null) parts.push(`Bz pointed south ${fmt(L.bz_south_pct_24h)}% of the last 24 h (longest stretch ${duration(L.longest_south_min_24h * 60)}).`);
  if (L.lead_time_min != null) parts.push(`What's measured now reaches Earth in about ${fmt(L.lead_time_min)} min.`);
  $("status-text").textContent = parts.join(" ");

  const tiles = [
    ["Wind speed", fmt(L.speed), "km/s", L.plasma_source ? `${NAME[L.plasma_source]} · usual 300–500` : "no reading in the last hour"],
    ["Density", fmt(L.density, 1), "/cm³", "protons per cm³ · usual 3–10"],
    ["Bz", fmt(L.bz, 1), "nT", L.bz == null ? "" : L.bz < 0 ? "south: energy can enter" : "north: shield mostly closed"],
    ["Ram pressure", fmt(L.pdyn, 2), "nPa", "push on the shield · usual 1–3"],
    ["Warning time", fmt(L.lead_time_min), "min", "until this wind reaches Earth"],
    ["Planetary Kp", fmt(L.kp, 2), "", "0 calm · 5 or more = storm"],
  ];
  $("tiles").innerHTML = tiles.map(([l, v, u, c]) =>
    `<div class="tile"><div class="label">${l}</div><div class="value">${v}<span class="unit">${u}</span></div><div class="ctx">${c}</div></div>`).join("");
}

/* ---------- section: explore ---------- */
const explore = { metric: "speed", range: "24h" };

function renderExplore(D) {
  const longOK = D.longterm && D.longterm.available;
  seg($("metric-seg"), Object.entries(METRICS).map(([k, m]) => [k, m.label]), explore.metric, k => { explore.metric = k; drawExplore(D); });
  [...$("metric-seg").children].forEach(b => b.title = ABOUT[b.dataset.k][0]);
  seg($("range-seg"), RANGES.map(([k, l, h]) => [k, l, h == null && !longOK]), explore.range,
      k => { explore.range = k; drawExplore(D); });
  drawExplore(D);
}

function drawExplore(D) {
  const M = METRICS[explore.metric], R = RANGES.find(r => r[0] === explore.range);
  const A = ABOUT[explore.metric];
  $("metric-about").innerHTML = `<div><b>What it is</b>${A[0]}</div><div><b>Typical values</b>${A[1]}</div><div><b>Why it matters</b>${A[2]}</div>`;
  const live = R[2] != null;
  const end = Date.parse(D.latest.time);
  let datasets = [], legend = [], values = [], note = "", type = "line";

  if (live) {
    const S = D.series[R[2] <= 72 ? "fine" : "hourly"];
    const lo = end - R[2] * 3600e3;
    const cut = (t, v) => t.map((x, i) => ({ x: Date.parse(x), y: v[i] })).filter(p => p.x >= lo);
    if (M.kp) {
      const pts = cut(D.series.kp.t, D.series.kp.kp);
      type = "bar";
      datasets = [{ label: "Kp", data: pts, backgroundColor: pts.map(p => p.y >= 5 ? css("--critical") : p.y >= 4 ? css("--warning") : css("--series-1")),
                    barPercentage: 1, categoryPercentage: 0.9 }];
      values = pts.map(p => p.y);
    } else if (M.perSource) {
      for (const [k, name, c] of SRC) {
        const ser = S.per_source[k];
        if (!ser) continue;
        datasets.push({ label: name, data: cut(ser.t, ser[M.live]), borderColor: css(c), backgroundColor: css(c),
                        borderWidth: 1.75, pointRadius: 0, pointHoverRadius: 4, spanGaps: false });
        legend.push(`<span style="--c:${css(c)}">${name}</span>`);
      }
      values = cut(S.active.t, S.active[M.live]).map(p => p.y);
    } else {
      const pts = cut(S.active.t, S.active[M.live]);
      datasets = [{ label: M.label, data: pts, borderColor: css("--series-1"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 }];
      values = pts.map(p => p.y);
      legend.push(`<span style="--c:${css("--series-1")}">best available spacecraft each minute</span>`);
    }
    const first = Date.parse(D.history.start);
    note = first > lo + 3600e3
      ? `This pipeline's own archive starts ${when(D.history.start)}, so the window fills in as it keeps running. Pick 1 year or longer for NASA's long-term record.`
      : `${R[2] <= 72 ? "5-minute" : "Hourly"} averages from this pipeline's archive.`;
  } else {
    const LT = D.longterm, yrs = R[3], daily = yrs <= 5, block = daily ? LT.daily : LT.monthly;
    const lo = yrs >= 99 ? 0 : Date.parse(LT.end) - yrs * 365.25 * 864e5;
    const idx = block.t.map((t, i) => [Date.parse(t), i]).filter(([t]) => t >= lo);
    const S = block[M.omni];
    const weekly = daily && yrs >= 3;   // 3-5 years of daily points is too dense to read: average by week
    const p = arr => {
      const raw = idx.map(([t, i]) => ({ x: t, y: arr[i] }));
      if (!weekly) return raw;
      const out = [];
      for (let j = 0; j < raw.length; j += 7) {
        const w = raw.slice(j, j + 7).filter(q => q.y != null);
        if (w.length) out.push({ x: w[0].x, y: w.reduce((a, q) => a + q.y, 0) / w.length });
      }
      return out;
    };
    const c = css("--series-1");
    datasets = [
      { label: "25th percentile", data: p(S.p25), borderWidth: 0, pointRadius: 0, fill: false },
      { label: "75th percentile", data: p(S.p75), borderWidth: 0, pointRadius: 0, fill: "-1", backgroundColor: c + "30" },
      { label: "Average", data: p(S.mean), borderColor: c, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4 },
    ];
    values = p(S.mean).map(q => q.y);
    const res = weekly ? "weekly" : daily ? "daily" : "monthly";
    legend = [`<span style="--c:${c}">${res} average</span>`,
              `<span class="band" style="--c:${c}">middle half of hours</span>`];
    note = `NASA OMNI hourly data (near-Earth solar wind, already time-shifted to Earth), ${res} averages through ${new Date(LT.end).toLocaleDateString()}. OMNI is published about a month behind real time.`;
  }

  $("explore-legend").innerHTML = legend.join("");
  const o = baseOpts(`${M.label} (${M.unit})`);
  o.plugins.refs = { lines: M.refs, southFill: !!M.southFill };
  o.plugins.tooltip.callbacks = {
    title: items => items.length ? new Date(items[0].parsed.x).toLocaleString(undefined, live
      ? { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" } : { year: "numeric", month: "short", day: (R[3] || 0) <= 5 ? "numeric" : undefined }) : "",
    label: c => ` ${c.dataset.label}: ${fmt(c.parsed.y, M.d)}`,
  };
  if (!live) o.plugins.tooltip.filter = item => item.dataset.label === "Average" || item.datasetIndex !== 0;
  if (M.kp) { o.scales.y.min = 0; o.scales.y.suggestedMax = 6; }
  draw("explore-chart", { type, data: { datasets }, options: o });

  const v = values.filter(x => x != null && isFinite(x));
  const st = [];
  if (v.length) {
    const mean = v.reduce((a, b) => a + b, 0) / v.length;
    st.push(["Average", `${fmt(mean, M.d)} ${["relative units", "0–9 scale"].includes(M.unit) ? "" : M.unit}`]);
    st.push(["Median", fmt(quantile(v, .5), M.d)]);
    st.push(["Middle half", `${fmt(quantile(v, .25), M.d)} to ${fmt(quantile(v, .75), M.d)}`]);
    // the share-of-time stat needs individual readings; averages over days or months would hide short storms
    if (M.share && live) {
      const lbl = M.share[1].replace(/^of /, "").replace(/^the time /, "time ");
      st.push([lbl[0].toUpperCase() + lbl.slice(1), `${fmt(100 * v.filter(M.share[0]).length / v.length)}%`]);
    }
    st.push([live ? "Highest" : "Highest average", fmt(Math.max(...v), M.d)]);
  }
  $("explore-stats").innerHTML = st.map(([k, val]) => `<div class="stat"><div class="k">${k}</div><div class="v">${val}</div></div>`).join("")
    || `<div class="empty">No data in this window yet.</div>`;
  $("explore-note").textContent = note;
}

/* ---------- section: seasons and cycle ---------- */
function renderLongview(D) {
  const LT = D.longterm;
  if (!LT || !LT.available) { $("longview").style.display = "none"; return; }
  const bm = LT.by_month.kp_mean;
  const hi = bm.indexOf(Math.max(...bm)), lo = bm.indexOf(Math.min(...bm));
  const o = baseOpts("average Kp");
  o.scales.x = { type: "category", grid: { display: false }, ticks: { color: css("--text-muted") }, border: { display: false } };
  o.plugins.tooltip.callbacks = { label: c => ` average Kp ${fmt(c.parsed.y, 2)}` };
  const eq = [2, 3, 8, 9];
  draw("bymonth-chart", { type: "bar", data: { labels: MON, datasets: [{ data: bm, borderRadius: 4,
    backgroundColor: MON.map((_, i) => eq.includes(i) ? css("--series-1") : css("--seq-2")) }] }, options: o });
  $("bymonth-note").textContent = `Most disturbed month: ${MON[hi]} (${fmt(bm[hi], 2)}); calmest: ${MON[lo]} (${fmt(bm[lo], 2)}). ` +
    `Darker bars are the equinox months. Around the equinoxes Earth's magnetic axis is angled so that the solar wind's field ` +
    `links up with it more easily (the Russell–McPherron effect), so the same solar wind stirs up more activity.`;

  const Y = LT.by_year;
  const o2 = baseOpts("3-hour windows with Kp ≥ 5");
  o2.scales.x = { type: "category", grid: { display: false }, ticks: { color: css("--text-muted"), maxTicksLimit: 14 }, border: { display: false } };
  o2.plugins.tooltip.callbacks = { label: c => ` ${fmt(c.parsed.y)} storm windows · average sunspot number ${fmt(Y.sunspot[c.dataIndex])}` };
  draw("byyear-chart", { type: "bar", data: { labels: Y.years, datasets: [{ data: Y.storms, backgroundColor: css("--series-1"), borderRadius: 3 }] }, options: o2 });
  const top = Y.storms.indexOf(Math.max(...Y.storms));
  $("byyear-note").textContent = Y.storms[top] > 0
    ? `Storm counts rise and fall with the roughly 11-year sunspot cycle. Stormiest year in this record: ${Y.years[top]} ` +
      `(${fmt(Y.storms[top])} storm windows, average sunspot number ${fmt(Y.sunspot[top])}). Tap or hover a bar for its sunspot number.`
    : `No storm-level windows in this record yet.`;
}

/* ---------- section: where ---------- */
const where = { pick: null };

function svgEl(tag, attrs, text) {
  const a = Object.entries(attrs).map(([k, v]) => `${k}="${v}"`).join(" ");
  return text == null ? `<${tag} ${a}/>` : `<${tag} ${a}>${text}</${tag}>`;
}

function renderWhere(D) {
  const P = D.positions;
  if (!P || !P.available) { $("where").style.display = "none"; return; }
  const have = SRC.filter(([k]) => P.tracks[k]);
  $("pos-legend").innerHTML = have.map(([, name, c]) => `<span style="--c:${css(c)}">${name}</span>`).join("");
  const ink = css("--text-primary"), quiet = css("--text-muted"), axis = css("--axis");
  const halo = { stroke: css("--surface-1"), "stroke-width": 4, "paint-order": "stroke", "stroke-linejoin": "round" };
  const placed = [];  // simple label collision avoidance: flip a label below its dot if it would overlap another
  const labelY = (x, y) => { const up = y - 12, clash = placed.some(([px, py]) => Math.abs(px - x) < 70 && Math.abs(py - up) < 16);
    const ly = clash ? y + 22 : up; placed.push([x, ly]); return ly; };
  const labelYs = (x, y) => { const up = y - 12 * fs, clash = placed.some(([px, py]) => Math.abs(px - x) < 70 * fs && Math.abs(py - up) < 16 * fs);
    const ly = clash ? y + 22 * fs : up; placed.push([x, ly]); return ly; };
  const moonR = 384.4, sunKm = P.bodies.SUN ? P.bodies.SUN.x_gse : 1.48e8, L1x = sunKm * 0.01 / 1e3;

  /* top view: X (toward Sun) to the right, Y up, everything in thousand km, equal scale */
  const allX = have.flatMap(([k]) => P.tracks[k].x), allY = have.flatMap(([k]) => P.tracks[k].y);
  const xmin = -moonR - 90, xmax = Math.max(...allX, L1x) + 190;
  const ymax = Math.max(moonR, ...allY.map(Math.abs)) + 90;
  const W = 1000, k = W / (xmax - xmin), Hh = 2 * ymax * k;
  // the SVG shrinks to fit the screen, so on a phone (about 350 px wide) everything is drawn bigger to stay readable
  const fs = Math.min(Math.max(640 / Math.max($("map-top").clientWidth, 1), 1), 2.6), F = n => +(n * fs).toFixed(1);
  const X = x => (x - xmin) * k, Y = y => Hh / 2 - y * k;
  let s = `<svg viewBox="0 0 ${W} ${Hh + F(30)}" role="img" aria-label="Top-down map of Earth, the Moon's orbit, L1 and the three spacecraft orbits">`;
  s += `<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="${quiet}"/></marker></defs>`;
  s += svgEl("line", { x1: X(0), y1: Y(0), x2: W - 8, y2: Y(0), stroke: axis, "stroke-dasharray": "5 5", "marker-end": "url(#arr)" });
  s += svgEl("text", { x: W - 12, y: F(18), "text-anchor": "end", fill: quiet, "font-size": F(13) },
    fs > 1.5 ? `Sun → ${fmt(sunKm / 1e6, 0)} million km` : `Sun is off the map to the right → ${fmt(sunKm / 1e6, 1)} million km away, 100× further than L1`);
  s += svgEl("circle", { cx: X(0), cy: Y(0), r: moonR * k, fill: "none", stroke: axis, "stroke-dasharray": "3 5" });
  s += svgEl("text", { x: X(0), y: Y(moonR) - F(8), "text-anchor": "middle", fill: quiet, "font-size": F(12.5) }, "Moon's orbit · 384,000 km");
  if (P.bodies.MOON) {
    const m = P.bodies.MOON;
    s += svgEl("circle", { cx: X(m.x_gse / 1e3), cy: Y(m.y_gse / 1e3), r: F(5), fill: quiet });
    s += svgEl("text", { x: X(m.x_gse / 1e3) + F(9), y: Y(m.y_gse / 1e3) + 4, fill: quiet, "font-size": F(12.5) }, "Moon");
  }
  s += svgEl("circle", { cx: X(0), cy: Y(0), r: F(8), fill: css("--series-1") });
  s += svgEl("text", { x: X(0), y: Y(0) + F(26), "text-anchor": "middle", fill: ink, "font-size": F(13.5), "font-weight": 650 }, "Earth");
  s += `<path d="M${X(L1x) - 6} ${Y(0) - 6}L${X(L1x) + 6} ${Y(0) + 6}M${X(L1x) - 6} ${Y(0) + 6}L${X(L1x) + 6} ${Y(0) - 6}" stroke="${ink}" stroke-width="2"/>`;
  s += svgEl("text", { x: X(L1x), y: Y(0) + F(22), "text-anchor": "middle", fill: ink, "font-size": F(13), "font-weight": 650, ...halo }, `L1 · ${fmt(L1x / 1e3, 2)} million km`);
  for (const [kk, name, c] of have) {
    const T = P.tracks[kk], n = T.x.length - 1;
    s += svgEl("polyline", { points: T.x.map((x, i) => `${X(x).toFixed(1)},${Y(T.y[i]).toFixed(1)}`).join(" "), fill: "none", stroke: css(c), "stroke-width": F(1.75), "stroke-opacity": .75 });
    s += svgEl("circle", { cx: X(T.x[n]), cy: Y(T.y[n]), r: F(6.5), fill: css(c), stroke: css("--surface-1"), "stroke-width": 2 });
    s += svgEl("text", { x: X(T.x[n]), y: labelYs(X(T.x[n]), Y(T.y[n])), "text-anchor": "middle", fill: ink, "font-size": F(12.5), "font-weight": 650, ...halo }, name);
  }
  const bar = 500, bx = 16, by = Hh + F(18);
  s += svgEl("line", { x1: bx, y1: by, x2: bx + bar * k, y2: by, stroke: ink, "stroke-width": F(2) });
  s += svgEl("text", { x: bx + bar * k + 8, y: by + 4, fill: quiet, "font-size": F(12) }, "500,000 km");
  $("map-top").innerHTML = s + "</svg>";

  /* view from Earth: Y sideways, Z up, square, Moon's orbit for scale */
  const Rr = Math.max(moonR, ...have.flatMap(([kk]) => [...P.tracks[kk].y, ...P.tracks[kk].z].map(Math.abs))) + 60;
  const S2 = 440, k2 = S2 / (2 * Rr), cx = S2 / 2, cy = S2 / 2;
  const PX = y => cx - y * k2, PY = z => cy - z * k2;  // looking toward the Sun, +Y (GSE) appears on the left
  let t = `<svg viewBox="0 0 ${S2} ${S2}" role="img" aria-label="Halo orbits seen from Earth, with the Moon's orbit for scale">`;
  t += svgEl("circle", { cx, cy, r: moonR * k2, fill: "none", stroke: axis, "stroke-dasharray": "3 5" });
  t += svgEl("text", { x: cx, y: cy - moonR * k2 - 6, "text-anchor": "middle", fill: quiet, "font-size": 12 }, "Moon's orbit, same scale");
  t += `<path d="M${cx - 7} ${cy}H${cx + 7}M${cx} ${cy - 7}V${cy + 7}" stroke="${ink}" stroke-width="1.5"/>`;
  t += svgEl("text", { x: cx, y: cy + 20, "text-anchor": "middle", fill: quiet, "font-size": 12 }, "Sun–Earth line");
  for (const [kk, name, c] of have) {
    const T = P.tracks[kk], n = T.y.length - 1;
    t += svgEl("polyline", { points: T.y.map((y, i) => `${PX(y).toFixed(1)},${PY(T.z[i]).toFixed(1)}`).join(" "), fill: "none", stroke: css(c), "stroke-width": 1.75, "stroke-opacity": .75 });
    t += svgEl("circle", { cx: PX(T.y[n]), cy: PY(T.z[n]), r: 6.5, fill: css(c), stroke: css("--surface-1"), "stroke-width": 2 });
    t += svgEl("text", { x: PX(T.y[n]), y: labelY(PX(T.y[n]) + 5000, PY(T.z[n])), "text-anchor": "middle", fill: ink, "font-size": 12.5, "font-weight": 650, ...halo }, name);
  }
  t += svgEl("text", { x: 8, y: 16, fill: quiet, "font-size": 12 }, "north ↑");
  $("map-yz").innerHTML = t + "</svg>";

  where.pick = where.pick || (P.latest[D.latest.plasma_source] ? D.latest.plasma_source : have[0][0]);
  seg($("dist-seg"), have.map(([kk, name]) => [kk, name]), where.pick, kk => { where.pick = kk; distTable(D); });
  distTable(D);
}

function distTable(D) {
  const P = D.positions, q = P.latest[where.pick], v = D.latest.speed || P.speed_used || 400, c = P.light_kms || 299792.458;
  const rows = [["Earth", q.dist_km.EARTH], ["the Moon", q.dist_km.MOON], ["the Sun", q.dist_km.SUN]];
  if (q.nearest_planet) rows.push([`${q.nearest_planet[0] + q.nearest_planet.slice(1).toLowerCase()} (nearest planet)`, q.dist_km[q.nearest_planet]]);
  for (const [k] of SRC) if (k !== where.pick && P.latest[k]) {
    const o = P.latest[k];
    rows.push([NAME[k], Math.hypot(o.x_km - q.x_km, o.y_km - q.y_km, o.z_km - q.z_km)]);
  }
  $("dist-table").innerHTML = `<tr><th>From ${NAME[where.pick]} to</th><th>Distance</th><th>Radio</th><th>Solar wind</th><th>Jet</th></tr>` +
    rows.filter(r => r[1] != null).map(([name, d]) =>
      `<tr><td>${name}</td><td>${km(d)}</td><td>${duration(d / c)}</td><td>${duration(d / v)}</td><td>${duration(d / 900 * 3600)}</td></tr>`).join("");
}

/* ---------- section: field and events ---------- */
const field = { range: "3d", zoom: null };

function renderField(D) {
  seg($("bz-range-seg"), [["24h", "24 h"], ["3d", "3 days"], ["7d", "7 days"]], field.range, k => { field.range = k; field.zoom = null; drawField(D); });
  $("bz-reset").onclick = () => { field.zoom = null; drawField(D); };
  const card = (e, i) => {
    const n = i + 1;
    let impact, hit = false;
    if (e.geoeffective && e.kp_after >= 5) { impact = `Bz went south (to ${fmt(e.min_bz, 1)} nT) and a storm followed: Kp reached ${fmt(e.kp_after, 1)}.`; hit = true; }
    else if (e.geoeffective) impact = `Bz went south (to ${fmt(e.min_bz, 1)} nT). Kp in the next 6 h: ${e.kp_after == null ? "not yet known" : fmt(e.kp_after, 1)}.`;
    else if (e.min_bz < 0) impact = `Bz was only weakly south (lowest ${fmt(e.min_bz, 1)} nT), so little effect at Earth${e.kp_after != null ? ` (Kp max ${fmt(e.kp_after, 1)} afterwards)` : ""}.`;
    else impact = `Bz stayed north, so little effect at Earth${e.kp_after != null ? ` (Kp max ${fmt(e.kp_after, 1)} afterwards)` : ""}.`;
    return `<button class="event ${e.significant === false ? "minor" : ""}" data-i="${i}">
      <div><span class="num">${n}</span><span class="type">${e.significant === false ? "Minor: " + e.type.toLowerCase() : e.type}</span></div>
      <div class="when">${when(e.start)} · ${e.minutes} min</div>
      <ul>${e.why.map(w => `<li>${w}</li>`).join("")}</ul>
      ${e.significance && e.significance.length ? `<div class="when">Significant because: ${e.significance.join(", ")}</div>` : ""}
      <div class="impact ${hit ? "hit" : ""}">${impact}</div></button>`;
  };
  const all = D.anomalies.map((e, i) => [e, i]).reverse();
  const big = all.filter(([e]) => e.significant !== false), minor = all.filter(([e]) => e.significant === false);
  $("events").innerHTML = (big.length ? big.map(([e, i]) => card(e, i)).join("")
      : `<div class="empty" style="grid-column:1/-1">No significant events in the last 7 days: nothing crossed a physical threshold (a 50 km/s speed jump, Bz below −10 nT, or pressure or field strength 2.5× normal).</div>`) +
    (minor.length ? `<details class="minor-list" style="grid-column:1/-1"><summary>${minor.length} minor fluctuation${minor.length > 1 ? "s" : ""}: statistically unusual for this week, but below every physical threshold</summary>
      <div class="events">${minor.map(([e, i]) => card(e, i)).join("")}</div></details>` : "");
  $("events").onclick = e => {
    const b = e.target.closest(".event");
    if (!b) return;
    e.preventDefault();
    const ev2 = D.anomalies[+b.dataset.i];
    field.zoom = { i: +b.dataset.i, lo: Date.parse(ev2.start) - 3 * 3600e3, hi: Date.parse(ev2.end) + 3 * 3600e3 };
    if (Date.now() - field.zoom.lo > 70 * 3600e3) field.range = "7d";
    $("events").querySelectorAll(".event").forEach(x => x.classList.toggle("on", x === b));
    drawField(D);
    $("bz-chart").scrollIntoView({ behavior: "smooth", block: "center" });
  };
  drawField(D);
}

function drawField(D) {
  const hours = { "24h": 24, "3d": 72, "7d": 168 }[field.range];
  const S = D.series[hours <= 72 ? "fine" : "hourly"].active;
  const end = Date.parse(D.latest.time), lo = end - hours * 3600e3;
  const pts = S.t.map((t, i) => ({ x: Date.parse(t), y: S.bz[i] })).filter(p => p.x >= lo);
  const o = baseOpts("Bz (nT)");
  o.plugins.refs = {
    southFill: true,
    lines: [{ y: 0, label: "north above · south below" }, { y: -5, label: "moderately south" }, { y: -10, label: "strongly south" }],
    bands: D.anomalies.map((e, i) => ({ start: e.start, end: e.end, label: e.significant === false ? "" : String(i + 1),
                                        minor: e.significant === false, on: field.zoom && field.zoom.i === i })),
  };
  o.plugins.tooltip.callbacks = { label: c => ` Bz ${fmt(c.parsed.y, 1)} nT (${c.parsed.y < 0 ? "south" : "north"})` };
  if (field.zoom) { o.scales.x.min = field.zoom.lo; o.scales.x.max = field.zoom.hi; }
  $("bz-reset").hidden = !field.zoom;
  draw("bz-chart", { type: "line", data: { datasets: [{ label: "Bz", data: pts, borderColor: css("--series-1"), borderWidth: 1.75, pointRadius: 0, pointHoverRadius: 4 }] }, options: o });

  const v = pts.map(p => p.y).filter(x => x != null);
  if (!v.length) { $("bz-insight").textContent = ""; return; }
  const step = hours <= 72 ? 5 : 60;
  let run = 0, best = 0;
  for (const x of v) { run = x < 0 ? run + 1 : 0; best = Math.max(best, run); }
  const southPct = 100 * v.filter(x => x < 0).length / v.length, strong = v.filter(x => x <= -10).length * step;
  $("bz-insight").textContent = `In this window Bz pointed south ${fmt(southPct)}% of the time; the longest southward stretch lasted ${duration(best * step * 60)}` +
    (strong ? `, and it spent ${duration(strong * 60)} below −10 nT, enough to drive a storm if sustained.` : `. It never reached −10 nT, the level that usually drives storms.`);
}

/* ---------- section: L1 -> Earth ---------- */
function liveVerdict(K) {
  if (K.rho_shifted == null) return `Only ${K.n} three-hour windows so far; at least 8 are needed for a first estimate.`;
  const chance = K.p_shifted == null ? null : K.p_shifted < 0.001 ? "less than 0.1%" : `about ${fmt(100 * K.p_shifted)}%`;
  const strength = Math.abs(K.rho_shifted) >= 0.7 ? "strong" : Math.abs(K.rho_shifted) >= 0.4 ? "moderate" : Math.abs(K.rho_shifted) >= 0.2 ? "weak" : "very weak";
  let txt = `ρ = ${fmt(K.rho_shifted, 2)} (${strength}) across ${K.n} three-hour windows, after shifting each reading by its travel time (about ${fmt(K.mean_lead_min)} min). `;
  if (chance) txt += `If coupling and Kp were unrelated, a ρ this far from zero would still turn up ${chance} of the time by chance (p = ${fmt(K.p_shifted, 3)}). `;
  if (K.p_shifted != null && K.p_shifted >= 0.05) {
    txt += `So this can't yet be told apart from random variation`;
    txt += K.n_needed ? `: a true ρ of this size needs about ${K.n_needed} windows (${fmt(K.n_needed / 8)} days) to show up reliably.` : ".";
    if (K.kp_range && K.kp_range[1] - K.kp_range[0] < 2) txt += ` Kp has also stayed in a narrow range (${fmt(K.kp_range[0], 1)}–${fmt(K.kp_range[1], 1)}), which hides any relationship; a storm would make it much clearer.`;
  } else if (K.p_shifted != null) {
    txt += `That's unlikely to be luck: the link is showing up in this pipeline's own data.`;
  }
  return txt;
}

function renderPredict(D) {
  const K = D.kp_live, LT = D.longterm;
  const o = baseOpts("Kp at Earth");
  o.interaction = { mode: "nearest", intersect: true };
  o.scales.x = axisLin("Coupling arriving at Earth, 3-hour average (relative units)");
  o.scales.y.min = 0; o.scales.y.suggestedMax = 6;
  o.plugins.tooltip.callbacks = { label: c => ` coupling ${fmt(c.parsed.x, 1)} · Kp ${fmt(c.parsed.y, 2)}` };
  draw("kp-live-chart", { type: "scatter", data: { datasets: [{ data: (K.pairs || []).map(p => ({ x: p.newell, y: p.kp })),
    backgroundColor: css("--series-1"), borderColor: css("--surface-1"), borderWidth: 2, pointRadius: 5, pointHoverRadius: 7 }] }, options: o });
  $("kp-live-note").textContent = K.n ? liveVerdict(K) : "Collecting data: the comparison appears once a few three-hour windows have passed.";

  if (!LT || !LT.available) { ["lag-chart", "resid-chart"].forEach(id => $(id).closest("div").style.display = "none"); return; }
  const KD = LT.kp_drivers;
  $("lag-title").textContent = `How long Earth takes to respond (NASA OMNI, ${new Date(LT.start).getFullYear()}–${new Date(LT.end).getFullYear()})`;
  const o2 = baseOpts("ρ (rank correlation)");
  o2.scales.x = { type: "category", title: { display: true, text: "hours between the wind arriving and Kp", color: css("--text-muted") },
                  grid: { display: false }, ticks: { color: css("--text-muted") }, border: { display: false } };
  o2.scales.y.min = 0; o2.scales.y.max = 1;
  o2.plugins.tooltip.callbacks = { label: c => ` ρ = ${fmt(c.parsed.y, 3)}` };
  draw("lag-chart", { type: "bar", data: { labels: KD.lag_hours.map(h => `${h}`), datasets: [{ data: KD.lag_rho, borderRadius: 3,
    backgroundColor: KD.lag_hours.map(h => h === KD.best_lag_h ? css("--series-1") : css("--seq-2")) }] }, options: o2 });
  $("lag-note").textContent = `The match is strongest when coupling is compared with Kp about ${KD.best_lag_h} h later (ρ = ${fmt(KD.rho_best, 2)} over ` +
    `${KD.n_windows.toLocaleString()} windows; with that many samples the chance of this being luck is effectively zero). ` +
    `Earth's field stores energy before releasing it, so Kp trails the solar wind.`;

  const a = KD.r2_coupling, b = Math.max(KD.r2_plus_memory - a, 0), c2 = Math.max(KD.r2_plus_season - KD.r2_plus_memory, 0), rest = Math.max(1 - KD.r2_plus_season, 0);
  const segs = [["solar wind coupling", a, "--series-1"], ["memory (Kp 3 h earlier)", b, "--series-2"], ["time of year", c2, "--series-3"]];
  $("explain-bar").innerHTML = segs.map(([l, v, col]) => `<div style="width:${100 * v}%;background:${css(col)}" title="${l}: ${fmt(100 * v)}%">${v > .07 ? fmt(100 * v) + "%" : ""}</div>`).join("") +
    `<div class="rest" style="width:${100 * rest}%" title="not explained: ${fmt(100 * rest)}%">${rest > .07 ? fmt(100 * rest) + "%" : ""}</div>`;
  $("explain-bar").insertAdjacentHTML("afterend", `<div class="explain-key">${segs.map(([l, , col]) => `<span><i style="background:${css(col)}"></i>${l}</span>`).join("")}` +
    `<span><i style="background:${css("--surface-2")};box-shadow:inset 0 0 0 1px ${css("--border")}"></i>unexplained</span></div>`);
  $("explain-note").textContent = `Coupling alone explains ${fmt(100 * a)}% of Kp's ups and downs. Knowing Kp three hours earlier adds ${fmt(100 * b)} points, ` +
    `because a disturbed field takes time to settle. ` +
    (c2 >= 0.01 ? `The time of year adds ${fmt(100 * c2)} more. ` : `The time of year adds almost nothing once those two are known: the seasonal effect is real but small next to day-to-day swings. `) +
    `The remaining ${fmt(100 * rest)}% comes from things this model doesn't see, ` +
    `such as energy stored and suddenly released in Earth's magnetic tail, and the gap between the wind measured near L1 and the wind that actually arrives.`;

  const o3 = baseOpts("Kp above / below prediction");
  o3.scales.x = { type: "category", grid: { display: false }, ticks: { color: css("--text-muted") }, border: { display: false } };
  o3.plugins.tooltip.callbacks = { label: c => ` ${c.parsed.y >= 0 ? "+" : ""}${fmt(c.parsed.y, 2)} Kp vs. the coupling-only prediction` };
  o3.plugins.refs = { lines: [{ y: 0, label: "" }] };
  draw("resid-chart", { type: "bar", data: { labels: MON, datasets: [{ data: KD.resid_by_month, borderRadius: 3,
    backgroundColor: KD.resid_by_month.map(v => v >= 0 ? css("--series-2") : css("--seq-2")) }] }, options: o3 });
  const rm = KD.resid_by_month, top2 = rm.map((v, i) => [v, i]).sort((x, y) => y[0] - x[0]).slice(0, 2).map(([, i]) => MON[i]);
  $("resid-note").textContent = `Bars above zero: Earth was more disturbed than the solar wind alone predicts. The largest excess is in ${top2.join(" and ")}` +
    ` (up to ${fmt(Math.max(...rm), 2)} Kp), the equinox effect from the month chart above. It's a real pattern, but a fraction of a Kp step.`;

  $("predict-insight").textContent = `Over ${new Date(LT.start).getFullYear()}–${new Date(LT.end).getFullYear()}, the wind's coupling explains about ${fmt(100 * a)}% of Kp, ` +
    `with the best match ${KD.best_lag_h} h after the wind arrives. ${K.n ? `This pipeline's own data so far: ρ = ${fmt(K.rho_shifted, 2)} after the time shift.` : ""}`;
}

/* ---------- section: agreement ---------- */
function renderAgree(D) {
  const A = D.agreement;
  const sync = A.three_way_sync_pct ?? A.any_pair_sync_pct;
  $("sync-num").textContent = sync == null ? "–" : `${fmt(sync)}%`;
  $("sync-label").textContent = A.three_way_sync_pct != null ? "of the time all three move in step (last 7 days, offsets removed)"
    : A.any_pair_sync_pct != null ? "of the time the reporting spacecraft move in step (all three rarely report together)" : "not enough overlapping data yet";
  const cls = { "in step": "instep", "partly in step": "partly", "out of step": "out", "not enough data": "none" };
  const words = { "in step": "Rises and falls with the others: trust its changes.",
                  "partly in step": "Follows the big moves, drifts on the small ones.",
                  "out of step": "Often moves differently: check before trusting it.",
                  "not enough data": "Too little overlap with the others to judge yet." };
  const off = (v, unit) => v.bias == null ? "—" : `${v.bias > 0 ? "+" : ""}${fmt(v.bias, unit === "nT" ? 1 : 0)}${unit === "nT" ? " nT" : "%"}`;
  const varRow = (label, v, unit) => `<tr><td>${label}</td><td>${v && v.track_pct != null ? fmt(v.track_pct) + "%" : "—"}</td><td>${v && v.track_pct != null ? off(v, unit) : (v ? v.n + " bins" : "—")}</td></tr>`;
  $("sc-cards").innerHTML = SRC.filter(([k]) => A.spacecraft[k]).map(([k, name, c]) => {
    const q = A.spacecraft[k];
    return `<div class="sc" style="--c:${css(c)}"><div class="name">${name}<span class="badge ${cls[q.verdict]}">${q.verdict}</span></div>
      <div class="row"><span>Moves with the others</span><b>${q.track_pct == null ? "—" : fmt(q.track_pct) + "%"}</b></div>
      <div class="row"><span>Reporting</span><b>${fmt(q.coverage_pct)}%</b></div>
      <table class="vars"><tr><th></th><th>in step</th><th>offset</th></tr>${varRow("Speed", q.vars.speed, "%")}${varRow("Density", q.vars.density, "%")}${varRow("Bz", q.vars.bz, "nT")}</table>
      <div class="row"><span>Bz sign vs. the other two</span><b>${q.bz_contrarian_pct == null ? "—" : fmt(q.bz_contrarian_pct, 1) + "% opposite"}</b></div>
      ${q.calibration_flags.length ? `<div class="flags">Calibration: ${q.calibration_flags.join(", ")} vs. the others</div>` : ""}
      <div class="note">${words[q.verdict]}</div></div>`;
  }).join("") + `<p class="note" style="grid-column:1/-1">In step: share of 5-minute steps within tolerance after removing the offset. Offset: how far it reads above or below the group on average. ` +
    (D.calibration && D.calibration.factors && Object.keys(D.calibration.factors).length ? `The headline numbers at the top of the page combine spacecraft on ${NAME[D.calibration.reference]}'s scale: ` +
      Object.entries(D.calibration.factors).map(([k, f]) => `${NAME[k]} ${Object.entries(f).map(([c, v]) => `${c.replace("proton_", "")} ×${fmt(v, 2)}`).join(", ")}`).join("; ") + ". " : "") +
    (A.aligned_by_position ? `Times were lined up first: ${SRC.filter(([k]) => A.spacecraft[k]).map(([k, n]) => `${n} ${A.spacecraft[k].time_shift_min >= 0 ? "+" : ""}${fmt(A.spacecraft[k].time_shift_min, 1)} min`).join(", ")}.` : "") + `</p>`;

  const T = A.strip.t;
  $("strip").innerHTML = SRC.filter(([k]) => A.strip.status[k]).map(([k, name]) =>
    `<div class="lane"><span>${name}</span><div class="cells" style="grid-template-columns:repeat(${T.length},1fr)">` +
    A.strip.status[k].map((st, i) => `<i class="sw ${st}" title="${when(T[i])}: ${st === "nodata" ? "no data" : st === "solo" ? "reporting alone" : st === "agree" ? "agrees" : "differs"}"></i>`).join("") +
    `</div></div>`).join("");

  const cols = r => {
    const sp = r.proton_speed || {}, bz = r.bz_gsm || {};
    const few = sp.r == null && bz.r == null;
    return `<tr><td>${r.pair.replace("SOLAR1", "SOLAR-1")}</td>` +
      (few ? `<td colspan="4" style="text-align:left">too little overlap (${Math.max(sp.n || 0, bz.n || 0)} five-minute bins)</td>`
           : `<td>${fmt(sp.r, 3)}</td><td>${sp.median_bias == null ? "—" : (sp.median_bias > 0 ? "+" : "") + fmt(sp.median_bias, 1) + " km/s"}</td>` +
             `<td>${fmt(bz.r, 3)}</td><td>${bz.median_bias == null ? "—" : (bz.median_bias > 0 ? "+" : "") + fmt(bz.median_bias, 2) + " nT"}</td>`) +
      `<td>${r.bz_best_lag_min != null ? r.bz_best_lag_min + " min" : "—"}</td><td>${r.position_lag_min != null ? r.position_lag_min + " min" : "—"}</td>` +
      `<td>${r.separation_km != null ? fmt(r.separation_km / 1000) + "k km" : "—"}</td></tr>`;
  };
  $("pair-table").innerHTML = `<tr><th>Pair</th><th>Speed r</th><th>Speed bias</th><th>Bz r</th><th>Bz bias</th><th>Measured lag</th><th>Predicted lag</th><th>Apart</th></tr>` +
    D.cross_spacecraft.map(cols).join("");
}

/* ---------- section: separation ---------- */
function renderSeparation(D) {
  const S = D.separation || { points: [] };
  const pts = S.points.map(p => ({ x: p.sep_km / 1000, y: p.speed_diff_pct, pair: p.pair, day: p.day }));
  const ds = [{ label: "pair-days", data: pts, backgroundColor: css("--series-1"), borderColor: css("--surface-1"), borderWidth: 2, pointRadius: 5, pointHoverRadius: 7 }];
  if (S.fit && pts.length) {
    const xs = pts.map(p => p.x), x0 = 0, x1 = Math.max(...xs) * 1.1;
    ds.push({ label: "trend", type: "line", data: [{ x: x0, y: S.fit.intercept_pct + S.fit.pct_per_100k_km * x0 / 100 },
      { x: x1, y: S.fit.intercept_pct + S.fit.pct_per_100k_km * x1 / 100 }], borderColor: css("--text-muted"), borderDash: [5, 5], borderWidth: 1.5, pointRadius: 0 });
  }
  const o = baseOpts("typical speed difference (%)");
  o.interaction = { mode: "nearest", intersect: true };
  o.scales.x = axisLin("distance between the two spacecraft (thousand km)", { min: 0 });
  o.scales.y.min = 0;
  o.plugins.tooltip.callbacks = { label: c => c.raw.pair ? ` ${c.raw.pair.replace("SOLAR1", "SOLAR-1")}, ${new Date(c.raw.day).toLocaleDateString()}: ${fmt(c.raw.x)}k km apart, ${fmt(c.raw.y, 1)}% different` : " trend" };
  draw("sep-chart", { type: "scatter", data: { datasets: ds }, options: o });
  if (S.fit) {
    const E = S.earth_estimate;
    $("sep-insight").textContent = `Across ${S.fit.n} pair-days, measured speeds differ by about ${fmt(S.fit.pct_per_100k_km, 2)}% more for every extra 100,000 km between spacecraft ` +
      (S.fit.intercept_pct > 0 ? `(${fmt(S.fit.intercept_pct, 1)}% even when close, mostly instrument differences).` : ".") +
      (S.fit.n < 30 ? " Early estimate: it sharpens as more pair-days accumulate." : "") +
      (E ? ` ${NAME[E.source]} sits ${fmt(E.off_axis_km / 1000)},000 km off the Sun–Earth line, so the wind that reaches Earth may differ from its reading by roughly ${fmt(E.expected_speed_diff_pct, 1)}%.` : "");
  } else {
    $("sep-insight").textContent = `Collecting: the trend needs at least ${S.needed || 10} pair-days of overlap spread across different separations (currently ${pts.length}). As the halo orbits carry the spacecraft closer together and further apart over the coming months, this becomes a real measurement.`;
  }
}

/* ---------- boot ---------- */
fetch("data/latest.json", { cache: "no-store" })
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(D => {
    window.L1DATA = D;
    for (const f of [renderNow, renderExplore, renderLongview, renderWhere, renderField, renderPredict, renderAgree, renderSeparation]) {
      try { f(D); } catch (e) { console.error(f.name, e); }
    }
    window.dispatchEvent(new CustomEvent("l1data", { detail: D }));
  })
  .catch(e => { console.error(e); $("meta").textContent = "No data yet. Run the pipeline (python -m pipeline.run) or wait for the scheduled GitHub Action."; });

/* Older phones (iOS before 16.4) can't load the 3D model's modules. Everything else still works;
   replace the "Loading" text with an honest message instead of leaving it spinning. */
setTimeout(() => {
  const f = document.querySelector(".magneto-fallback");
  if (f) f.textContent = "The 3D model needs a newer browser (iOS 16.4 or later, or a current Chrome, Firefox or Edge). The rest of the page works normally.";
}, 12000);
