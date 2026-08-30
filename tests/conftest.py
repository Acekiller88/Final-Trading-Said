"""Shared test fixtures: synthetic candle factories + a fake API client.

Synthetic data is used ONLY in tests (the engine itself never fabricates
market data). The ``long_market`` fixture encodes a realistic SMC reversal:
staircase pullback -> liquidity sweep -> CHoCH + displacement + FVG ->
continuation, on a strong 4H/1H uptrend. The engine accepts it as an A+ LONG.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.market_data import Candle  # noqa: E402
from scanner.config import Config  # noqa: E402

T0 = 1_700_000_000_000
MS_15M, MS_1H, MS_4H, MS_1M = 900_000, 3_600_000, 14_400_000, 60_000


def mk(i, o, h, l, c, v, ms=MS_15M, t0=T0):
    t = t0 + i * ms
    return Candle(t, o, h, l, c, v, t + ms - 1, v * c, 100)


def row(o, c, wu=0.5, wd=0.15, v=1000.0):
    return (o, max(o, c) + (wu if c >= o else wd),
            min(o, c) - (wd if c <= o else wu), c, v)


def trend(n, start, up, dn, wu, wd, vol, period=8):
    out, p = [], start
    for i in range(n):
        c = p + (up if i % period < period // 2 else -dn)
        out.append(row(p, c, wu, wd, vol))
        p = c
    return out


def staircase_down(p, blocks, flat=0.55, fdown=0.15, drop=3.0, cat=0.5):
    rows = []
    for _ in range(blocks):
        for d in (flat, -fdown, flat, -fdown, flat):
            c = p + d
            rows.append(row(p, c, 0.5, 0.15))
            p = c
        c = p - drop
        rows.append(row(p, c, 0.5, 0.15))
        p = c
        c = p + cat
        rows.append(row(p, c, 0.5, 0.15))
        p = c
    return rows, p


def build_15m():
    """Deterministic 15M series ending in a sweep -> CHoCH/displacement -> FVG."""
    base = trend(240, 100.0, 0.95, 0.45, 0.6, 0.2, 1000.0)
    p = base[-1][3]
    base.append(row(p, p + 1.0, 0.6, 0.2)); p += 1.0
    base.append(row(p, p + 1.0, 0.6, 0.2)); p += 1.0
    top = p + 0.6
    base.append(row(p, p - 0.2, 0.6, 0.2)); p -= 0.2
    stairs, p = staircase_down(p, blocks=18)
    tail = list(stairs)
    for d in (-0.9, -0.8, +1.6, -1.0, -0.6):
        c = p + d
        tail.append(row(p, c, 0.5, 0.15))
        p = c
    s1 = p
    o, c = p, p + 0.3
    tail.append((o, c + 0.25, o - 0.7, c, 2600)); sweep_high = c + 0.25; p = c
    o, c = p, p + 2.35
    tail.append((o, c + 0.10, sweep_high + 0.25, c, 2800)); p = c
    for d in (+0.15, +0.15):
        c2 = p + d
        tail.append(row(p, c2, 0.3, 0.3, 2400))
        p = c2
    rows = base + tail
    candles = [mk(i, o, h, l, c, v) for i, (o, h, l, c, v) in enumerate(rows)]
    return candles, top, s1


def to_candles(rows, ms):
    return [mk(i, o, h, l, c, v, ms=ms) for i, (o, h, l, c, v) in enumerate(rows)]


def invert(candles, mirror=400.0):
    """Mirror all prices around `mirror` -> a valid SHORT-market equivalent."""
    out = []
    for k in candles:
        out.append(Candle(k.openTime, mirror - k.open, mirror - k.low,
                          mirror - k.high, mirror - k.close, k.volume,
                          k.closeTime, k.quoteVolume, k.trades))
    return out


def build_frames(direction="long"):
    k15, top, s1 = build_15m()
    k4 = to_candles(trend(260, 100.0, 2.7, 1.15, 1.5, 0.5, 5000.0), MS_4H)
    k1 = to_candles(trend(300, 100.0, 1.65, 0.7, 0.95, 0.3, 3000.0), MS_1H)
    frames = {"4h": k4, "1h": k1, "15m": k15}
    if direction == "short":
        frames = {tf: invert(cs) for tf, cs in frames.items()}
    return frames


def long_analysis(cfg=None):
    from scanner.analysis import analyze_symbol
    cfg = cfg or Config.load()
    frames = build_frames("long")
    now = frames["15m"][-1].closeTime + 1
    return analyze_symbol("TESTUSDT", frames, cfg, now), frames, now


def short_analysis(cfg=None):
    from scanner.analysis import analyze_symbol
    cfg = cfg or Config.load()
    frames = build_frames("short")
    now = frames["15m"][-1].closeTime + 1
    return analyze_symbol("TESTUSDT", frames, cfg, now), frames, now


class FakeClient:
    """Offline stand-in for MarketDataClient (no network)."""

    def __init__(self, frames_by_symbol, extra_1m=None):
        self.frames_by_symbol = frames_by_symbol
        self.extra_1m = extra_1m or {}
        self.stats = type("S", (), {"requests": 0, "errors": 0, "retries": 0,
                                    "rateLimitHits": 0, "lastError": None,
                                    "as_dict": lambda self: {
                                        "requests": self.requests, "errors": self.errors,
                                        "retries": self.retries, "rateLimitHits": self.rateLimitHits,
                                        "lastError": self.lastError}})()
        from scanner.market_data import MarketDataError

        class _Err(MarketDataError):
            pass
        self.error_to_raise = None

    def endpoint_info(self):
        return {"name": "fake", "market": "futures", "base": "fake://"}

    def exchange_info(self):
        if self.error_to_raise:
            raise self.error_to_raise
        return {"symbols": [
            {"symbol": s, "status": "TRADING", "quoteAsset": "USDT",
             "baseAsset": s[:-4], "contractType": "PERPETUAL",
             "onboardDate": T0 - 90 * 86_400_000}
            for s in self.frames_by_symbol]}

    def ticker_24h(self):
        return [{"symbol": s, "quoteVolume": str(10_000_000 * (i + 1))}
                for i, s in enumerate(self.frames_by_symbol)]

    def klines(self, symbol, interval, limit):
        if self.error_to_raise:
            raise self.error_to_raise
        candles = self.frames_by_symbol[symbol][interval][-limit:]
        return list(candles)

    def klines_since(self, symbol, interval, start_ms):
        if self.error_to_raise:
            raise self.error_to_raise
        if interval == "1m":
            return [c for c in self.extra_1m.get(symbol, []) if c.openTime >= start_ms]
        return [c for c in self.frames_by_symbol[symbol][interval] if c.openTime >= start_ms]


def make_signal(direction="LONG", status="WAITING_TRIGGER", **overrides):
    """Minimal valid signal object for lifecycle/performance tests."""
    base = {
        "id": "SIG-test0001", "symbol": "TESTUSDT", "direction": direction,
        "timeframe": "15m", "quality": "A+", "score": 92.0,
        "components": {}, "generatedAt": T0, "signalCandleCloseTime": T0,
        "triggerPrice": 110.0, "entryPrice": None, "entryZone": [109.0, 110.0],
        "currentPrice": 109.0, "currentPriceAt": T0, "currentPriceSource": "scan",
        "stopLoss": 105.0, "takeProfit": 122.5, "riskReward": 2.5,
        "invalidationLevel": 105.5, "targetKind": "swing_high",
        "marketRegime": "TRENDING_UP", "htf4hBias": "strong_bullish",
        "htf1hBias": "bullish", "structure": {"event": "CHoCH_up", "ageBars": 1, "level": 108},
        "liquiditySweep": {"type": "bullish", "level": 106.5, "barsAgo": 3},
        "bos": False, "choch": True, "displacement": True,
        "fvg": {"bottom": 107.0, "top": 108.5}, "orderBlock": None,
        "rsi": 55.0, "adx": 18.0, "atr": 1.5, "atrPercent": 1.4, "relativeVolume": 1.9,
        "vwap": None, "status": status, "expiryAt": T0 + 12 * MS_15M,
        "tradeMaxDurationMs": 16 * MS_15M, "triggeredAt": None, "closedAt": None,
        "outcome": None, "rMultiple": None, "dataSource": "test", "updatedAt": T0,
    }
    if direction == "SHORT":
        base.update({"triggerPrice": 90.0, "entryZone": [90.0, 91.0],
                     "stopLoss": 95.0, "takeProfit": 77.5, "currentPrice": 91.0,
                     "marketRegime": "TRENDING_DOWN",
                     "htf4hBias": "strong_bearish", "htf1hBias": "bearish",
                     "structure": {"event": "CHoCH_down", "ageBars": 1, "level": 92},
                     "liquiditySweep": {"type": "bearish", "level": 93.5, "barsAgo": 3}})
    base.update(overrides)
    return base


def candles_from_closes(closes, start_idx=0, volume=1000.0, wick=0.3, ms=MS_15M):
    """Simple candles from a close path (opens = previous close)."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(mk(start_idx + i, prev, max(prev, c) + wick, min(prev, c) - wick,
                     c, volume, ms=ms))
        prev = c
    return out
