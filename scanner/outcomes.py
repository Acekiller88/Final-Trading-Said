"""Outcome engine -- chronological signal lifecycle evaluation (spec §18-20).

Walking rule (no improper future-data use): for each signal we walk 15M
candles strictly AFTER the signal candle, in order:

WAITING_TRIGGER
    LONG : candle.high >= trigger -> TRIGGERED (fill at trigger)
    SHORT: candle.low  <= trigger -> TRIGGERED
    If expiryAt passes first -> EXPIRED.

TRIGGERED
    LONG : candle.high >= TP -> WIN ; candle.low <= SL -> LOSS
    SHORT: candle.low  <= TP -> WIN ; candle.high >= SL -> LOSS
    If both TP and SL are touched inside ONE candle -> AMBIGUOUS unless
    1-minute data can determine which level traded first (config
    lifecycle.resolveAmbiguousWith1m). Ambiguous is never counted as a win.
    Holding longer than tradeMaxDurationMs -> CANCELLED (max holding time).

Immutability: only currentPrice/currentPriceAt, status, triggeredAt,
closedAt, outcome, rMultiple and updatedAt are ever written after creation.
Entry / trigger / SL / TP are never modified (no repainting).
"""
from __future__ import annotations

from typing import Optional

from .market_data import Candle, MarketDataClient, MarketDataError

from .signals import (WAITING_TRIGGER, TRIGGERED, WIN, LOSS, EXPIRED,
                      AMBIGUOUS, CANCELLED, ACTIVE_STATUSES)


def _candles_after(candles: list[Candle], close_time_ms: int) -> list[Candle]:
    return [c for c in candles if c.closeTime > close_time_ms]


def _resolve_with_1m(client: Optional[MarketDataClient], symbol: str, candle: Candle,
                     sl: float, tp: float, direction: str, allow: bool) -> Optional[str]:
    if not allow or client is None:
        return None
    try:
        rows = client.klines_since(symbol, "1m", candle.openTime)
    except MarketDataError:
        return None
    minute = [c for c in rows if candle.openTime <= c.openTime <= candle.closeTime]
    for m in minute:  # chronological: first level touched decides
        if direction == "LONG":
            if m.low <= sl:
                return LOSS
            if m.high >= tp:
                return WIN
        else:
            if m.high >= sl:
                return LOSS
            if m.low <= tp:
                return WIN
    return None


