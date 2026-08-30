# 📡 Crypto 15-Minute High-Quality Signal Scanner

A **zero-cost, production-ready crypto market-analysis and signal-generation system** for the
top-100 liquid Binance USDT-perpetual pairs. It scans every 15 minutes, identifies high-confluence
LONG/SHORT setups using multi-timeframe technical analysis (4H macro → 1H directional → 15M
execution) combined with SMC/institutional price-action logic, publishes them to a professional
dark dashboard, tracks every signal's outcome, and computes statistically honest performance
metrics.

> **This system never executes trades.** It is a quantitative, rule-based market screening and
> signal-analysis tool. It does **not** guarantee profitability and does **not** predict the
> market. The confluence score is a quality ranking, **not** a probability.

---

## 1. Architecture

```
GitHub Actions (cron */15)          ← free scheduler/compute
        ↓
Python Signal Engine (stdlib only)  ← scanner/ package
        ↓
Binance USDⓈ-M public API           ← free market data, no API key
        ↓
Top-100 Universe Builder            ← dynamic, volume-ranked, per scan
        ↓
Market Data Collector (4H/1H/15M)   ← retries, failover, degraded modes
        ↓
Indicator Engine                    ← EMA 20/50/200 · RSI · ADX · ATR · RelVol · VWAP
        ↓
4H Macro  →  1H Direction  →  15M Signal Analysis
        ↓
SMC Engine                          ← swings · BOS · CHoCH · displacement · FVG · OB · sweeps
        ↓
Confluence Scoring (100 pts)        ← A+ / A / B+ … < 80 = REJECT
        ↓
Risk/Reward Validation              ← structure-based SL/TP, RR ≥ 2.5
        ↓
Signal Generator (WAITING_TRIGGER)  → Outcome Tracker (WIN/LOSS/EXPIRED/AMBIGUOUS/CANCELLED)
        ↓
JSON persistence → git commit       ← data/*.json mirrored to frontend/data
        ↓
Cloudflare Pages (free)             ← static dark dashboard, auto-refresh
```

**No paid service anywhere**: GitHub Actions (free tier) + Cloudflare Pages (free) + Binance
public market data + JSON-in-git storage. Total cost: **RM0 / USD0 per month**.

### Repository layout

```
crypto-signal-scanner/
├── scanner/                  Python signal engine (stdlib only)
│   ├── market_data.py        resilient Binance public client (failover, retries)
│   ├── universe.py           dynamic top-100 universe builder
│   ├── indicators.py         EMA · RSI · ATR · ADX · RelVol · VWAP (pure, tested)
│   ├── structure.py          swings · HH/HL/LH/LL · BOS · CHoCH · displacement · sweeps
│   ├── smc.py                Fair Value Gap (3-candle) · Order Block (deterministic)
│   ├── analysis.py           multi-timeframe analysis (4H/1H/15M snapshots)
│   ├── scoring.py            100-point confluence score + quality tiers
│   ├── risk.py               trigger/entry/SL/TP construction, RR math
│   ├── signals.py            LONG/SHORT models, hard rejections, dedupe, signal objects
│   ├── outcomes.py           chronological lifecycle engine (+1M ambiguity resolution)
│   ├── performance.py        win rate, profit factor, streaks, breakdowns
│   ├── validation.py         data-integrity + no-repaint enforcement
│   ├── persist.py            atomic JSON writes + retention
│   ├── main.py               scan orchestrator / CLI
│   ├── replay.py             historical replay/backtest mode
│   └── validate_data.py      JSON validation CLI (used by CI)
├── frontend/                 dark dashboard (HTML/CSS/vanilla JS, no frameworks)
│   ├── index.html  styles.css  app.js
│   └── data/                 ← mirror of /data served statically by Cloudflare Pages
├── config/strategy.json      ALL strategy parameters (nothing hard-coded)
├── data/                     signals.json · performance.json · system-status.json · market-snapshots.json
├── tests/                    103 unit + integration tests (offline, deterministic)
├── .github/workflows/
│   ├── scanner.yml           cron */15 scan → validate → commit → push
│   └── tests.yml             pytest on every push/PR
└── README.md
```

---

## 2. Installation

```bash
git clone <your-repo-url> crypto-signal-scanner
cd crypto-signal-scanner
pip install -r requirements.txt      # pytest only; the engine is stdlib-only
python -m pytest tests/ -q           # 103 tests must pass
```

