"""SMC / institutional price-action proxies.

These are *price-action proxies*. The system does not pretend to identify
actual institutional orders; every concept below is a deterministic,
documented candle-pattern definition.

Fair Value Gap (3-candle imbalance model)
    bullish FVG at candle i (evaluated on the completed triple i-2, i-1, i):
        low[i]  > high[i-2]  -> gap zone [high[i-2], low[i]]
    bearish FVG:
        high[i] < low[i-2]   -> gap zone [high[i], low[i-2]]
    The gap must be >= fvgMinGapAtrMultiple * ATR[i] (filters micro-gaps).
    Midpoint = "consequent encroachment" (ICT). The FVG is known only after
    candle i closes -> no lookahead.

Order Block (deterministic definition)
    bullish OB: the most recent *bearish* candle (close < open) within
        obLookback bars immediately before a bullish displacement candle
        whose move also broke structure (a BOS_up event at or within
        obValidationBars after the displacement). Zone = that candle's
        [low, high].
    bearish OB: mirror image before a bearish displacement + BOS_down.
    The OB is usable from the close of the displacement candle. If several
    displacement candles share the same origin, the most recent candle wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .structure import Displacement, StructureEvent


@dataclass
class FVG:
    index: int          # third candle of the triple (gap becomes known at its close)
    direction: str      # 'bullish' | 'bearish'
    bottom: float
    top: float
    size: float
    midpoint: float

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def find_fvgs(opens: list[float], highs: list[float], lows: list[float],
              closes: list[float], atr_series: list[Optional[float]],
              min_gap_atr: float = 0.10) -> list[FVG]:
    out: list[FVG] = []
    for i in range(2, len(closes)):
        atr_v = atr_series[i]
        if atr_v is None or atr_v <= 0:
            continue
        # bullish imbalance: candle-3 low leaves a gap above candle-1 high
        if lows[i] > highs[i - 2]:
            gap = lows[i] - highs[i - 2]
            if gap >= min_gap_atr * atr_v:
                out.append(FVG(i, "bullish", highs[i - 2], lows[i], gap, (highs[i - 2] + lows[i]) / 2.0))
        # bearish imbalance: candle-3 high leaves a gap below candle-1 low
        if highs[i] < lows[i - 2]:
            gap = lows[i - 2] - highs[i]
            if gap >= min_gap_atr * atr_v:
                out.append(FVG(i, "bearish", highs[i], lows[i - 2], gap, (highs[i] + lows[i - 2]) / 2.0))
    return out


@dataclass
class OrderBlock:
    index: int          # the origin candle
    direction: str      # 'bullish' | 'bearish' (direction it supports)
    bottom: float
    top: float
    displacementIndex: int
    bosIndex: int

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


def find_order_blocks(opens: list[float], highs: list[float], lows: list[float],
                      closes: list[float],
                      displacements: list[Displacement],
                      structure_events: list[StructureEvent],
                      lookback: int = 10,
                      bos_within_bars: int = 3) -> list[OrderBlock]:
    out: list[OrderBlock] = []
    bos_up_idx = [e.index for e in structure_events if e.type in ("BOS_up", "CHoCH_up")]
    bos_down_idx = [e.index for e in structure_events if e.type in ("BOS_down", "CHoCH_down")]

    for d in displacements:
        # the displacement must be validated by a structure break at/after it
        validated = any(d.index <= b <= d.index + bos_within_bars for b in (bos_up_idx if d.direction == "bullish" else bos_down_idx))
        if not validated:
            continue
        want_bearish = d.direction == "bullish"
        for j in range(d.index - 1, max(-1, d.index - 1 - lookback), -1):
            is_red = closes[j] < opens[j]
            if is_red == want_bearish and closes[j] != opens[j]:
                out.append(OrderBlock(j, d.direction, lows[j], highs[j], d.index, min(
                    (b for b in (bos_up_idx if want_bearish else bos_down_idx) if b >= d.index), default=d.index)))
                break
    # dedupe by origin index, newest first
    seen: set[int] = set()
    unique: list[OrderBlock] = []
    for ob in sorted(out, key=lambda o: -o.index):
        if ob.index not in seen:
            seen.add(ob.index)
            unique.append(ob)
    return unique
