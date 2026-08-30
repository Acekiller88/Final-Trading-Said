/* Crypto 15M Signal Scanner — dashboard logic (vanilla JS, no dependencies).
 *
 * Data flow:
 *  - /data/*.json files are refreshed every 60 s (produced by the Python engine
 *    every 15 min via GitHub Actions). The "last data update" label always shows
 *    the browser refresh time of those files.
 *  - Live prices are fetched directly from Binance's public ticker every 20 s.
 *    A price is labelled "LIVE" ONLY when that browser fetch succeeded; if it
 *    fails we fall back to the scan-time price and label it "SCAN".
 *  - Performance metrics for the selected date range are recomputed client-side
 *    from signals.json with the same definitions as the Python engine
 *    (win rate = wins / (wins + losses); ambiguous/expired excluded).
 */
"use strict";

const DATA_INTERVAL_MS = 60_000;
const PRICE_INTERVAL_MS = 20_000;
const PRICE_ENDPOINTS = [
  "https://fapi.binance.com/fapi/v1/ticker/price",
  "https://www.binance.com/fapi/v1/ticker/price",
];

const TZ = "Asia/Kuala_Lumpur";
const $ = (id) => document.getElementById(id);
const fmtTime = (ms) => ms ? new Date(ms).toLocaleString("en-MY", {
  timeZone: TZ, hour12: false, day: "2-digit", month: "short",
  hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
const fmtClock = (ms) => ms ? new Date(ms).toLocaleTimeString("en-MY", {
  timeZone: TZ, hour12: false }) : "—";
const fmtPrice = (v) => v == null ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 6 });
const fmtAgo = (ms) => {
  if (!ms) return "—";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
};
const fmtDur = (ms) => {
  if (ms == null) return "—";
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
};

const state = {
  signals: [], status: null, snapshots: [], performance: null,
  lastDataUpdate: null, embeddedLoaded: false,
  livePrices: {}, liveOk: false, liveAt: null,
  activeFilter: new Set(["A+", "A", "B+"]),
  perfRange: 7, histRange: 7, histDir: "all", histQuality: "all",
  histOutcome: "all", histSearch: "",
};