## 3. Local execution

```bash
# full live scan (top-100 universe, writes data/ + frontend/data)
python -m scanner.main

# quick partial scan
python -m scanner.main --max-symbols 20
python -m scanner.main --symbols BTCUSDT ETHUSDT      # skip universe build
python -m scanner.main --dry-run                      # no files written

# validate the JSON files (same check CI runs)
python -m scanner.validate_data

# serve the dashboard locally
cd frontend && python -m http.server 8080
# → http://localhost:8080
```

## 4. GitHub Actions setup (the 15-minute scheduler)

1. Create a **new GitHub repository** (e.g. `crypto-signal-scanner`) and push this code:
   ```bash
   git init && git add -A && git commit -m "feat: initial signal scanner"
   git branch -M main
   git remote add origin https://github.com/<you>/crypto-signal-scanner.git
   git push -u origin main
   ```
2. Go to **Settings → Actions → General → Workflow permissions** and enable
   **Read and write permissions** (the scan workflow commits JSON updates).
3. The included `.github/workflows/scanner.yml` runs at `*/15 * * * *`
   (00/15/30/45) plus on every push and manual dispatch. It runs the engine,
   validates the JSON, and commits/pushes only when data actually changed.
4. **No secrets are needed.** Public market data only — never add API keys.

Notes:
- GitHub cron can jitter by a few minutes or skip runs under load; the engine
  records `scheduledAt` vs `executedAt` and the dashboard shows the **last
  successful scan** when a run fails.
- Binance geo-blocks some cloud IPs (HTTP 451). The client automatically
  fails over through the configured endpoint chain (canonical futures API →
  futures API mirror → official spot market-data mirror, clearly flagged as a
  degraded source in System Status). See `dataSource.failoverEndpoints` in
  `config/strategy.json`.

## 5. Cloudflare Pages deployment (free hosting)

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to Git**.
2. Select the repository and branch `main`.
3. Build settings:
   - **Framework preset:** None
   - **Build command:** *(leave empty)*
   - **Build output directory:** `frontend`
4. Save and deploy. Your dashboard is live at
   `https://<project>.pages.dev` — a free subdomain (no paid domain needed).
5. Every scan commit (which updates `frontend/data/*.json`) automatically
   triggers a redeploy — the dashboard data refreshes itself.

The frontend also fetches live prices **directly from Binance in your browser**
(CORS-enabled public ticker) every 20 s and re-reads the JSON every 60 s.
Prices are labelled `LIVE MARKET PRICE` **only** when that browser fetch
succeeds; otherwise they fall back to the scan snapshot and are labelled
accordingly.

## 6. Data model

`data/signals.json` — every signal ever generated (persistent history). Each object:

| Field | Meaning |
|---|---|
| `id, symbol, direction, timeframe` | identity (IDs are content-hashed, deterministic) |
| `quality, score, components` | tier (A+/A/B+), 0–100 confluence score and its breakdown |
| `generatedAt, signalCandleCloseTime` | when/on which closed 15M candle it was created |
| `triggerPrice, entryZone, entryPrice` | trigger (buy/sell-stop), zone, and actual fill |
| `stopLoss, takeProfit, riskReward, invalidationLevel` | structure-based levels & RR |
| `marketRegime, htf4hBias, htf1hBias` | 4H regime and both HTF biases at generation |
| `structure, liquiditySweep, bos, choch, displacement, fvg, orderBlock` | SMC context |
| `rsi, adx, atr, atrPercent, relativeVolume, vwap` | indicator snapshot |
| `status` | `WAITING_TRIGGER → TRIGGERED → WIN / LOSS / EXPIRED / AMBIGUOUS / CANCELLED` |
| `expiryAt, tradeMaxDurationMs` | 12×15m trigger expiry; 4h max hold (configurable) |
| `triggeredAt, closedAt, outcome, rMultiple` | lifecycle timestamps and R result |

`data/system-status.json` — scan telemetry (scheduled vs actual time, duration,
universe, API stats, freshness, health) + rolling logs.
`data/market-snapshots.json` — per-scan breadth snapshot (retention-capped).
`data/performance.json` — headline metrics (the dashboard also recomputes
metrics client-side for its 7D/30D/90D/ALL filters).

