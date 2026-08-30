"""Entry / trigger / stop / target model (spec §14-16).

All levels are structure-based and deterministic:

Trigger & entry zone (momentum-confirmation model)
    LONG : trigger  = high[confirmation candle] + triggerBuffer * ATR
                      (buy-stop above the displacement/CHoCH candle)
           zone     = [trigger - entryZoneAtr * ATR, trigger]
    SHORT: mirrored below the confirmation candle low.
    The signal stays WAITING_TRIGGER until real price trades at/through the
    trigger. This avoids using "current price" as the entry.

Stop loss (structure invalidation, not a fixed percentage)
    LONG : invalidation = the swept swing low (or the confirmation swing low)
           stop  = invalidation - stopBuffer * ATR   (ATR-based buffer)
    SHORT: mirrored above the swept swing high.
    Rejected if the stop distance exceeds maxStopAtrMultiple * ATR
    ("abnormally large stop" hard rule).

Take profit (real liquidity targets only -- never fabricated)
    LONG : candidate targets = confirmed swing highs & equal-high pools
           above the trigger, sorted ascending. The engine picks the NEAREST
           target whose RR >= minRr; if none reaches minRr the setup is
    rejected (the opposing structure is too close). When several targets
           qualify, a target with RR >= preferredRr is preferred.
    SHORT: mirrored.

RR = (TP - trigger) / (trigger - SL) for LONG, (trigger - TP) / (SL - trigger) for SHORT.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Setup:
    trigger: float
    entryLow: float
    entryHigh: float
    stop: float
    target: float
    invalidation: float
    rr: float
    targetKind: str
    targetIndex: Optional[int]
    notes: list


def _rr(direction: str, trigger: float, stop: float, target: float) -> Optional[float]:
    denom = (trigger - stop) if direction == "long" else (stop - trigger)
    numer = (target - trigger) if direction == "long" else (trigger - target)
    if denom <= 0 or numer <= 0:
        return None
    return round(numer / denom, 4)


def build_setup(direction: str, price: float, atr: float,
                confirm_high: float, confirm_low: float,
                invalidation: float,
                opposing_levels: list[tuple[float, int, str]],
                cfg) -> tuple[Optional[Setup], str]:
    """Build entry/trigger/SL/TP. Returns (setup|None, rejection_reason)."""
    risk = cfg.get("risk", {})
    notes: list[str] = []

    if direction == "long":
        trigger = confirm_high + risk.get("triggerBufferAtrMultiple", 0.1) * atr
        entry_low = trigger - risk.get("entryZoneAtrMultiple", 0.5) * atr
        entry_high = trigger
        stop = invalidation - risk.get("stopBufferAtrMultiple", 0.5) * atr
        candidates = sorted([lv for lv in opposing_levels if lv[0] > trigger + 1e-12],
                            key=lambda lv: lv[0])
    else:
        trigger = confirm_low - risk.get("triggerBufferAtrMultiple", 0.1) * atr
        entry_low = trigger
        entry_high = trigger + risk.get("entryZoneAtrMultiple", 0.5) * atr
        stop = invalidation + risk.get("stopBufferAtrMultiple", 0.5) * atr
        candidates = sorted([lv for lv in opposing_levels if lv[0] < trigger - 1e-12],
                            key=lambda lv: -lv[0])

    if stop <= 0 or trigger <= 0:
        return None, "non-positive levels"
    stop_dist = abs(trigger - stop)
    if stop_dist > risk.get("maxStopAtrMultiple", 3.0) * atr:
        return None, f"stop too wide ({stop_dist / atr:.2f}x ATR)"
    if stop_dist < 0.15 * atr:
        return None, "stop too tight (<0.15x ATR)"

    min_rr = risk.get("minRr", 2.5)
    pref_rr = risk.get("preferredRr", 3.0)
    chosen = None
    for level, idx, kind in candidates:
        rr = _rr(direction, trigger, stop, level)
        if rr is not None and rr >= min_rr:
            chosen = (level, idx, kind, rr)
            if rr >= pref_rr:
                break  # nearest target that also reaches preferred RR
    if chosen is None:
        return None, "no opposing structure offers >= min RR"

    target, tidx, tkind, rr = chosen
    setup = Setup(
        trigger=round(trigger, 10), entryLow=round(entry_low, 10),
        entryHigh=round(entry_high, 10), stop=round(stop, 10),
        target=round(target, 10), invalidation=round(invalidation, 10),
        rr=rr, targetKind=tkind, targetIndex=tidx, notes=notes,
    )
    # final sanity: LONG SL < entry < TP ; SHORT TP < entry < SL
    if direction == "long" and not (setup.stop < setup.entryLow and setup.target > setup.trigger):
        return None, "long level ordering invalid"
    if direction == "short" and not (setup.stop > setup.entryHigh and setup.target < setup.trigger):
        return None, "short level ordering invalid"
    return setup, ""
