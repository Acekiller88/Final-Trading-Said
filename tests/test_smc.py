"""SMC tests: Fair Value Gaps (3-candle model) and Order Blocks."""
import pytest

from scanner import structure as st
from scanner import smc


class TestFvg:
    def test_bullish_three_candle_gap(self):
        # candle0 high 100, candle2 low 103 -> bullish gap [100, 103]
        opens = [99, 101, 102]
        highs = [100, 101.5, 104]
        lows = [98, 100.5, 103]
        closes = [99.5, 101, 103.5]
        gaps = smc.find_fvgs(opens, highs, lows, closes, [1.0] * 3, min_gap_atr=0.1)
        assert len(gaps) == 1
        g = gaps[0]
        assert g.direction == "bullish"
        assert g.bottom == pytest.approx(100.0) and g.top == pytest.approx(103.0)
        assert g.midpoint == pytest.approx(101.5)
        assert g.index == 2  # known at the close of the 3rd candle

    def test_bearish_gap(self):
        opens = [105, 103, 102]
        highs = [106, 103.5, 102.5]
        lows = [104, 102.5, 100]
        closes = [104.5, 103, 100.5]
        gaps = smc.find_fvgs(opens, highs, lows, closes, [1.0] * 3, min_gap_atr=0.1)
        assert gaps[0].direction == "bearish"
        assert gaps[0].bottom == pytest.approx(102.5) and gaps[0].top == pytest.approx(104.0)

    def test_micro_gap_filtered_by_atr(self):
        opens = [99, 100, 100]
        highs = [100, 100.5, 100.3]
        lows = [98, 99.8, 100.2]
        closes = [99.5, 100.2, 100.4]
        # gap = 0.2 < 0.5 (0.5x ATR)
        assert smc.find_fvgs(opens, highs, lows, closes, [1.0] * 3, min_gap_atr=0.5) == []

    def test_no_gap_when_overlap(self):
        opens = [99, 101, 102]
        highs = [100, 101.5, 104]
        lows = [98, 100.5, 99.5]  # low overlaps candle-0 high
        closes = [99.5, 101, 103.5]
        assert smc.find_fvgs(opens, highs, lows, closes, [1.0] * 3, 0.1) == []


class TestOrderBlock:
    def _frames(self):
        # swing high @2 (101.9), bullish displacement @6 (body 3.0) validated
        # by BOS_up @6; idx5 is the last red candle -> the bullish OB
        opens = [100.0, 100.5, 101.0, 100.5, 101.2, 101.1, 101.0, 104.0]
        highs = [100.6, 101.5, 101.9, 101.0, 101.5, 101.3, 104.4, 105.5]
        lows = [99.7, 100.0, 100.4, 99.8, 100.9, 100.7, 100.8, 104.8]
        closes = [100.4, 100.8, 101.2, 100.2, 101.3, 101.05, 104.0, 105.3]
        atr = [1.0] * 8
        disps = st.find_displacements(opens, closes, atr, 1.5)
        events = st.detect_structure_events(closes, st.find_swing_highs(highs, 2),
                                            st.find_swing_lows(lows, 2))
        return opens, highs, lows, closes, atr, disps, events

    def test_bullish_ob_before_displacement(self):
        opens, highs, lows, closes, atr, disps, events = self._frames()
        obs = smc.find_order_blocks(opens, highs, lows, closes, disps, events,
                                    lookback=5, bos_within_bars=3)
        bull = [ob for ob in obs if ob.direction == "bullish"]
        assert bull, f"expected bullish OB, events={[e.type for e in events]}, disps={[d.index for d in disps]}"
        # origin must be a bearish candle just before the displacement
        ob = bull[0]
        assert closes[ob.index] < opens[ob.index]
        assert ob.displacementIndex >= ob.index

    def test_zone_is_candle_range(self):
        opens, highs, lows, closes, atr, disps, events = self._frames()
        obs = smc.find_order_blocks(opens, highs, lows, closes, disps, events, 5, 3)
        ob = [o for o in obs if o.direction == "bullish"][0]
        assert ob.bottom == pytest.approx(lows[ob.index])
        assert ob.top == pytest.approx(highs[ob.index])

    def test_no_ob_without_structure_validation(self):
        opens, highs, lows, closes, atr, disps, events = self._frames()
        # kill the displacement (tiny body) -> no structure validation
        closes = list(closes)
        closes[6] = 101.1
        disps = st.find_displacements(opens, closes, atr, 1.5)
        disps = st.find_displacements(opens, closes, atr, 1.5)
        events = st.detect_structure_events(closes, st.find_swing_highs(highs, 2),
                                            st.find_swing_lows(lows, 2))
        assert smc.find_order_blocks(opens, highs, lows, closes, disps, events, 5, 3) == []
