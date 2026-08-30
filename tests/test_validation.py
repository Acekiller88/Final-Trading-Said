"""Validation tests (§31): invalid signals must never reach the dashboard."""
import pytest

from scanner.config import Config
from scanner.validation import (validate_signal, validate_signals_payload,
                                check_immutability)

from conftest import make_signal


@pytest.fixture()
def cfg():
    return Config.load()


class TestSignalValidation:
    def test_valid_signal_passes(self, cfg):
        assert validate_signal(make_signal(), cfg) == []

    def test_sl_equals_tp_rejected(self, cfg):
        sig = make_signal(stopLoss=122.5)  # SL identical to TP
        errs = validate_signal(sig, cfg)
        assert any("SL == TP" in e for e in errs)

    def test_long_level_ordering(self, cfg):
        sig = make_signal(stopLoss=111.0)  # SL above entry for a LONG
        errs = validate_signal(sig, cfg)
        assert any("SL < entry < TP" in e for e in errs)

    def test_short_level_ordering(self, cfg):
        sig = make_signal(direction="SHORT", takeProfit=95.0)  # TP above entry
        errs = validate_signal(sig, cfg)
        assert any("TP < entry < SL" in e for e in errs)

    def test_rr_must_be_mathematically_consistent(self, cfg):
        sig = make_signal(riskReward=9.9)
        errs = validate_signal(sig, cfg)
        assert any("RR inconsistent" in e for e in errs)

    def test_quality_must_match_score(self, cfg):
        sig = make_signal(score=81.0, quality="A+")
        errs = validate_signal(sig, cfg)
        assert any("quality" in e for e in errs)

    def test_score_below_publish_floor(self, cfg):
        sig = make_signal(score=70.0, quality="B+")
        errs = validate_signal(sig, cfg)
        assert any("below" in e for e in errs)

    def test_missing_indicator_rejected(self, cfg):
        sig = make_signal(rsi=None)
        errs = validate_signal(sig, cfg)
        assert any("indicator" in e for e in errs)

    def test_missing_fields(self, cfg):
        sig = make_signal()
        del sig["takeProfit"]
        errs = validate_signal(sig, cfg)
        assert errs and "missing fields" in errs[0]


class TestPayloadValidation:
    def test_duplicate_active_same_direction_flagged(self, cfg):
        a = make_signal(id="SIG-a")
        b = make_signal(id="SIG-b", generatedAt=make_signal.__defaults__ and 1700000900000)
        errs = validate_signals_payload({"signals": [a, b]}, cfg, 1700001000000)
        assert any("multiple active" in e for e in errs)

    def test_duplicate_ids_flagged(self, cfg):
        a = make_signal(id="SIG-a")
        b = make_signal(id="SIG-a", status="WIN", rMultiple=2.0)
        errs = validate_signals_payload({"signals": [a, b]}, cfg, 1700001000000)
        assert any("duplicate id" in e for e in errs)
