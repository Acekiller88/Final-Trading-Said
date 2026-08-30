"""Indicator engine tests -- hand-computed reference vectors (spec §34)."""
import math

import pytest

from scanner import indicators as ind


class TestEma:
    def test_seed_and_recursion(self):
        values = [float(i) for i in range(1, 11)]  # 1..10
        out = ind.ema(values, 3)
        assert out[0] == out[1] is None
        assert out[2] == pytest.approx((1 + 2 + 3) / 3)  # SMA seed = 2
        k = 2.0 / 4.0
        assert out[3] == pytest.approx(4 * k + 2 * (1 - k))  # 3
        assert out[4] == pytest.approx(5 * k + 3 * (1 - k))  # 4

    def test_constant_series_is_constant(self):
        out = ind.ema([5.0] * 50, 20)
        assert all(v == pytest.approx(5.0) for v in out[19:])

    def test_insufficient_history_all_none(self):
        assert ind.ema([1.0, 2.0], 20) == [None, None]


class TestRsi:
    def test_all_gains_is_100(self):
        closes = [float(i) for i in range(1, 30)]
        out = ind.rsi(closes, 14)
        assert out[13] is None
        assert out[14] == pytest.approx(100.0)
        assert out[-1] == pytest.approx(100.0)

    def test_hand_computed_wilder(self):
        closes = [10, 11, 10, 11, 10, 11, 10]
        out = ind.rsi(closes, 5)
        # first avg gain = mean(1,0,1,0,1)=0.6, loss = mean(0,1,0,1,0)=0.4
        assert out[5] == pytest.approx(100 - 100 / (1 + 0.6 / 0.4))  # 60.0
        # next: gain=(0.6*4+0)/5=.48, loss=(0.4*4+1)/5=.52 -> RSI=48
        assert out[6] == pytest.approx(100 - 100 / (1 + 0.48 / 0.52))

    def test_monotone_falls_reach_zero(self):
        closes = [float(30 - i) for i in range(30)]
        assert ind.rsi(closes, 14)[-1] == pytest.approx(0.0)


class TestAtr:
    def test_constant_range(self):
        n = 40
        highs, lows, closes = [10.0 + 2.0] * n, [10.0] * n, [11.0] * n
        out = ind.atr(highs, lows, closes, 14)
        assert out[12] is None
        assert out[13] == pytest.approx(2.0)
        assert out[-1] == pytest.approx(2.0)

    def test_true_range_uses_prev_close(self):
        highs = [12.0, 10.5]
        lows = [10.0, 8.5]
        closes = [11.0, 10.0]
        tr = ind.true_range(highs, lows, closes)
        assert tr[0] == pytest.approx(2.0)
        assert tr[1] == pytest.approx(max(2.0, abs(10.5 - 11.0), abs(8.5 - 11.0)))  # 2.5

    def test_wilder_smoothing_one_candle(self):
        # 15 identical TRs then one TR of 3 -> ATR = (2*13 + 3)/14
        highs = [12.0] * 15 + [13.0]
        lows = [10.0] * 15 + [10.0]
        closes = [11.0] * 16
        out = ind.atr(highs, lows, closes, 14)
        assert out[-1] == pytest.approx((2.0 * 13 + 3.0) / 14)


class TestAdx:
    def test_uptrend_directional_indices(self):
        n = 60
        highs = [100.0 + 1.0 * i for i in range(n)]
        lows = [98.0 + 1.0 * i for i in range(n)]
        closes = [99.0 + 1.0 * i for i in range(n)]
        pack = ind.adx(highs, lows, closes, 14)
        last = n - 1
        assert pack["plusDi"][last] > pack["minusDi"][last]
        assert pack["adx"][last] > 20  # strong trend

    def test_flat_market_low_adx(self):
        n = 80
        closes, highs, lows = [50.0] * n, [50.02] * n, [49.98] * n
        pack = ind.adx(highs, lows, closes, 14)
        assert pack["adx"][-1] < 20

    def test_warmup_positions(self):
        n = 60
        closes = [100.0 + i * 0.5 for i in range(n)]
        pack = ind.adx(closes, closes, closes, 14)
        first_valid = next(i for i, v in enumerate(pack["adx"]) if v is not None)
        assert first_valid == 2 * 14 - 1


class TestRelativeVolume:
    def test_excludes_current_from_baseline(self):
        vols = [1000.0] * 20 + [1500.0]
        out = ind.relative_volume(vols, 20)
        assert out[-1] == pytest.approx(1.5)
        assert out[0] is None  # no baseline for the first candle

    def test_zero_baseline(self):
        out = ind.relative_volume([0.0, 100.0], 20)
        assert out[1] is None


class TestVwap:
    def test_rolling_window(self):
        highs = [10.0, 20.0]
        lows = [10.0, 20.0]
        closes = [10.0, 20.0]
        vols = [1.0, 1.0]
        out = ind.rolling_vwap(highs, lows, closes, vols, 2)
        assert out[0] is None
        assert out[1] == pytest.approx(15.0)


class TestPurity:
    def test_no_lookahead_in_ema(self):
        values = [3.0, 1.0, 4.0, 1.5, 5.0, 9.0, 2.5, 6.0]
        full = ind.ema(values, 3)
        prefix = ind.ema(values[:5], 3)
        assert full[:5] == prefix  # history must not change from future data

    def test_all_outputs_finite_or_none(self):
        import random
        rng = random.Random(7)  # test-only RNG; the ENGINE itself never uses randomness
        vals = [rng.uniform(1, 100) for _ in range(120)]
        for series in (ind.ema(vals, 20), ind.rsi(vals, 14), ind.relative_volume(vals, 20)):
            for v in series:
                assert v is None or math.isfinite(v)
