"""Scoring + risk-model tests (confluence score, tiers, RR, entry/SL/TP)."""
import pytest

from scanner.config import Config
from scanner.risk import build_setup
from scanner.scoring import score_setup, quality_tier


@pytest.fixture()
def cfg():
    return Config.load()


class TestScoring:
    def _parts(self, **over):
        parts = {
            "bias4h": {"bias": "strong_bullish"}, "bias1h": {"bias": "strong_bullish"},
            "structureEventAgeBars": 2, "additionalBos": True, "displacement": True,
            "sweep": True, "fvg": True, "orderBlock": True,
            "rsi": 58.0, "adx": 22.0, "adxRising": True, "diSpread": 6.0,
            "plusDi": 25.0, "minusDi": 15.0,
            "relVolume": 1.8, "atrPercent": 1.0,
        }
        parts.update(over)
        return parts

    def test_full_confluence_scores_high(self, cfg):
        out = score_setup("LONG", self._parts(), 3.2, cfg)
        assert out["score"] >= 90
        assert quality_tier(out["score"], cfg) == "A+"

    def test_component_weights_sum_to_100(self, cfg):
        w = cfg.get("scoring.weights")
        assert sum(w.values()) == 100

    def test_missing_fvg_and_ob_reduces_score(self, cfg):
        full = score_setup("LONG", self._parts(), 3.2, cfg)
        partial = score_setup("LONG", self._parts(fvg=False, orderBlock=False), 3.2, cfg)
        assert partial["score"] == pytest.approx(full["score"] - 10)

    def test_below_threshold_rejected(self, cfg):
        out = score_setup("LONG", self._parts(
            fvg=False, orderBlock=False, additionalBos=False,
            bias1h={"bias": "neutral"}, relVolume=1.0, rr_low=True), 2.55, cfg)
        assert out["score"] < 80
        assert quality_tier(out["score"], cfg) is None

    def test_rr_component_scaling(self, cfg):
        at_min = score_setup("LONG", self._parts(), 2.5, cfg)
        at_pref = score_setup("LONG", self._parts(), 3.0, cfg)
        assert at_min["components"]["riskReward"] == pytest.approx(5.0)
        assert at_pref["components"]["riskReward"] == pytest.approx(10.0)

    def test_short_mirrors_long_htf(self, cfg):
        long = score_setup("LONG", self._parts(), 3.0, cfg)
        short = score_setup("SHORT", self._parts(
            bias4h={"bias": "strong_bearish"}, bias1h={"bias": "strong_bearish"},
            rsi=42.0, plusDi=15.0, minusDi=25.0), 3.0, cfg)
        assert short["score"] == pytest.approx(long["score"], abs=0.5)


class TestRiskModel:
    def _levels(self):
        return [(103.5, 10, "swing_high"), (107.5, 20, "swing_high"), (112.0, 30, "equal_highs")]

    def test_long_setup_rr_math(self, cfg):
        setup, reason = build_setup("long", 100.0, 1.0, 100.5, 99.5, 98.5, self._levels(), cfg)
        assert setup is not None, reason
        assert setup.trigger == pytest.approx(100.5 + 0.1)  # buffer 0.1 ATR
        assert setup.stop == pytest.approx(98.5 - 0.5)      # buffer 0.5 ATR
        assert setup.entryLow == pytest.approx(setup.trigger - 0.5)
        assert setup.entryHigh == pytest.approx(setup.trigger)
        expected_rr = (setup.target - setup.trigger) / (setup.trigger - setup.stop)
        assert setup.rr == pytest.approx(expected_rr, abs=1e-3)
        assert setup.rr >= 2.5

    def test_preferred_rr_target_chosen_when_available(self, cfg):
        # 107.5 gives RR 2.65 (>= min) but 112 reaches the preferred 3R ->
        # per spec §16 the 3R target is preferred when structure allows
        setup, _ = build_setup("long", 100.0, 1.0, 100.5, 99.5, 98.5, self._levels(), cfg)
        assert setup.target == pytest.approx(112.0)
        assert setup.rr >= 3.0

    def test_nearest_min_rr_target_when_no_3r_available(self, cfg):
        levels = [(103.5, 10, "swing_high"), (107.2, 20, "swing_high")]
        setup, reason = build_setup("long", 100.0, 1.0, 100.5, 99.5, 98.5, levels, cfg)
        assert setup is not None
        assert setup.target == pytest.approx(107.2)  # nearest with RR >= 2.5
        assert 2.5 <= setup.rr < 3.0

    def test_reject_when_no_target_reaches_min_rr(self, cfg):
        levels = [(101.5, 10, "swing_high"), (102.5, 20, "swing_high")]
        setup, reason = build_setup("long", 100.0, 1.0, 100.5, 99.5, 98.5, levels, cfg)
        assert setup is None
        assert "min RR" in reason

    def test_short_setup_geometry(self, cfg):
        levels = [(96.5, 10, "swing_low"), (92.5, 20, "swing_low"), (88.0, 30, "equal_lows")]
        setup, reason = build_setup("short", 100.0, 1.0, 100.2, 99.5, 101.5, levels, cfg)
        assert setup is not None, reason
        assert setup.trigger == pytest.approx(99.5 - 0.1)
        assert setup.stop > setup.entryHigh > setup.target
        assert setup.rr >= 2.5

    def test_stop_too_wide_rejected(self, cfg):
        setup, reason = build_setup("long", 100.0, 1.0, 100.5, 99.5, 90.0, self._levels(), cfg)
        assert setup is None and "stop too wide" in reason

    def test_stop_too_tight_rejected(self, cfg):
        setup, reason = build_setup("long", 100.0, 1.0, 100.5, 99.5, 101.0, self._levels(), cfg)
        assert setup is None and "tight" in reason
