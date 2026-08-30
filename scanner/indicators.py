"""Indicator engine -- pure, deterministic, independently testable functions.

Every function takes plain lists and returns a list aligned to the input,
using ``None`` during the warm-up period. All values are computed from data
at or before index ``i`` only (no lookahead by construction -- a value at
index i never touches index > i). No randomness, no fabricated data.

Conventions
-----------
* EMA    -- seeded with the SMA of the first `period` values, then
            ``ema[i] = value[i] * k + ema[i-1] * (1-k)`` with k = 2/(period+1).
* RSI    -- Wilder's smoothing (avg gain/loss updated with
            ``(prev*(p-1)+x)/p``), first averages = simple mean of the first
            `period` changes. Oversold < 30 < ... < 70 overbought.
* ATR    -- TR_i = max(high-low, |high-close[i-1]|, |low-close[i-1]|),
            Wilder-smoothed, seeded with the simple mean of the first
            `period` TRs.
* ADX    -- Wilder +DM/-DM, DI+ = 100*Smoothed(+DM)/ATR, DI- likewise,
            DX = 100*|DI+-DI-|/(DI++DI-), ADX = Wilder MA of DX.
            First valid ADX at index 2*period-1.
* Relative volume -- volume[i] / mean(volume[i-lookback : i]) (current
            candle excluded from its own baseline).
* VWAP   -- rolling window: sum(typical*v)/sum(v), typical=(h+l+c)/3.
            Contextual only; not part of entry/exit logic.
"""
from __future__ import annotations

from typing import Optional


def sma(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be > 0")
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n <= period or period <= 0:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = change if change > 0 else 0.0
        losses[i] = -change if change < 0 else 0.0
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    out = [0.0] * len(closes)
    for i, (h, l, c) in enumerate(zip(highs, lows, closes)):
        if i == 0:
            out[i] = h - l
        else:
            prev_close = closes[i - 1]
            out[i] = max(h - l, abs(h - prev_close), abs(l - prev_close))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float],
        period: int = 14) -> list[Optional[float]]:
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    tr = true_range(highs, lows, closes)
    prev = sum(tr[:period]) / period  # seed: TR[0] uses h-l (no prev close)
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx(highs: list[float], lows: list[float], closes: list[float],
        period: int = 14) -> dict[str, list[Optional[float]]]:
    """Return {'adx': [...], 'plusDi': [...], 'minusDi': [...]} (Wilder)."""
    n = len(closes)
    nones: list[Optional[float]] = [None] * n
    result = {"adx": list(nones), "plusDi": list(nones), "minusDi": list(nones)}
    if period <= 0 or n < 2 * period:
        return result

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr = true_range(highs, lows, closes)
    sm_tr = sum(tr[1:period + 1])
    sm_plus = sum(plus_dm[1:period + 1])
    sm_minus = sum(minus_dm[1:period + 1])
    plus_di: list[Optional[float]] = list(nones)
    minus_di: list[Optional[float]] = list(nones)
    dxs: list[tuple[int, float]] = []
    for i in range(period, n):
        if i > period:
            sm_tr = sm_tr - sm_tr / period + tr[i]
            sm_plus = sm_plus - sm_plus / period + plus_dm[i]
            sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        atr_v = sm_tr if sm_tr > 0 else 1e-12
        pdi = 100.0 * sm_plus / atr_v
        mdi = 100.0 * sm_minus / atr_v
        plus_di[i] = pdi
        minus_di[i] = mdi
        di_sum = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / di_sum if di_sum > 0 else 0.0
        dxs.append((i, dx))
    # ADX = Wilder smoothing of DX, first value = mean of first `period` DXs
    first_adx_idx = dxs[period - 1][0]
    seed = sum(dx for _, dx in dxs[:period]) / period
    result["adx"][first_adx_idx] = seed
    prev = seed
    for i, dx in dxs[period:]:
        prev = (prev * (period - 1) + dx) / period
        result["adx"][i] = prev
    result["plusDi"] = plus_di
    result["minusDi"] = minus_di
    return result


def relative_volume(volumes: list[float], lookback: int = 20) -> list[Optional[float]]:
    """volume[i] / mean(volume[i-lookback:i]) -- current bar excluded."""
    n = len(volumes)
    out: list[Optional[float]] = [None] * n
    if lookback <= 0:
        raise ValueError("lookback must be > 0")
    for i in range(n):
        start = max(0, i - lookback)
        window = volumes[start:i]
        if not window:
            continue
        base = sum(window) / len(window)
        if base > 0:
            out[i] = volumes[i] / base
    return out


def rolling_vwap(highs: list[float], lows: list[float], closes: list[float],
                 volumes: list[float], window: int = 48) -> list[Optional[float]]:
    """Rolling VWAP = sum(typical*vol)/sum(vol) over trailing `window` bars."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if window <= 0:
        raise ValueError("window must be > 0")
    pv = 0.0
    vv = 0.0
    queue: list[tuple[float, float]] = []
    for i in range(n):
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        pair = (typical * volumes[i], volumes[i])
        queue.append(pair)
        pv += pair[0]
        vv += pair[1]
        if len(queue) > window:
            opv, ovv = queue.pop(0)
            pv -= opv
            vv -= ovv
        if len(queue) == window and vv > 0:
            out[i] = pv / vv
    return out


def last_valid(series: list[Optional[float]]) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return None


def value_at(series: list[Optional[float]], index: int) -> Optional[float]:
    if 0 <= index < len(series):
        return series[index]
    return None
