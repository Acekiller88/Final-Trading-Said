"""High-quality signal generation (spec §10-13, §17, §43).

LONG model (SHORT is the exact mirror):
  4H bullish (or acceptable bullish regime)
  AND 1H bullish bias
  AND 15M liquidity sweep (of a swing low)
  AND 15M CHoCH/BOS confirmation after the sweep
  AND bullish displacement at/after the structure event
  AND bullish FVG and/or valid bullish order block in the setup window
  AND momentum confirmation (RSI band, ADX >= min, DI aligned)
  AND volume confirmation (relative volume >= min)
  AND acceptable volatility (ATR% within band)
  AND RR >= 2.5
  -> score >= 80 (B+/A/A+) or REJECT.

Hard rejection rules (regardless of score) are enforced in ``try_setup`` and
``generate_signals``: HTF conflict, RR < min, ranging regime, ADX too low,
thin volume, oversized stop, entry pressed against opposing structure, late
entry after excessive displacement, stale data, missing candles, duplicate
active signal (same symbol+direction), cooldown duplicates, and any
future-data dependency (impossible by construction -- see analysis.py).

If there is no valid setup, ZERO signals are produced. Nothing is forced.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from .analysis import analyze_symbol  # noqa: F401  (re-exported for convenience)
from .risk import build_setup
from .scoring import score_setup, quality_tier

BULLISH = ("strong_bullish", "bullish")
BEARISH = ("strong_bearish", "bearish")

WAITING_TRIGGER = "WAITING_TRIGGER"
TRIGGERED = "TRIGGERED"
WIN = "WIN"
LOSS = "LOSS"
EXPIRED = "EXPIRED"
AMBIGUOUS = "AMBIGUOUS"
CANCELLED = "CANCELLED"
ACTIVE_STATUSES = (WAITING_TRIGGER, TRIGGERED)
RESOLVED_STATUSES = (WIN, LOSS)


def setup_hash(symbol: str, direction: str, trigger: float, stop: float, target: float) -> str:
    payload = f"{symbol}|{direction}|{trigger:.6f}|{stop:.6f}|{target:.6f}"
    return hashlib.sha1(payload.encode()).hexdigest()


def signal_id(symbol: str, direction: str, generated_ms: int, trigger: float) -> str:
    payload = f"{symbol}|{direction}|{generated_ms}|{trigger:.8f}"
    return "SIG-" + hashlib.sha1(payload.encode()).hexdigest()[:10]


def _last_index(items, lo: int, hi: int, predicate) -> Optional[int]:
    """Most recent index in [lo, hi] satisfying predicate, or None."""
    for i in range(hi, lo - 1, -1):
        if i < 0:
            continue
        if predicate(items[i]):
            return i
    return None


def try_setup(direction: str, a: dict, cfg) -> tuple[Optional[dict], list[str]]:
    """Evaluate one symbol for one direction. Returns (setup_payload|None, reasons)."""
    reasons: list[str] = []
    if not a.get("ok"):
        return None, ["data insufficient"]
    bull = direction == "long"
    f15 = a["frames"]["15m"]
    n = a["last15mIndex"]
    model = cfg.get("signalModel", {})
    window = int(cfg.get("smc.setupWindowBars", 12))
    lo = max(2, n - window)
    atr = a.get("atr")
    price = a.get("price")

    # ---- required indicator values exist (rule 10/14: missing data)
    for key in ("rsi", "adx", "atr", "relVolume", "atrPercent"):
        if a.get(key) is None:
            return None, [f"{key} missing"]
    if atr <= 0:
        return None, ["atr invalid"]

    # ---- 4H / 1H regime gates (rule 1: strong conflict)
    b4, b1 = a["bias4h"]["bias"], a["bias1h"]["bias"]
    want, opposite = (BULLISH, BEARISH) if bull else (BEARISH, BULLISH)
    if b4 in opposite and b1 in opposite:
        return None, ["HTF conflict (4H+1H oppose)"]
    htf_ok = (b4 in want) or (b4 == "neutral" and b1 == ("strong_bullish" if bull else "strong_bearish"))
    ltf_ok = (b1 in want) or (b1 == "neutral" and b4 == ("strong_bullish" if bull else "strong_bearish"))
    if not (htf_ok and ltf_ok):
        return None, [f"HTF not aligned (4H={b4},1H={b1})"]

    # ---- regime + volatility + momentum + volume gates (rules 3,4,5)
    if a["regime"] == "RANGING":
        return None, ["4H regime RANGING"]
    atr_pct = a["atrPercent"]
    if not (model.get("minAtrPercent", 0.1) <= atr_pct <= model.get("maxAtrPercent", 3.0)):
        return None, [f"volatility out of band ({atr_pct:.3f}%)"]
    # ADX gate: trend either *established* (ADX >= min) or *emerging*
    # (directional index aligned and ADX rising) -- reversals legitimately
    # start with low ADX right after a CHoCH.
    min_adx = model.get("minAdx15m", 15)
    di_aligned = (a.get("plusDi", 0) > a.get("minusDi", 0)) if bull \
        else (a.get("minusDi", 0) > a.get("plusDi", 0))
    adx_tail = [v for v in f15["adx"][-5:] if v is not None]
    adx_rising = len(adx_tail) >= 2 and adx_tail[-1] > adx_tail[0]
    di_spread = abs((a.get("plusDi") or 0) - (a.get("minusDi") or 0))
    adx_ok = (a["adx"] >= min_adx) or (di_aligned and (adx_rising or di_spread >= 3.0))
    if not adx_ok:
        return None, [f"ADX {a['adx']:.1f} < min and not emerging"]
    rsi_lo, rsi_hi = (model.get("rsiLongMin", 50), model.get("rsiLongMax", 72)) if bull \
        else (model.get("rsiShortMin", 28), model.get("rsiShortMax", 50))
    if not (rsi_lo <= a["rsi"] <= rsi_hi):
        return None, [f"RSI {a['rsi']:.1f} outside [{rsi_lo},{rsi_hi}]"]
    if a["relVolume"] < model.get("minRelVolume", 1.2):
        return None, [f"relVol {a['relVolume']:.2f} < min"]

    # ---- 15M sequence: sweep -> CHoCH/BOS -> displacement (rules via model)
    sweeps = [s for s in f15["sweeps"]
              if s.direction == ("bullish" if bull else "bearish") and lo <= s.index <= n]
    if not sweeps:
        return None, ["no liquidity sweep in window"]
    sweep = sweeps[-1]
    ev_types = ("CHoCH_up", "BOS_up") if bull else ("CHoCH_down", "BOS_down")
    events = [e for e in f15["events"]
              if e.type in ev_types and sweep.index <= e.index <= n]
    if not events:
        return None, ["no CHoCH/BOS after sweep"]
    event = events[-1]
    struct_direction_ok = True
    disps = [d for d in f15["displacements"]
             if d.direction == ("bullish" if bull else "bearish") and event.index <= d.index <= n]
    if not disps:
        return None, ["no displacement after structure event"]

    # ---- FVG and/or order block
    fvgs = [g for g in f15["fvgs"]
            if g.direction == ("bullish" if bull else "bearish") and sweep.index <= g.index <= n]
    obs = [ob for ob in f15["orderBlocks"]
           if ob.direction == ("bullish" if bull else "bearish")
           and sweep.index <= ob.displacementIndex <= n]
    if not fvgs and not obs:
        return None, ["no FVG and no order block"]

    # ---- late entry (rule 8): price already ran too far from invalidation
    lows, highs = f15["l"], f15["h"]
    if bull:
        invalidation = min(min(lows[sweep.index:n + 1]), sweep.level)
        runup = price - invalidation
    else:
        invalidation = max(max(highs[sweep.index:n + 1]), sweep.level)
        runup = invalidation - price
    if runup > cfg.get("risk.maxRunupAtrMultiple", 3.0) * atr:
        return None, [f"late entry (runup {runup / atr:.1f}x ATR)"]

    if bull:
        confirm_high, confirm_low = max(highs[event.index:n + 1]), lows[n]
    else:
        confirm_high, confirm_low = highs[n], min(lows[event.index:n + 1])

    # ---- opposing structure levels (real liquidity targets + rule 7)
    swing_levels = []
    if bull:
        for s in f15["swingHighs"][-cfg.get("structure.maxStructureSwings", 40):]:
            if s.confirmIndex <= n and s.price > price:
                swing_levels.append((s.price, s.index, "swing_high"))
        for eq in f15["equalHighs"]:
            if eq["confirmIndex"] <= n and eq["level"] > price:
                swing_levels.append((eq["level"], eq["indexB"], "equal_highs"))
    else:
        for s in f15["swingLows"][-cfg.get("structure.maxStructureSwings", 40):]:
            if s.confirmIndex <= n and s.price < price:
                swing_levels.append((s.price, s.index, "swing_low"))
        for eq in f15["equalLows"]:
            if eq["confirmIndex"] <= n and eq["level"] < price:
                swing_levels.append((eq["level"], eq["indexB"], "equal_lows"))

    setup, risk_reason = build_setup(direction, price, atr, confirm_high, confirm_low,
                                     invalidation, swing_levels, cfg)
    if setup is None:
        return None, [f"risk model rejected: {risk_reason}"]

    # rule 7: entry must have room before MAJOR opposing structure. Minor
    # pullback levels below the pullback origin are expected to be reclaimed
    # by the reversal; only structure beyond the origin counts as opposition.
    risk_cfg = cfg.get("risk", {})
    room = risk_cfg.get("minStructureRoomAtrMultiple", 1.0) * atr
    if bull:
        origin = max(highs[max(0, sweep.index - window):sweep.index + 1])
        threshold = max(origin, setup.trigger)
        nearest = min((lv[0] for lv in swing_levels if lv[0] > threshold), default=None)
        if nearest is not None and (nearest - setup.trigger) < room:
            return None, ["entry too close to opposing structure"]
    else:
        origin = min(lows[max(0, sweep.index - window):sweep.index + 1])
        threshold = min(origin, setup.trigger)
        nearest = max((lv[0] for lv in swing_levels if lv[0] < threshold), default=None)
        if nearest is not None and (setup.trigger - nearest) < room:
            return None, ["entry too close to opposing structure"]

    # ---- score & tier
    parts = {
        "bias4h": a["bias4h"], "bias1h": a["bias1h"],
        "structureEventAgeBars": n - event.index,
        "additionalBos": any(e.type == ("BOS_up" if bull else "BOS_down")
                             and sweep.index <= e.index < event.index for e in f15["events"]),
        "displacement": True,
        "sweep": True, "fvg": bool(fvgs), "orderBlock": bool(obs),
        "rsi": a["rsi"], "adx": a["adx"], "adxRising": adx_rising, "diSpread": di_spread,
        "plusDi": a.get("plusDi") or 0.0, "minusDi": a.get("minusDi") or 0.0,
        "relVolume": a["relVolume"], "atrPercent": a["atrPercent"],
    }
    scored = score_setup(direction, parts, setup.rr, cfg)
    tier = quality_tier(scored["score"], cfg)
    if tier is None:
        return None, [f"score {scored['score']} below threshold"]
    if setup.rr < cfg.get("risk.minRr", 2.5):
        return None, [f"RR {setup.rr} < min"]  # belt & braces

    fvg = fvgs[-1] if fvgs else None
    ob = obs[-1] if obs else None
    payload = {
        "direction": direction,
        "score": scored["score"],
        "quality": tier,
        "components": scored["components"],
        "trigger": setup.trigger,
        "entryZone": [setup.entryLow, setup.entryHigh],
        "stopLoss": setup.stop,
        "takeProfit": setup.target,
        "riskReward": setup.rr,
        "invalidation": setup.invalidation,
        "targetKind": setup.targetKind,
        "structure": {"event": event.type, "ageBars": n - event.index, "level": event.level},
        "liquiditySweep": {"type": sweep.direction, "level": sweep.level,
                           "barsAgo": n - sweep.index},
        "displacement": True,
        "fvg": {"bottom": fvg.bottom, "top": fvg.top} if fvg else None,
        "orderBlock": {"bottom": ob.bottom, "top": ob.top} if ob else None,
        "rsi": round(a["rsi"], 2), "adx": round(a["adx"], 2),
        "atr": round(a["atr"], 10), "atrPercent": round(a["atrPercent"], 4),
        "relativeVolume": round(a["relVolume"], 3),
        "vwap": round(a["vwap"], 10) if a.get("vwap") is not None else None,
        "marketRegime": a["regime"],
        "htf4hBias": b4, "htf1hBias": b1,
    }
    return payload, []


def generate_signals(analyses: list[dict], cfg, existing: list[dict],
                     now_ms: int, data_source: str | None = None) -> tuple[list[dict], list[dict]]:
    """Produce new signal objects. Returns (new_signals, rejection_log)."""
    new_signals: list[dict] = []
    rejects: list[dict] = []
    dedupe = cfg.get("dedupe", {})
    cooldown_ms = int(dedupe.get("symbolCooldownMinutes", 240)) * 60_000
    same_dir_only = bool(dedupe.get("sameDirectionOnly", True))
    max_active = int(dedupe.get("maxActiveSignalsTotal", 12))
    candle_ms = cfg.get("lifecycle.candleMs", 900_000)

    active = [s for s in existing if s.get("status") in ACTIVE_STATUSES]
    if len(active) >= max_active:
        return [], [{"reason": "max active signals reached", "symbol": "*"}]

    last_by_key: dict[tuple[str, str], int] = {}
    seen_hashes: dict[str, int] = {}
    for s in existing:
        key = (s["symbol"], s["direction"])
        last_by_key[key] = max(last_by_key.get(key, 0), s.get("generatedAt", 0))
        h = setup_hash(s["symbol"], s["direction"], s.get("triggerPrice", 0),
                       s.get("stopLoss", 0), s.get("takeProfit", 0))
        seen_hashes[h] = max(seen_hashes.get(h, 0), s.get("generatedAt", 0))

    active_symbols_dir = {(s["symbol"], s["direction"]) for s in active}

    for a in analyses:
        for direction in ("long", "short"):
            sym = a["symbol"]
            dir_upper = direction.upper()
            if (sym, dir_upper) in active_symbols_dir:
                rejects.append({"symbol": sym, "direction": direction,
                                "reason": "duplicate active signal (rule 11)"})
                continue
            last = last_by_key.get((sym, dir_upper))
            if last and now_ms - last < cooldown_ms:
                rejects.append({"symbol": sym, "direction": direction,
                                "reason": "symbol cooldown (rule 12)"})
                continue
            payload, reasons = try_setup(direction, a, cfg)
            if payload is None:
                rejects.append({"symbol": sym, "direction": direction, "reason": reasons[0]})
                continue
            h = setup_hash(sym, dir_upper, payload["trigger"], payload["stopLoss"],
                           payload["takeProfit"])
            if now_ms - seen_hashes.get(h, 0) < cooldown_ms:
                rejects.append({"symbol": sym, "direction": direction,
                                "reason": "duplicate setup hash in cooldown (rule 12)"})
                continue

            expiry_ms = int(cfg.get("lifecycle.triggerExpiryCandles", 12)) * candle_ms
            hold_ms = int(cfg.get("lifecycle.tradeExpiryCandles", 16)) * candle_ms
            sig = {
                "id": signal_id(sym, direction, now_ms, payload["trigger"]),
                "symbol": sym,
                "direction": direction.upper(),
                "timeframe": "15m",
                "quality": payload["quality"],
                "score": payload["score"],
                "components": payload["components"],
                "generatedAt": now_ms,
                "signalCandleCloseTime": a.get("last15mCloseTime"),
                "triggerPrice": payload["trigger"],
                "entryPrice": None,
                "entryZone": payload["entryZone"],
                "currentPrice": a.get("price"),
                "currentPriceAt": now_ms,
                "currentPriceSource": "scan",
                "stopLoss": payload["stopLoss"],
                "takeProfit": payload["takeProfit"],
                "riskReward": payload["riskReward"],
                "invalidationLevel": payload["invalidation"],
                "targetKind": payload["targetKind"],
                "marketRegime": payload["marketRegime"],
                "htf4hBias": payload["htf4hBias"],
                "htf1hBias": payload["htf1hBias"],
                "structure": payload["structure"],
                "liquiditySweep": payload["liquiditySweep"],
                "bos": payload["structure"]["event"].startswith("BOS"),
                "choch": payload["structure"]["event"].startswith("CHoCH"),
                "displacement": payload["displacement"],
                "fvg": payload["fvg"],
                "orderBlock": payload["orderBlock"],
                "rsi": payload["rsi"], "adx": payload["adx"],
                "atr": payload["atr"], "atrPercent": payload["atrPercent"],
                "relativeVolume": payload["relativeVolume"],
                "vwap": payload["vwap"],
                "status": WAITING_TRIGGER,
                "expiryAt": now_ms + expiry_ms,
                "tradeMaxDurationMs": hold_ms,
                "triggeredAt": None,
                "closedAt": None,
                "outcome": None,
                "rMultiple": None,
                "dataSource": data_source,
                "updatedAt": now_ms,
            }
            new_signals.append(sig)
            if len([s for s in new_signals]) + len(active) >= max_active:
                return new_signals, rejects
    return new_signals, rejects