**Immutability:** entry, trigger, SL, TP, RR and generation fields are frozen
after creation (`scanner/validation.py` enforces this on every scan) — signals
never repaint. Only current price, status, outcome and timestamps may update.

## 7. Signal methodology (summary)

**Sequence (LONG; SHORT is the mirror):**

4H bullish (or acceptable bullish) **AND** 1H bullish bias **AND** 15M
liquidity sweep **AND** CHoCH/BOS confirmation **AND** bullish displacement
**AND** FVG and/or valid order block **AND** momentum confirmation (RSI band,
ADX established-or-emerging, DI aligned) **AND** volume ≥ 1.2× **AND**
volatility within band **AND** RR ≥ 2.5 → then score ≥ 80 (B+) / 85 (A) /
90 (A+) or **reject**.

**Key definitions** (fully documented in the code and the dashboard's
Methodology section):

- *Swing* — 2-bar strict fractal, usable only 2 bars after its extreme
  (**confirmation index** = the lookahead guard).
- *BOS* — close (never wick) beyond the most recent confirmed swing, with trend.
- *CHoCH* — close against the established trend (character change).
- *Liquidity sweep* — wick beyond a prior confirmed swing (excursion ≤ 1× ATR)
  with a close back inside (stop-hunt rejection proxy).
- *Displacement* — body ≥ 1.5× ATR.
- *FVG* — 3-candle imbalance, gap ≥ 0.1× ATR, known only at the 3rd close.
- *Order block* — last opposite-colour candle before a structure-breaking
  displacement; zone = that candle's range. *(All are price-action proxies —
  no claim of seeing real institutional orders.)*
- *Trigger* — confirmation-candle extreme ± 0.1× ATR (stop-order style), so
  the system never uses "current price" as the entry.
- *Stop* — swept invalidation structure ± 0.5× ATR buffer; rejected if > 3.5×
  ATR ("abnormally large").
- *Take profit* — the nearest **real** swing-liquidity level with RR ≥ 2.5,
  preferring ≥ 3R when available; if structure can't fund the RR, no signal.

**Hard rejections** (regardless of score): HTF conflict · RR < 2.5 · ranging
4H · no momentum (ADX < 15 with no emerging-trend evidence) · thin volume ·
oversized stop · entry pressed against major opposing structure · late entry
after excessive displacement · stale/missing data · duplicate active
same-direction signal · setup-hash cooldown · any integrity failure.

**If there is no valid setup, zero signals are generated.** Nothing is forced.

## 8. Scoring methodology (100 points)

| Component | Points | Evidence |
|---|---|---|
| HTF alignment | 20 | 4H 10 + 1H 10 (strong/aligned/neutral scaling) |
| Market structure | 20 | CHoCH/BOS 10 (+5 extra BOS) + displacement 5 |
| Liquidity / SMC | 20 | sweep 10 + FVG 5 + order block 5 |
| Momentum | 15 | RSI band 8 + ADX/DI 7 (emerging-trend credit) |
| Volume | 10 | linear on relative volume 1.0×→2.0× |
| Volatility | 5 | ATR% within band |
| Risk/Reward | 10 | 5 at RR 2.5 → 10 at RR ≥ 3.0 |

Tiers: **A+ ≥ 90 · A ≥ 85 · B+ ≥ 80 · below 80 rejected** (all configurable).

## 9. Win-rate & performance definitions

- **Win rate = wins ÷ (wins + losses)** — `WAITING`, `EXPIRED`, `AMBIGUOUS`,
  `CANCELLED` are never in the denominator; sample size always displayed.
- **Profit factor** = ΣR(won) ÷ |ΣR(lost)| (a stop-out is −1R by definition).
- **AMBIGUOUS** — TP and SL touched within one 15M candle and 1-minute data
  cannot determine the order. Never counted as a win.
- Breakdowns by direction, quality, regime, score band, symbol and date range.
- No "94% probability"-style claims are ever made — the score is not a
  probability and is never presented as one.

## 10. Backtesting / replay

```bash
python -m scanner.replay --symbols BTCUSDT ETHUSDT --candles 800
python -m scanner.replay --symbols BTCUSDT --determinism-check   # identical input ⇒ identical output
```

Replay walks history candle-by-candle and feeds the engine **only the candles
that had closed at each step** (causal slicing) — signals appear exactly as
they would have live. The determinism check proves byte-identical output.
Reports land in `data/replay-report.json` (git-ignored).

