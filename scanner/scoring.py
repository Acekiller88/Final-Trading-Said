"""100-point confluence quality scoring (spec §12).

Component weights come from config scoring.weights (default: HTF 20,
structure 20, liquidity/SMC 20, momentum 15, volume 10, volatility 5, RR 10).

Sub-allocation (documented, deterministic):
  HTF alignment   : 4H strongly aligned 10 / aligned 6 ; 1H strongly aligned
                    10 / aligned 6 / neutral 2. (conflicting -> 0 and the
                    hard-rejection rules in signals.py apply anyway)
  Market structure: fresh CHoCH/BOS within 6 bars 10 / older 6 ; additional
                    confirming BOS +5 (max 15) ; qualifying displacement +5.
  Liquidity/SMC   : liquidity sweep 10 ; bullish/bearish FVG in window +5 ;
                    valid order block +5 (cap 20).
  Momentum        : RSI inside the model band 8 (proportional otherwise) ;
                    ADX >= min & DI aligned 7.
  Volume          : relVol >= 2.0 -> 10 ; linear 0..10 between 1.0 and 2.0.
  Volatility      : ATR% inside [minAtrPercent, maxAtrPercent] -> 5 ; else 0.
  Risk/Reward     : RR >= preferred (3.0) -> 10 ; linear 5..10 between
                    minRr (2.5) and preferred; below minRr cannot happen
                    (hard reject) but scores 0.

The score is a *quality-of-confluence ranking*, NOT a probability. Tier
boundaries: A+ >= 90, A >= 85, B+ >= 80, below 80 -> rejected.
"""
from __future__ import annotations

from typing import Optional


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def score_setup(direction: str, parts: dict, rr: Optional[float], cfg: Config_like) -> dict:
    w = cfg.get("scoring.weights", {})
    risk = cfg.get("risk", {})
    model = cfg.get("signalModel", {})
    d = direction.lower()  # 'long' | 'short'
    bull = d == "long"

    # ---------------- HTF alignment (20)
    htf = 0.0
    b4 = parts.get("bias4h", {}).get("bias", "unknown")
    b1 = parts.get("bias1h", {}).get("bias", "unknown")
    want4 = ("strong_bullish", "bullish") if bull else ("strong_bearish", "bearish")
    strong4 = "strong_" + ("bullish" if bull else "bearish")
    if b4 == strong4:
        htf += 10
    elif b4 in want4:
        htf += 6
    if b1 == strong4:
        htf += 10
    elif b1 in want4:
        htf += 6
    elif b1 == "neutral":
        htf += 2
    htf = min(htf, w.get("htfAlignment", 20))

    # ---------------- market structure (20)
    struct = 0.0
    choch_age = parts.get("structureEventAgeBars")
    if choch_age is not None:
        struct += 10 if choch_age <= 6 else 6
    if parts.get("additionalBos"):
        struct += 5
    if parts.get("displacement"):
        struct += 5
    struct = min(struct, w.get("marketStructure", 20))

    # ---------------- liquidity / SMC (20)
    liq = 0.0
    if parts.get("sweep"):
        liq += 10
    if parts.get("fvg"):
        liq += 5
    if parts.get("orderBlock"):
        liq += 5
    liq = min(liq, w.get("liquiditySmc", 20))

    # ---------------- momentum (15)
    momentum = 0.0
    rsi_v = parts.get("rsi")
    band = (model.get("rsiLongMin", 50), model.get("rsiLongMax", 72)) if bull \
        else (model.get("rsiShortMin", 28), model.get("rsiShortMax", 50))
    if rsi_v is not None:
        lo, hi = band
        if lo <= rsi_v <= hi:
            momentum += 8
        else:  # proportional credit, decaying with distance from the band
            dist = (lo - rsi_v) if rsi_v < lo else (rsi_v - hi)
            momentum += 8 * _clamp(1 - dist / 15.0, 0, 1)
    adx_v = parts.get("adx")
    if adx_v is not None:
        di_ok = (parts.get("plusDi", 0) > parts.get("minusDi", 0)) if bull else \
                (parts.get("minusDi", 0) > parts.get("plusDi", 0))
        if adx_v >= model.get("minAdx15m", 15):
            momentum += 7 if di_ok else 3
        elif (parts.get("adxRising") or (parts.get("diSpread") or 0) >= 3.0) and di_ok:
            momentum += 5  # emerging trend right after structure shift
    momentum = min(momentum, w.get("momentum", 15))

    # ---------------- volume (10)
    rv = parts.get("relVolume")
    volume = 0.0
    if rv is not None:
        volume = 10.0 * _clamp((rv - 1.0) / 1.0, 0, 1)
    volume = min(volume, w.get("volume", 10))

    # ---------------- volatility (5)
    atr_pct = parts.get("atrPercent")
    volatility = 0.0
    if atr_pct is not None and model.get("minAtrPercent", 0.1) <= atr_pct <= model.get("maxAtrPercent", 3.0):
        volatility = w.get("volatility", 5)

    # ---------------- risk / reward (10)
    rr_score = 0.0
    min_rr = risk.get("minRr", 2.5)
    pref_rr = risk.get("preferredRr", 3.0)
    if rr is not None and rr >= pref_rr:
        rr_score = 10.0
    elif rr is not None and rr >= min_rr:
        rr_score = 5.0 + 5.0 * (rr - min_rr) / max(pref_rr - min_rr, 1e-9)
    rr_score = min(rr_score, w.get("riskReward", 10))

    total = round(htf + struct + liq + momentum + volume + volatility + rr_score, 1)
    return {
        "score": total,
        "components": {
            "htfAlignment": round(htf, 1),
            "marketStructure": round(struct, 1),
            "liquiditySmc": round(liq, 1),
            "momentum": round(momentum, 1),
            "volume": round(volume, 1),
            "volatility": round(volatility, 1),
            "riskReward": round(rr_score, 1),
        },
    }


def quality_tier(score: float, cfg: Config_like) -> Optional[str]:
    s = cfg.get("scoring", {})
    if score >= s.get("aPlusThreshold", 90):
        return "A+"
    if score >= s.get("aThreshold", 85):
        return "A"
    if score >= s.get("bPlusThreshold", 80):
        return "B+"
    return None


# minimal structural typing (avoids circular import with scanner.config)
class Config_like:  # pragma: no cover - typing only
    def get(self, dotted: str, default=None): ...