/* ------------------------------------------------------------------ data */
async function loadJSON(name) {
  const res = await fetch(`data/${name}?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
  return res.json();
}

async function refreshData() {
  try {
    const [signals, status, snapshots, performance] = await Promise.all([
      loadJSON("signals.json"), loadJSON("system-status.json"),
      loadJSON("market-snapshots.json").catch(() => []),
      loadJSON("performance.json").catch(() => null),
    ]);
    state.signals = Array.isArray(signals?.signals) ? signals.signals : [];
    state.status = status || null;
    state.snapshots = Array.isArray(snapshots) ? snapshots : [];
    state.performance = performance;
    state.lastDataUpdate = Date.now();
    state.embeddedLoaded = false;
    $("preview-notice").classList.add("hidden");
    renderAll();
  } catch (err) {
    // No reachable data files (offline file:// open, sandboxed preview, blocked
    // network): fall back to the snapshot embedded at build time, if present.
    if (window.__EMBEDDED_DATA__ && !state.embeddedLoaded) {
      const d = window.__EMBEDDED_DATA__;
      state.signals = Array.isArray(d.signals?.signals) ? d.signals.signals : [];
      state.status = d.status || null;
      state.snapshots = Array.isArray(d.snapshots) ? d.snapshots : [];
      state.performance = d.performance || null;
      state.lastDataUpdate = d.builtAt || null;
      state.embeddedLoaded = true;
      renderAll();
    }
    console.warn("data refresh failed:", err);
    $("preview-notice").classList.remove("hidden");
  }
}

/* live prices from the browser (Binance public ticker, CORS-enabled) */
async function refreshPrices() {
  const symbols = [...new Set(state.signals
    .filter(s => s.status === "WAITING_TRIGGER" || s.status === "TRIGGERED")
    .map(s => s.symbol))];
  if (!symbols.length) { state.liveOk = false; renderTopChips(); return; }
  for (const base of PRICE_ENDPOINTS) {
    try {
      const qs = encodeURIComponent(JSON.stringify(symbols.slice(0, 40)));
      const res = await fetch(`${base}?symbols=${qs}`, { cache: "no-store" });
      if (!res.ok) continue;
      const rows = await res.json();
      if (!Array.isArray(rows)) continue;
      rows.forEach(r => { state.livePrices[r.symbol] = parseFloat(r.price); });
      state.liveOk = true;
      state.liveAt = Date.now();
      renderTopChips(); renderActive(); renderRecent();
      return;
    } catch (_) { /* try next endpoint */ }
  }
  state.liveOk = false;
  renderTopChips(); renderActive(); renderRecent();
}

/* ------------------------------------------------------------------ render */
function renderAll() {
  renderTopChips();
  renderStatus();
  renderOverview();
  renderActive();
  renderRecent();
  renderPerformance();
  renderHistory();
  renderLogs();
  $("ft-version").textContent = state.status?.version ? `engine v${state.status.version}` : "";
}

function renderTopChips() {
  const st = state.status;
  const health = st?.health ?? "UNKNOWN";
  const chip = $("chip-health");
  chip.textContent = health === "HEALTHY" ? "SYSTEM ONLINE" :
    health === "DEGRADED" ? "DEGRADED" : health === "FAILED" ? "SYSTEM OFFLINE" : "…";
  chip.className = "chip " + (health === "HEALTHY" ? "chip-ok" :
    health === "DEGRADED" ? "chip-warn" : "chip-bad");
  const last = st?.lastScan?.executedAt;
  $("chip-lastscan").textContent = `last scan ${last ? fmtClock(last) + " MYT" : "—"}`;
  const next = st?.nextExpectedScanAt;
  $("chip-nextscan").textContent = next ? `next ~${fmtClock(next)} MYT` : "next —";
  const pm = $("chip-price-mode");
  if (state.liveOk) {
    pm.textContent = `LIVE PRICE ${fmtClock(state.liveAt)}`;
    pm.className = "chip chip-ok";
  } else {
    pm.textContent = "price: scan snapshot";
    pm.className = "chip";
  }
  $("data-updated").textContent = state.lastDataUpdate
    ? `Last data update: ${fmtTime(state.lastDataUpdate)}${state.embeddedLoaded ? " (embedded snapshot — offline file)" : ""}`
    : "Last data update: —";

  const notice = $("health-notice");
  if (health === "FAILED") {
    notice.textContent = "⚠ Last scan FAILED — API unreachable. Showing the last successful scan data. No simulated data is ever shown.";
    notice.classList.remove("hidden");
  } else if (health === "DEGRADED") {
    notice.textContent = "⚠ Last scan completed with degraded data quality (some symbols failed or data delayed).";
    notice.classList.remove("hidden");
  } else notice.classList.add("hidden");
}

function renderStatus() {
  const st = state.status; if (!st) return;
  const ls = st.lastScan || {};
  $("st-system").textContent = st.health ?? "—";
  $("st-health-sub").textContent = ls.status ? `scan ${ls.status}` : "—";
  $("st-lastscan").textContent = ls.executedAt ? fmtClock(ls.executedAt) : "—";
  $("st-jitter").textContent = ls.jitterSeconds != null ? `scheduled ${fmtClock(ls.scheduledAt)} · jitter ${ls.jitterSeconds}s` : "—";
  $("st-nextscan").textContent = st.nextExpectedScanAt ? fmtClock(st.nextExpectedScanAt) : "—";
  $("st-duration").textContent = ls.durationMs != null ? (ls.durationMs / 1000).toFixed(1) + "s" : "—";
  $("st-symbols").textContent = `${ls.symbolsValid ?? 0}/${ls.symbolsScanned ?? 0} valid · ${ls.dataFailures ?? 0} failed`;
  $("st-universe").textContent = `${ls.universeSize ?? st.universeSize ?? "—"} pairs`;
  $("st-universe-src").textContent = ls.universeSource ? `via ${ls.universeSource}` : "—";
  const api = ls.apiHealth ?? "—";
  $("st-api").textContent = api;
  $("st-api-reqs").textContent = ls.apiStats ? `${ls.apiStats.requests ?? 0} req · ${ls.apiStats.errors ?? 0} err · ${ls.apiStats.retries ?? 0} retry` : "—";
  const fresh = ls.dataFreshness ?? {};
  $("st-fresh").textContent = fresh.label ?? "—";
  $("st-fresh-age").textContent = fresh.ageSeconds != null ? `candle age ${fmtDur(fresh.ageSeconds * 1000)}` : "—";
  const active = state.signals.filter(s => s.status === "WAITING_TRIGGER" || s.status === "TRIGGERED");
  $("st-active").textContent = active.length;
  $("st-active-sub").textContent = `${active.filter(s => s.status === "TRIGGERED").length} triggered · ${active.filter(s => s.status === "WAITING_TRIGGER").length} waiting`;
}

function renderOverview() {
  const snap = state.snapshots[state.snapshots.length - 1];
  const bar = $("breadth-bar"), legend = $("breadth-legend");
  bar.innerHTML = ""; legend.innerHTML = "";
  if (!snap) return;
  const colors = { TRENDING_UP: "var(--green)", TRENDING_DOWN: "var(--red)",
                   RANGING: "var(--amber)", MIXED: "var(--blue)" };
  const total = Object.values(snap.breadth || {}).reduce((a, b) => a + b, 0) || 1;
  for (const [k, v] of Object.entries(snap.breadth || {})) {
    if (!v) continue;
    const seg = document.createElement("div");
    seg.className = "seg";
    seg.style.background = colors[k] || "var(--muted)";
    seg.style.width = `${(v / total) * 100}%`;
    seg.title = `${k}: ${v}`;
    if (v / total > 0.08) seg.textContent = v;
    bar.appendChild(seg);
    const li = document.createElement("span");
    li.innerHTML = `<span class="dot" style="background:${colors[k]}"></span>${k.replace("_", " ")} · ${v} (${Math.round(100 * v / total)}%)`;
    legend.appendChild(li);
  }
  const top = snap.topVolume || [];
  $("top-volume").innerHTML = top.slice(0, 10).map((t, i) =>
    `<li><span><span class="rank">#${i + 1}</span>${t.symbol}</span>
     <span>$${Number(t.quoteVolume / 1e6).toFixed(1)}M</span></li>`).join("");
}

function signalCard(s) {
  const long = s.direction === "LONG";
  const live = state.livePrices[s.symbol];
  const price = state.liveOk && live != null ? live : s.currentPrice;
  const priceLbl = state.liveOk && live != null ? "LIVE MARKET PRICE" : "PRICE (SCAN)";
  const trigger = s.triggerPrice;
  const gapPct = trigger ? ((price - trigger) / trigger * 100) : null;
  const gapTxt = gapPct == null ? "" :
    (s.status === "WAITING_TRIGGER"
      ? (long ? `needs +${Math.max(0, ((trigger - price) / price) * 100).toFixed(2)}% to trigger`
              : `needs −${Math.max(0, ((price - trigger) / price) * 100).toFixed(2)}% to trigger`)
      : `filled @ ${fmtPrice(s.entryPrice)}`);
  const zone = Array.isArray(s.entryZone) ? s.entryZone : [null, null];
  const f = s.fvg, ob = s.orderBlock;
  const card = document.createElement("div");
  card.className = `sig-card ${long ? "long" : "short"}`;
  card.innerHTML = `
    <div class="sig-head">
      <span class="sig-symbol">${s.symbol} <span class="sig-dir">${s.direction}</span></span>
      <span class="sig-quality q-${s.quality}">${s.quality} · ${Number(s.score).toFixed(1)}/100</span>
    </div>
    <div class="sig-price-row">
      <div class="price-box">
        <span class="lbl">${priceLbl} <span>${state.liveOk && live != null ? fmtAgo(state.liveAt) : fmtAgo(s.currentPriceAt)}</span></span>
        <div class="val">${fmtPrice(price)}</div>
      </div>
      <div class="price-box">
        <span class="lbl">TRIGGER</span>
        <div class="val">${fmtPrice(trigger)}</div>
        <div class="trigger-gap">${gapTxt}</div>
      </div>
    </div>
    <div class="sig-levels">
      <div class="lvl"><span class="lbl">Entry zone</span><div class="val">${fmtPrice(zone[0])}–${fmtPrice(zone[1])}</div></div>
      <div class="lvl tp"><span class="lbl">Take profit</span><div class="val">${fmtPrice(s.takeProfit)}</div></div>
      <div class="lvl sl"><span class="lbl">Stop loss</span><div class="val">${fmtPrice(s.stopLoss)}</div></div>
      <div class="lvl rr"><span class="lbl">Risk / reward</span><div class="val">1 : ${Number(s.riskReward).toFixed(2)}</div></div>
      <div class="lvl"><span class="lbl">4H bias</span><div class="val">${(s.htf4hBias || "—").replace("_", " ")}</div></div>
      <div class="lvl"><span class="lbl">1H bias</span><div class="val">${(s.htf1hBias || "—").replace("_", " ")}</div></div>
    </div>
    <div class="sig-meta">
      <span class="tag on">sweep ${s.liquiditySweep ? "@" + fmtPrice(s.liquiditySweep.level) : "—"}</span>
      <span class="tag ${s.structure?.event?.startsWith("CHoCH") ? "on" : ""}">${s.structure?.event ?? "—"}</span>
      <span class="tag ${s.displacement ? "on" : "off"}">displacement ${s.displacement ? "✓" : "✗"}</span>
      <span class="tag ${f ? "on" : "off"}">FVG ${f ? "@" + fmtPrice(f.bottom) + "–" + fmtPrice(f.top) : "✗"}</span>
      <span class="tag ${ob ? "on" : "off"}">OB ${ob ? "@" + fmtPrice(ob.bottom) + "–" + fmtPrice(ob.top) : "✗"}</span>
      <span class="tag">vol ${Number(s.relativeVolume).toFixed(2)}×</span>
      <span class="tag">RSI ${Number(s.rsi).toFixed(0)}</span>
      <span class="tag">ADX ${Number(s.adx).toFixed(0)}</span>
      <span class="tag">${s.marketRegime ?? ""}</span>
    </div>
    <div class="sig-status">
      <span class="status-badge st-${s.status}">${s.status.replace("_", " ")}</span>
      <span class="sig-time">generated ${fmtTime(s.generatedAt)} · ${s.timeframe}</span>
    </div>`;
  return card;
}

function renderActive() {
  const grid = $("active-grid"), empty = $("active-empty");
  const active = state.signals
    .filter(s => (s.status === "WAITING_TRIGGER" || s.status === "TRIGGERED")
                 && state.activeFilter.has(s.quality))
    .sort((a, b) => b.score - a.score || b.generatedAt - a.generatedAt);
  grid.innerHTML = "";
  empty.classList.toggle("hidden", state.signals
    .filter(s => s.status === "WAITING_TRIGGER" || s.status === "TRIGGERED").length > 0);
  const order = { "A+": 0, "A": 1, "B+": 2 };
  active.sort((a, b) => (order[a.quality] ?? 9) - (order[b.quality] ?? 9) || b.score - a.score);
  active.forEach(s => grid.appendChild(signalCard(s)));
}

function renderRecent() {
  const tbody = document.querySelector("#recent-table tbody");
  const recent = [...state.signals].sort((a, b) => b.generatedAt - a.generatedAt).slice(0, 10);
  $("recent-empty").classList.toggle("hidden", recent.length > 0);
  tbody.innerHTML = recent.map(s => `
    <tr>
      <td>${fmtTime(s.generatedAt)}</td>
      <td><b>${s.symbol}</b></td>
      <td class="${s.direction === "LONG" ? "dir-l" : "dir-s"}">${s.direction}</td>
      <td class="sig-quality q-${s.quality}">${s.quality} ${Number(s.score).toFixed(0)}</td>
      <td>${fmtPrice(s.triggerPrice)}</td>
      <td>${fmtPrice(s.takeProfit)}</td>
      <td>${fmtPrice(s.stopLoss)}</td>
      <td>1:${Number(s.riskReward).toFixed(2)}</td>
      <td><span class="status-badge st-${s.status}">${s.status.replace("_", " ")}</span></td>
    </tr>`).join("");
}

/* --------------------------------------------------- performance (JS mirror) */
function perfFor(rangeDays) {
  const since = rangeDays ? Date.now() - rangeDays * 864e5 : 0;
  const sigs = state.signals.filter(s => s.generatedAt >= since);
  const count = (st) => sigs.filter(s => s.status === st).length;
  const wins = count("WIN"), losses = count("LOSS");
  const rWins = sigs.filter(s => s.status === "WIN").map(s => s.rMultiple ?? 0);
  const rLosses = sigs.filter(s => s.status === "LOSS").map(s => s.rMultiple ?? 0);
  const grossW = rWins.reduce((a, b) => a + b, 0);
  const grossL = Math.abs(rLosses.reduce((a, b) => a + b, 0));
  const byDir = (d) => {
    const sub = sigs.filter(s => s.direction === d);
    const w = sub.filter(s => s.status === "WIN").length;
    const l = sub.filter(s => s.status === "LOSS").length;
    return { w, l, n: w + l, wr: w + l ? (100 * w / (w + l)) : null };
  };
  const byQ = (q) => {
    const sub = sigs.filter(s => s.quality === q);
    const w = sub.filter(s => s.status === "WIN").length;
    const l = sub.filter(s => s.status === "LOSS").length;
    return { w, l, n: w + l, wr: w + l ? (100 * w / (w + l)) : null };
  };
  return {
    total: sigs.length, waiting: count("WAITING_TRIGGER"), triggeredOpen: count("TRIGGERED"),
    wins, losses, expired: count("EXPIRED"), ambiguous: count("AMBIGUOUS"), cancelled: count("CANCELLED"),
    resolved: wins + losses,
    winRate: wins + losses ? 100 * wins / (wins + losses) : null,
    pf: grossL > 0 ? grossW / grossL : null,
    avgRr: sigs.length ? sigs.reduce((a, s) => a + (s.riskReward || 0), 0) / sigs.length : null,
    avgScore: sigs.length ? sigs.reduce((a, s) => a + (s.score || 0), 0) / sigs.length : null,
    long: byDir("LONG"), short: byDir("SHORT"),
    q: { "A+": byQ("A+"), "A": byQ("A"), "B+": byQ("B+") },
    sigs,
  };
}

function renderPerformance() {
  const p = perfFor(state.perfRange);
  $("pf-total").textContent = p.total;
  $("pf-waiting").textContent = `${p.waiting} waiting · ${p.triggeredOpen} open`;
  $("pf-resolved").textContent = p.resolved;
  $("pf-split").textContent = `${p.wins}W / ${p.losses}L · ${p.expired} exp · ${p.ambiguous} amb · ${p.cancelled} canc`;
  $("pf-winrate").textContent = p.winRate == null ? "—" + "" : p.winRate.toFixed(1) + "%";
  $("pf-winrate-n").textContent = `based on ${p.wins} wins / ${p.resolved} resolved trades`;
  $("pf-pf").textContent = p.pf == null ? "—" : p.pf.toFixed(2);
  $("pf-rr").textContent = p.avgRr == null ? "—" : "1:" + p.avgRr.toFixed(2);
  $("pf-score").textContent = p.avgScore == null ? "—" : `avg score ${p.avgScore.toFixed(1)}`;

  const st = state.performance;
  $("pf-streaks").textContent = st ? `${st.maxWinningStreak ?? 0} / ${st.maxLosingStreak ?? 0}` : "—";
  $("pf-times").textContent = st && st.avgTimeToTpMs != null
    ? `avg→TP ${fmtDur(st.avgTimeToTpMs)} · avg→SL ${fmtDur(st.avgTimeToSlMs ?? null)}` : "—";

  drawEquityChart(p.sigs);
  drawQualityChart(p);
  drawDirectionChart(p);
  drawOutcomeChart(p);
}

/* --------------------------------------------------- SVG charts (no libs) */
const NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function drawEquityChart(sigs) {
  const box = $("chart-equity"); box.innerHTML = "";
  const resolved = sigs.filter(s => s.status === "WIN" || s.status === "LOSS")
    .sort((a, b) => (a.closedAt ?? 0) - (b.closedAt ?? 0));
  if (resolved.length < 2) {
    box.innerHTML = `<div class="empty">Needs ≥ 2 resolved trades to plot
      (currently ${resolved.length}). History accumulates as signals resolve.</div>`;
    return;
  }
  const W = 560, H = 190, PAD = 28;
  let cum = 0;
  const pts = resolved.map(s => { cum += (s.rMultiple ?? 0); return cum; });
  const min = Math.min(0, ...pts), max = Math.max(1, ...pts);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  const x = (i) => PAD + (i / (pts.length - 1)) * (W - 2 * PAD);
  const y = (v) => H - PAD - ((v - min) / (max - min)) * (H - 2 * PAD);
  [0, 0.5, 1].forEach(f => svg.appendChild(svgEl("line", {
    x1: PAD, x2: W - PAD, y1: y(min + f * (max - min)), y2: y(min + f * (max - min)),
    class: "grid-line" })));
  const zero = svgEl("line", { x1: PAD, x2: W - PAD, y1: y(0), y2: y(0), class: "zero-line" });
  svg.appendChild(zero);
  const path = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  svg.appendChild(svgEl("path", { d: path, fill: "none", stroke: pts[pts.length - 1] >= 0 ? "var(--green)" : "var(--red)", "stroke-width": 2 }));
  const last = svgEl("circle", { cx: x(pts.length - 1), cy: y(pts[pts.length - 1]), r: 3.5,
    fill: pts[pts.length - 1] >= 0 ? "var(--green)" : "var(--red)" });
  svg.appendChild(last);
  const label = svgEl("text", { x: W - PAD, y: Math.max(12, y(pts[pts.length - 1]) - 8),
    "text-anchor": "end", class: "bar-label" });
  label.textContent = `${pts[pts.length - 1] >= 0 ? "+" : ""}${pts[pts.length - 1].toFixed(1)}R over ${pts.length} trades`;
  svg.appendChild(label);
  box.appendChild(svg);
}

function drawQualityChart(p) {
  const box = $("chart-quality"); box.innerHTML = "";
  const rows = ["A+", "A", "B+"].map(q => ({ q, ...p.q[q] }));
  const W = 560, H = 190, rowH = 44, pad = 46, barW = W - pad - 90;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
  rows.forEach((r, i) => {
    const y0 = 16 + i * rowH;
    const n = r.w + r.l || 1;
    const wl = (r.w / n) * barW, ll = (r.l / n) * barW;
    svg.appendChild(svgEl("text", { x: pad - 8, y: y0 + 14, "text-anchor": "end", class: "axis-label" })).textContent = r.q;
    svg.appendChild(svgEl("rect", { x: pad, y: y0, width: Math.max(wl, 0), height: 20, rx: 3, fill: "var(--green)" }));
    svg.appendChild(svgEl("rect", { x: pad + wl, y: y0, width: Math.max(ll, 0), height: 20, rx: 3, fill: "var(--red)" }));
    svg.appendChild(svgEl("text", { x: pad + barW + 8, y: y0 + 14, class: "bar-label" })).textContent =
      `${r.w}W/${r.l}L${r.wr != null ? ` · ${r.wr.toFixed(0)}%` : ""} (n=${r.n})`;
  });
  box.appendChild(svg);
}

function drawDirectionChart(p) {
  const box = $("chart-direction"); box.innerHTML = "";
  const W = 560, H = 190, rowH = 60, pad = 60, barW = W - pad - 120;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
  [["LONG", p.long, "var(--green)"], ["SHORT", p.short, "var(--red)"]].forEach(([name, d], i) => {
    const y0 = 24 + i * rowH;
    const n = d.w + d.l || 1;
    svg.appendChild(svgEl("text", { x: pad - 8, y: y0 + 14, "text-anchor": "end", class: "axis-label" })).textContent = name;
    svg.appendChild(svgEl("rect", { x: pad, y: y0, width: barW, height: 20, rx: 3, fill: "var(--card-2)", stroke: "var(--line)" }));
    svg.appendChild(svgEl("rect", { x: pad, y: y0, width: Math.max((d.w / n) * barW, 0), height: 20, rx: 3, fill: d3color(name) }));
    svg.appendChild(svgEl("text", { x: pad + barW + 8, y: y0 + 14, class: "bar-label" })).textContent =
      `${d.w}W/${d.l}L · ${d.wr == null ? "—" : d.wr.toFixed(0) + "%"} (n=${d.n})`;
  });
  function d3color(name) { return name === "LONG" ? "var(--green)" : "var(--red)"; }
  box.appendChild(svg);
}

function drawOutcomeChart(p) {
  const box = $("chart-outcomes"); box.innerHTML = "";
  const parts = [["WIN", p.wins, "var(--green)"], ["LOSS", p.losses, "var(--red)"],
                 ["EXPIRED", p.expired, "var(--muted)"], ["AMBIGUOUS", p.ambiguous, "var(--violet)"],
                 ["CANCELLED", p.cancelled, "var(--amber)"]];
  const total = parts.reduce((a, [, v]) => a + v, 0) || 1;
  const W = 560, H = 190, rowH = 32, pad = 90, barW = W - pad - 80;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
  parts.forEach(([name, v, color], i) => {
    const y0 = 12 + i * rowH;
    svg.appendChild(svgEl("text", { x: pad - 8, y: y0 + 13, "text-anchor": "end", class: "axis-label" })).textContent = name;
    svg.appendChild(svgEl("rect", { x: pad, y: y0, width: Math.max((v / total) * barW, 1), height: 17, rx: 3, fill: color, opacity: .85 }));
    svg.appendChild(svgEl("text", { x: pad + (v / total) * barW + 6, y: y0 + 13, class: "bar-label" })).textContent = v;
  });
  box.appendChild(svg);
}

/* --------------------------------------------------- history table */
function renderHistory() {
  const p = perfFor(state.histRange);
  let rows = p.sigs;
  if (state.histDir !== "all") rows = rows.filter(s => s.direction === state.histDir.toUpperCase());
  if (state.histQuality !== "all") rows = rows.filter(s => s.quality === state.histQuality);
  if (state.histOutcome !== "all") rows = rows.filter(s => s.status === state.histOutcome);
  if (state.histSearch) rows = rows.filter(s => s.symbol.toLowerCase().includes(state.histSearch));
  rows.sort((a, b) => b.generatedAt - a.generatedAt);
  const tbody = document.querySelector("#history-table tbody");
  $("history-empty").classList.toggle("hidden", rows.length > 0);
  $("history-count").textContent = `${rows.length} signal(s) shown of ${state.signals.length} total`;
  tbody.innerHTML = rows.slice(0, 300).map(s => {
    const dur = s.closedAt && s.triggeredAt ? s.closedAt - s.triggeredAt :
      (s.closedAt ? s.closedAt - s.generatedAt : null);
    return `<tr>
      <td>${fmtTime(s.generatedAt)}</td>
      <td><b>${s.symbol}</b></td>
      <td class="${s.direction === "LONG" ? "dir-l" : "dir-s"}">${s.direction}</td>
      <td>${Number(s.score).toFixed(1)}</td>
      <td class="sig-quality q-${s.quality}">${s.quality}</td>
      <td>${fmtPrice(s.triggerPrice)}</td>
      <td>${fmtPrice(s.entryZone?.[0])}–${fmtPrice(s.entryZone?.[1])}</td>
      <td>${fmtPrice(s.takeProfit)}</td>
      <td>${fmtPrice(s.stopLoss)}</td>
      <td>1:${Number(s.riskReward).toFixed(2)}</td>
      <td><span class="status-badge st-${s.status}">${s.status.replace("_", " ")}</span></td>
      <td>${s.rMultiple != null ? (s.rMultiple > 0 ? "+" : "") + Number(s.rMultiple).toFixed(2) + "R" : "—"}</td>
      <td>${fmtDur(dur)}</td>
    </tr>`;
  }).join("");
}

function renderLogs() {
  const logs = state.status?.logs ?? [];
  const tail = logs.slice(-140).reverse();
  $("log-box").innerHTML = tail.length
    ? tail.map(l => {
        const cls = l.level === "error" ? "lv-error" : l.level === "warn" ? "lv-warn" : "lv-info";
        return `<span class="${cls}">[${fmtTime(l.ts)}] [${String(l.level).toUpperCase().padEnd(5)}] ${escapeHtml(l.msg)}</span>`;
      }).join("\n")
    : "—";
}
const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* --------------------------------------------------- interactions */
function bindPills(containerId, onPick, multi) {
  const root = $(containerId);
  root.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".pill"); if (!btn) return;
    if (multi) {
      const q = btn.dataset.q;
      if (state.activeFilter.has(q)) { btn.classList.remove("active"); state.activeFilter.delete(q); }
      else { btn.classList.add("active"); state.activeFilter.add(q); }
    } else {
      root.querySelectorAll(".pill").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    }
    onPick(btn);
  });
}
bindPills("active-filter", () => renderActive(), true);
bindPills("perf-range", (b) => { state.perfRange = +b.dataset.range; renderPerformance(); });
bindPills("hist-direction", (b) => { state.histDir = b.dataset.d; renderHistory(); });
bindPills("hist-quality", (b) => { state.histQuality = b.dataset.q; renderHistory(); });
bindPills("hist-outcome", (b) => { state.histOutcome = b.dataset.o; renderHistory(); });
bindPills("hist-range", (b) => { state.histRange = +b.dataset.range; renderHistory(); });
$("hist-search").addEventListener("input", (e) => {
  state.histSearch = e.target.value.trim().toLowerCase(); renderHistory();
});

/* --------------------------------------------------- boot */
refreshData();
refreshPrices();
setInterval(refreshData, DATA_INTERVAL_MS);
setInterval(refreshPrices, PRICE_INTERVAL_MS);
setInterval(renderTopChips, 10_000); // keep "ago" labels fresh