Expect **few signals** — the gate is intentionally strict. On live top-10
pairs, roughly one setup per ~200 symbol-days clears all filters; across the
full 100-pair universe that typically means a handful of signals per day.
Tune `config/strategy.json` to loosen/tighten deliberately.

## 11. Strategy configuration (`config/strategy.json`)

Everything is configurable — indicators, structure detection, model bands,
risk parameters, scoring weights/tiers, lifecycle durations, dedupe/cooldowns,
universe filters, data-source failover chain, retention. Highlights:

```jsonc
"indicators": { "emaFast": 20, "emaMid": 50, "emaSlow": 200, "rsiPeriod": 14, ... },
"signalModel": { "minAdx15m": 15, "minRelVolume": 1.20, "rsiLongMin": 45, ... },
"risk": { "minRr": 2.5, "preferredRr": 3.0, "stopBufferAtrMultiple": 0.5, ... },
"scoring": { "minScore": 80, "aPlusThreshold": 90, "aThreshold": 85, "bPlusThreshold": 80 },
"lifecycle": { "triggerExpiryCandles": 12, "tradeExpiryCandles": 16 },
"dedupe":   { "symbolCooldownMinutes": 240 }
```

These defaults are **starting parameters validated by the test-suite and
replay — not optimised, not profitable-by-claim**. Backtest before changing
live behaviour.

## 12. Testing

```bash
python -m pytest tests/ -v          # 103 tests, fully offline
```

Coverage: EMA/RSI/ATR/ADX/RelVol/VWAP (hand-computed vectors) · swing
detection · HH/HL/LH/LL · equal levels · BOS/CHoCH (incl. wick-break
rejection) · displacement · liquidity sweeps (incl. genuine-breakout
exclusion) · FVG · order blocks · scoring & tiers · RR math & target
selection · LONG/SHORT end-to-end models · every hard-rejection rule ·
dedupe/cooldowns · full lifecycle (trigger, gap fill, WIN/LOSS, EXPIRED,
AMBIGUOUS + 1M resolution, max-hold cancel) · immutability/no-repaint ·
win-rate/profit-factor/streaks · validation · offline integration (full scan
pipeline, failure policy, no-lookahead slice stability, replay determinism).

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `HTTP 451 geo-restricted` | Binance blocks the host. The failover chain handles it automatically; if all endpoints fail, host the runner elsewhere or add an allowed mirror to `dataSource.failoverEndpoints`. |
| Workflow doesn't commit | Enable **Settings → Actions → General → Workflow permissions → Read and write**. |
| "No data changes — skipping commit" | Normal: nothing changed, no empty commits are created. |
| Dashboard shows `price: scan snapshot` | Browser couldn't reach the Binance ticker (network/region). JSON data is unaffected. |
| 0 signals for long stretches | By design. Loosen `scoring.minScore`, `risk.minRr`, or `signalModel` bands deliberately, then replay-test. |
| Scan marked DEGRADED | Some symbols failed or data arrived late; previous valid data retained and warning shown. |

## 14. Known limitations

- **15-minute cadence**: GitHub cron jitter (minutes) means scans are
  near-quarter-hour, not exact; outcome evaluation uses closed candles, so
  intracandle path order is resolved only via 1M data (else AMBIGUOUS).
- **Single data source** (Binance public). Outages degrade the system; no
  simulated data is ever substituted.
- **Spot fallback fidelity**: if both futures endpoints are blocked, the
  official spot market-data mirror is used and flagged — order-flow differs
  slightly from perps.
- **Statistical sample**: high-quality setups are rare; weeks of data may be
  needed before win-rate figures carry meaning (sample sizes are always shown).
- **Not optimised**: default parameters are sensible starting points, not
  curve-fitted edge. No profitability claim is made or implied.
- Ambiguous candles resolved with 1M data consume extra API requests
  (configurable via `lifecycle.resolveAmbiguousWith1m`).

---

## License & disclaimer

MIT License. **This software is for market analysis and education only.** It
does not execute trades, does not provide investment advice, and does not
guarantee any outcome. Cryptocurrency trading involves substantial risk of
loss. You are solely responsible for any trading decisions you make.
frontend/preview.html is committed (auto-generated each scan)
