"""Market-structure tests: swings, labels, BOS/CHoCH, displacement, sweeps."""
import pytest

from scanner import structure as st


def H(*vals):
    return list(vals)


class TestSwings:
    def test_swing_high_detection_and_confirmation(self):
        highs = [10, 11, 15, 11, 10, 12]
        swings = st.find_swing_highs(highs, k=2)
        assert len(swings) == 1
        assert swings[0].index == 2 and swings[0].price == 15
        assert swings[0].confirmIndex == 4  # usable only 2 bars later

    def test_swing_low_detection(self):
        lows = [10, 9, 5, 9, 10]
        swings = st.find_swing_lows(lows, k=2)
        assert swings[0].index == 2 and swings[0].price == 5

    def test_equal_highs_do_not_form_strict_swings(self):
        highs = [10, 15, 15, 10]
        assert st.find_swing_highs(highs, k=1) == []  # strict inequality required

    def test_labels_hh_lh_hl_ll(self):
        highs = st.find_swing_highs([1, 5, 2, 6, 2, 7, 3], k=1)
        labelled = st.label_swings(highs)
        assert [x["relation"] for x in labelled] == ["first", "HH", "HH"]
        highs = st.find_swing_highs([7, 5, 3, 6, 2, 4, 1], k=1)
        assert [x["relation"] for x in st.label_swings(highs)][-1] == "LH"
        lows = st.find_swing_lows([5, 3, 4, 2, 4, 1, 6], k=1)
        assert [x["relation"] for x in st.label_swings(lows)] == ["first", "LL", "LL"]
        lows = st.find_swing_lows([1, 3, 2, 4, 2.5, 5, 1], k=1)
        assert [x["relation"] for x in st.label_swings(lows)][-1] == "HL"

    def test_equal_levels_within_atr(self):
        swings = [st.Swing(2, 3, 100.0, "high"), st.Swing(6, 7, 100.05, "high")]
        out = st.equal_levels(swings, [1.0] * 10, 0.1)
        assert len(out) == 1
        assert out[0]["level"] == pytest.approx(100.025)


class TestBosChoch:
    def test_bos_continuation_after_break(self):
        # two consecutive close-breaks above successive swing highs -> BOS_up x2
        highs = [100, 105, 110, 106, 108, 112, 109, 114, 111, 112, 117]
        lows = [98, 100, 104, 102, 105, 108, 106, 110, 108, 109, 114]
        closes = [99, 104, 109, 105, 107, 111, 108, 113, 110, 111.5, 116]
        hi = st.find_swing_highs(highs, 2)
        lo = st.find_swing_lows(lows, 2)
        events = st.detect_structure_events(closes, hi, lo)
        types = [e.type for e in events]
        assert types.count("BOS_up") >= 2
        assert all(e.trendAfter == "up" for e in events)

    def test_choch_on_trend_flip(self):
        # BOS_up at 5 (close 111 > swing high 110), then candle 11 closes 102
        # below the confirmed swing low 104 -> CHoCH_down
        highs = [100, 105, 110, 106, 108, 112, 109, 110, 108, 109, 106.5, 104, 102, 101]
        lows = [98, 100, 104, 102, 105, 108, 106, 107, 104, 105.5, 104.5, 101, 99, 97]
        closes = [99, 104, 109, 105, 107, 111, 108, 109, 106, 107, 105.5, 102, 99, 96]
        hi = st.find_swing_highs(highs, 2)
        lo = st.find_swing_lows(lows, 2)
        events = st.detect_structure_events(closes, hi, lo)
        types = [(e.index, e.type, e.level) for e in events]
        assert (5, "BOS_up", 110.0) in types
        assert (11, "CHoCH_down", 104.0) in types

    def test_wick_break_does_not_count(self):
        # close never exceeds the swing high though highs do
        highs = [100, 104, 110, 104, 120, 104, 100]
        lows = [98, 99, 105, 100, 108, 99, 98]
        closes = [99, 101, 108, 101, 109, 101, 99]
        hi = st.find_swing_highs(highs, 2)
        events = st.detect_structure_events(closes, hi, st.find_swing_lows(lows, 2))
        ups = [e for e in events if e.type in ("BOS_up", "CHoCH_up")]
        assert all(e.level != 110.0 for e in ups)  # 110 never close-broken


class TestDisplacement:
    def test_body_multiple(self):
        opens = [100, 100, 100, 100]
        closes = [100.4, 103.0, 98.4, 100.1]
        atr = [1.0] * 4
        disps = st.find_displacements(opens, closes, atr, 1.5)
        assert [(d.index, d.direction) for d in disps] == [(1, "bullish"), (2, "bearish")]

    def test_warmup_none(self):
        assert st.find_displacements([100, 101], [100, 102], [None, None], 1.5) == []


class TestLiquiditySweep:
    def test_bullish_sweep_of_low(self):
        # swing low 100 at idx2 (confirmed idx4); candle 6 wicks to 99.2, closes 100.5
        highs = [104, 103, 102, 103, 104, 103, 101.5, 104]
        lows = [101, 100.5, 100.0, 101, 102, 102, 99.2, 103]
        closes = [103, 102.5, 101, 102, 103, 102.5, 100.5, 103.5]
        swings = st.find_swing_lows(lows, 2)
        atr = [1.0] * 8
        sweeps = st.find_liquidity_sweeps(highs, lows, closes, [], swings, atr,
                                          min_age_bars=1, max_exceed_atr=1.5)
        assert any(s.direction == "bullish" and s.level == 100.0 and s.index == 6 for s in sweeps)

    def test_genuine_breakout_is_not_a_sweep(self):
        # candle closes BELOW the swing low (acceptance) -> not a sweep
        highs = [104, 103, 102, 103, 104, 103, 101.0, 100]
        lows = [101, 100.5, 100.0, 101, 102, 102, 99.2, 98.5]
        closes = [103, 102.5, 101, 102, 103, 102.5, 100.0, 99.0]
        swings = st.find_swing_lows(lows, 2)
        sweeps = st.find_liquidity_sweeps(highs, lows, closes, [], swings,
                                          [1.0] * 8, min_age_bars=1, max_exceed_atr=1.5)
        assert not any(s.index == 6 and s.level == 100.0 for s in sweeps)

    def test_excessive_wick_exceeding_atr_cap(self):
        # wick 5x ATR below the level is a flush, not a token sweep
        highs = [104, 103, 102, 103, 104, 103, 101.5, 104]
        lows = [101, 100.5, 100.0, 101, 102, 102, 95.0, 103]
        closes = [103, 102.5, 101, 102, 103, 102.5, 100.5, 103.5]
        swings = st.find_swing_lows(lows, 2)
        sweeps = st.find_liquidity_sweeps(highs, lows, closes, [], swings,
                                          [1.0] * 8, min_age_bars=1, max_exceed_atr=1.0)
        assert not any(s.index == 6 and s.level == 100.0 for s in sweeps)