def update_signal(sig: dict, candles: list[Candle], now_ms: int,
                  client: Optional[MarketDataClient] = None,
                  allow_1m: bool = True) -> dict:
    """Advance one signal through time. Returns the (possibly) updated signal."""
    if sig["status"] not in ACTIVE_STATUSES:
        # refresh current price from the latest candle if provided
        if candles:
            last = candles[-1]
            sig["currentPrice"] = last.close
            sig["currentPriceAt"] = last.closeTime
        return sig

    d = sig["direction"]
    trigger, sl, tp = sig["triggerPrice"], sig["stopLoss"], sig["takeProfit"]
    is_long = d == "LONG"
    sig_open = sig.get("signalCandleCloseTime") or sig["generatedAt"]
    upcoming = _candles_after(candles, sig_open)

    for c in upcoming:
        candle_close = c.closeTime
        if candle_close > now_ms:
            break  # candle not closed yet: cannot be used (no lookahead)

        if sig["status"] == WAITING_TRIGGER:
            if candle_close > sig["expiryAt"] and not (c.high >= trigger if is_long else c.low <= trigger):
                sig["status"] = EXPIRED
                sig["outcome"] = "expired_untriggered"
                sig["closedAt"] = min(sig["expiryAt"], c.closeTime)
                sig["updatedAt"] = now_ms
                break
            hit_trigger = c.high >= trigger if is_long else c.low <= trigger
            if not hit_trigger:
                continue
            sig["status"] = TRIGGERED
            sig["triggeredAt"] = c.openTime
            # gap handling: if the candle opens beyond the trigger, the fill
            # happens at the open, not at the trigger (slippage-realistic)
            if (is_long and c.open > trigger) or (not is_long and c.open < trigger):
                sig["entryPrice"] = c.open
            else:
                sig["entryPrice"] = trigger
            sig["updatedAt"] = now_ms
            # fall through: same candle may already resolve the trade
            tp_hit = c.high >= tp if is_long else c.low <= tp
            sl_hit = c.low <= sl if is_long else c.high >= sl
            if tp_hit and sl_hit:
                resolved = _resolve_with_1m(client, sig["symbol"], c, sl, tp, d, allow_1m)
                if resolved == WIN:
                    _close(sig, WIN, tp, c, now_ms)
                elif resolved == LOSS:
                    _close(sig, LOSS, sl, c, now_ms)
                else:
                    sig["status"] = AMBIGUOUS
                    sig["outcome"] = "tp_and_sl_in_same_candle"
                    sig["closedAt"] = c.closeTime
                    sig["updatedAt"] = now_ms
                break
            if tp_hit:
                _close(sig, WIN, tp, c, now_ms)
                break
            if sl_hit:
                _close(sig, LOSS, sl, c, now_ms)
                break
            continue

        if sig["status"] == TRIGGERED:
            tp_hit = c.high >= tp if is_long else c.low <= tp
            sl_hit = c.low <= sl if is_long else c.high >= sl
            if tp_hit and sl_hit:
                resolved = _resolve_with_1m(client, sig["symbol"], c, sl, tp, d, allow_1m)
                if resolved == WIN:
                    _close(sig, WIN, tp, c, now_ms)
                elif resolved == LOSS:
                    _close(sig, LOSS, sl, c, now_ms)
                else:
                    sig["status"] = AMBIGUOUS
                    sig["outcome"] = "tp_and_sl_in_same_candle"
                    sig["closedAt"] = c.closeTime
                    sig["updatedAt"] = now_ms
                break
            if tp_hit:
                _close(sig, WIN, tp, c, now_ms)
                break
            if sl_hit:
                _close(sig, LOSS, sl, c, now_ms)
                break
            if c.closeTime - sig["triggeredAt"] > sig.get("tradeMaxDurationMs", 4 * 3_600_000):
                sig["status"] = CANCELLED
                sig["outcome"] = "max_holding_duration_exceeded"
                sig["closedAt"] = c.closeTime
                sig["rMultiple"] = round((c.close - sig["entryPrice"]) /
                                         abs(sig["entryPrice"] - sl) *
                                         (1 if is_long else -1), 4)
                sig["updatedAt"] = now_ms
                break

    # last closed candle refreshes current price (allowed mutable field)
    closed = [c for c in upcoming if c.closeTime <= now_ms] or candles
    if closed:
        last = closed[-1]
        sig["currentPrice"] = last.close
        sig["currentPriceAt"] = last.closeTime
        sig["currentPriceSource"] = "scan"
    sig["updatedAt"] = now_ms
    return sig


def _close(sig: dict, status: str, level: float, candle: Candle, now_ms: int) -> None:
    sig["status"] = status
    sig["closedAt"] = candle.closeTime
    sig["updatedAt"] = now_ms
    sig["outcome"] = "tp_hit" if status == WIN else "sl_hit"
    entry = sig["entryPrice"] or sig["triggerPrice"]
    risk = abs(entry - sig["stopLoss"])
    if status == WIN:
        sig["rMultiple"] = round(abs(level - entry) / risk, 4) if risk > 0 else None
    else:
        sig["rMultiple"] = -1.0


def update_outcomes(signals: list[dict], candle_map: dict[str, list[Candle]],
                    now_ms: int, client: Optional[MarketDataClient] = None,
                    allow_1m: bool = True) -> list[dict]:
    """Update every active signal; fetch missing candles via client when available."""
    updated: list[dict] = []
    for sig in signals:
        candles = candle_map.get(sig["symbol"])
        if candles is None and client is not None and sig["status"] in ACTIVE_STATUSES:
            try:
                candles = client.klines_since(sig["symbol"], "15m", sig["generatedAt"] - 900_000)
            except MarketDataError:
                candles = []
        if candles is None:
            candles = []
        updated.append(update_signal(sig, candles, now_ms, client, allow_1m))
    return updated
