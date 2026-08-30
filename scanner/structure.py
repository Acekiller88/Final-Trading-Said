"""Deterministic market-structure / price-action engine.

Documentation of every detection rule (spec §8). All detections are causal:
a swing detected at index ``i`` is only *usable* from its confirmation index
``i + k`` (it needs k completed bars on each side), so structure at candle N
is computed exclusively from information available at or before candle N.

Swing high (fractal, k = structure.swingLookback)
    high[i] is strictly greater than the highs of the k bars on each side
    (high[i] > high[i±j] for j = 1..k). Confirmed at candle i+k.

Swing low
    Symmetric with lows.

HH / HL / LH / LL
    A swing high is HH (higher high) when its price > previous swing high,
    otherwise LH. A swing low is HL when its price > previous swing low,
    otherwise LL.

Equal highs / equal lows
    Two consecutive same-type swings whose prices differ by <=
    equalLevelAtrTolerance * ATR(at the later swing) -- a liquidity pool proxy.

BOS (break of structure -- continuation)
    A *close* beyond the most recent confirmed opposing swing level in the
    direction of the established trend. bullish BOS: trend is up and close
    breaks above the last confirmed swing high. bearish BOS mirrored.
    Wick-only breaks do not count. A broken swing is consumed (cannot be
    broken again).

CHoCH (change of character -- trend change)
    A close beyond the most recent confirmed swing level *against* the
    established trend (e.g. trend up, close breaks below last confirmed
    swing low -> bullish CHoCH ... i.e. structure shifts bearish).
    Emits the event and flips the tracked trend.

Displacement candle
    body = |close - open| >= displacementBodyAtrMultiple * ATR[i]
    (default 1.5x). Direction = sign(close - open). Acts as the
    institutional-imbalance proxy.

Liquidity sweep (stop hunt proxy)
    Candle i takes out a prior confirmed swing level by wick but *closes
    back on the original side*:
      sweep of a swing high:  high[i] > S.high and close[i] < S.high
      sweep of a swing low:   low[i]  < S.low  and close[i] > S.low
    Constraints: the swing must be confirmed by bar i (confirmIndex <= i),
    must be at least minSwingAgeBars bars old, the wick excursion must not
    exceed sweepMaxAtrMultiple * ATR (a genuine breakout-and-acceptance is
    not a sweep), and closes must reject the level.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Swing:
    index: int          # candle index of the extreme
    confirmIndex: int   # candle index at which the swing became usable
    price: float
    kind: str           # 'high' | 'low'

    @property
    def label(self) -> str:
        return "swing_high" if self.kind == "high" else "swing_low"


def find_swing_highs(highs: list[float], k: int = 2) -> list[Swing]:
    out: list[Swing] = []
    n = len(highs)
    for i in range(k, n - k):
        ok = True
        for j in range(1, k + 1):
            if not (highs[i] > highs[i - j] and highs[i] > highs[i + j]):
                ok = False
                break
        if ok:
            out.append(Swing(i, i + k, highs[i], "high"))
    return out


def find_swing_lows(lows: list[float], k: int = 2) -> list[Swing]:
    out: list[Swing] = []
    n = len(lows)
    for i in range(k, n - k):
        ok = True
        for j in range(1, k + 1):
            if not (lows[i] < lows[i - j] and lows[i] < lows[i + j]):
                ok = False
                break
        if ok:
            out.append(Swing(i, i + k, lows[i], "low"))
    return out


def label_swings(swings: list[Swing]) -> list[dict]:
    """Label HH/LH (for highs) or HL/LL (for lows) on a same-type swing list."""
    labelled: list[dict] = []
    prev: Optional[float] = None
    for s in swings:
        if prev is None:
            relation = "first"
        elif s.kind == "high":
            relation = "HH" if s.price > prev else "LH"
        else:
            relation = "HL" if s.price > prev else "LL"
        labelled.append({"index": s.index, "confirmIndex": s.confirmIndex,
                         "price": s.price, "kind": s.kind, "relation": relation})
        prev = s.price
    return labelled


def equal_levels(swings: list[Swing], atr_series: list[Optional[float]],
                 atr_tolerance: float) -> list[dict]:
    """Consecutive same-type swings within an ATR-scaled tolerance."""
    out: list[dict] = []
    for a, b in zip(swings, swings[1:]):
        atr_v = atr_series[b.index] or 0.0
        if abs(b.price - a.price) <= atr_tolerance * max(atr_v, 1e-12):
            out.append({"kind": a.kind, "priceA": a.price, "priceB": b.price,
                        "indexA": a.index, "indexB": b.index,
                        "level": round((a.price + b.price) / 2.0, 10),
                        "confirmIndex": b.confirmIndex})
    return out


@dataclass
class StructureEvent:
    index: int          # candle whose CLOSE broke the level
    type: str           # 'BOS_up' | 'BOS_down' | 'CHoCH_up' | 'CHoCH_down'
    level: float        # broken swing price
    swingIndex: int
    trendAfter: str     # 'up' | 'down'


def detect_structure_events(closes: list[float],
                            swing_highs: list[Swing],
                            swing_lows: list[Swing],
                            max_swings: int = 40) -> list[StructureEvent]:
    """Chronological BOS / CHoCH events via a close-break state machine.

    At every candle the *most recently confirmed, not-yet-broken* swing high
    and swing low are the live levels (a level stays breakable from its
    confirmation until a close actually breaks it). A close beyond the level
    emits an event and consumes the level:
      trend already in the break direction  -> BOS (continuation)
      trend opposite to the break direction -> CHoCH (character change)
      no established trend yet              -> BOS (first break sets trend)
    Wick-only breaks never emit events. Only swings whose confirmIndex <=
    current candle exist yet (no lookahead).
    """
    events: list[StructureEvent] = []
    highs_by_confirm = sorted(swing_highs, key=lambda s: s.confirmIndex)[-max_swings:]
    lows_by_confirm = sorted(swing_lows, key=lambda s: s.confirmIndex)[-max_swings:]
    hi_iter = iter(highs_by_confirm)
    lo_iter = iter(lows_by_confirm)
    upcoming_high: Optional[Swing] = next(hi_iter, None)
    upcoming_low: Optional[Swing] = next(lo_iter, None)
    active_high: Optional[Swing] = None
    active_low: Optional[Swing] = None
    trend: Optional[str] = None

    for i, close in enumerate(closes):
        while upcoming_high is not None and upcoming_high.confirmIndex <= i:
            active_high = upcoming_high
            upcoming_high = next(hi_iter, None)
        while upcoming_low is not None and upcoming_low.confirmIndex <= i:
            active_low = upcoming_low
            upcoming_low = next(lo_iter, None)

        if active_high is not None and close > active_high.price:
            ev_type = "BOS_up" if trend in (None, "up") else "CHoCH_up"
            events.append(StructureEvent(i, ev_type, active_high.price, active_high.index, "up"))
            trend = "up"
            active_high = None
        if active_low is not None and close < active_low.price:
            ev_type = "BOS_down" if trend in (None, "down") else "CHoCH_down"
            events.append(StructureEvent(i, ev_type, active_low.price, active_low.index, "down"))
            trend = "down"
            active_low = None
    return events


@dataclass
class Displacement:
    index: int
    direction: str      # 'bullish' | 'bearish'
    body: float
    atr: float
    strength: float     # body / atr


def find_displacements(opens: list[float], closes: list[float],
                       atr_series: list[Optional[float]],
                       body_atr_multiple: float = 1.5) -> list[Displacement]:
    out: list[Displacement] = []
    for i in range(len(closes)):
        atr_v = atr_series[i]
        if atr_v is None or atr_v <= 0:
            continue
        body = abs(closes[i] - opens[i])
        if body >= body_atr_multiple * atr_v and body > 0:
            direction = "bullish" if closes[i] > opens[i] else "bearish"
            out.append(Displacement(i, direction, body, atr_v, round(body / atr_v, 4)))
    return out


@dataclass
class Sweep:
    index: int          # candle performing the sweep
    direction: str      # 'bullish' (swept a low) | 'bearish' (swept a high)
    level: float        # swept swing price
    swingIndex: int
    wick: float         # excursion beyond the level


def find_liquidity_sweeps(highs: list[float], lows: list[float], closes: list[float],
                          swing_highs: list[Swing], swing_lows: list[Swing],
                          atr_series: list[Optional[float]],
                          min_age_bars: int = 3,
                          max_exceed_atr: float = 1.0) -> list[Sweep]:
    out: list[Sweep] = []
    for s in swing_highs:
        for i in range(max(s.confirmIndex, s.index + min_age_bars), len(closes)):
            atr_v = atr_series[i] or 0.0
            wick = highs[i] - s.price
            if wick > 0 and closes[i] < s.price:
                if wick <= max_exceed_atr * max(atr_v, 1e-12):
                    out.append(Sweep(i, "bearish", s.price, s.index, round(wick, 10)))
                break  # first candle that trades beyond the swing decides
    for s in swing_lows:
        for i in range(max(s.confirmIndex, s.index + min_age_bars), len(closes)):
            atr_v = atr_series[i] or 0.0
            wick = s.price - lows[i]
            if wick > 0 and closes[i] > s.price:
                if wick <= max_exceed_atr * max(atr_v, 1e-12):
                    out.append(Sweep(i, "bullish", s.price, s.index, round(wick, 10)))
                break
    out.sort(key=lambda x: x.index)
    return out
