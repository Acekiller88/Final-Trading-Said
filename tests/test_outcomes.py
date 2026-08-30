"""Lifecycle tests: trigger, WIN/LOSS, EXPIRED, AMBIGUOUS, holding cap,
1-minute ambiguity resolution, and immutability (§18-20, §33)."""
import pytest

from scanner.market_data import Candle
from scanner.outcomes import update_signal, update_outcomes
from scanner.signals import (WAITING_TRIGGER, TRIGGERED, WIN, LOSS, EXPIRED,
                             AMBIGUOUS, CANCELLED)
from scanner.validation import check_immutability

from conftest import make_signal, mk, T0, MS_15M, MS_1M


def candle(i, o, h, l, c, v=1000.0):
    return mk(i, o, h, l, c, v)


class TestTrigger:
    def test_waits_until_trigger_touched(self):
        sig = make_signal()  # trigger 110, TP 122.5, SL 105
        later = [candle(1, 109, 109.5, 108.5, 109.2), candle(2, 109, 109.8, 108.8, 109.4)]
        sig = update_signal(sig, later, T0 + 3 * MS_15M)
        assert sig["status"] == WAITING_TRIGGER

    def test_triggers_and_fills_at_trigger(self):
        sig = make_signal()
        later = [candle(1, 109, 110.4, 108.9, 110.1)]
        sig = update_signal(sig, later, T0 + 2 * MS_15M)
        assert sig["status"] == TRIGGERED
        assert sig["entryPrice"] == pytest.approx(110.0)
        assert sig["triggeredAt"] == later[0].openTime

    def test_gap_open_fills_at_open(self):
        sig = make_signal()
        later = [candle(1, 111.0, 112.0, 110.5, 111.5)]  # opens above trigger
        sig = update_signal(sig, later, T0 + 2 * MS_15M)
        assert sig["status"] == TRIGGERED
        assert sig["entryPrice"] == pytest.approx(111.0)


class TestResolution:
    def test_tp_hit_is_win_with_r_multiple(self):
        sig = make_signal()
        sig["status"] = TRIGGERED
        sig["entryPrice"] = 110.0
        sig["triggeredAt"] = T0 + MS_15M
        later = [candle(1, 110, 111, 109.5, 110.8), candle(2, 111, 122.8, 110.9, 122.6)]
        sig = update_signal(sig, later, T0 + 3 * MS_15M)
        assert sig["status"] == WIN
        assert sig["rMultiple"] == pytest.approx((122.5 - 110.0) / 5.0, abs=1e-3)

    def test_sl_hit_is_loss_minus_1r(self):
        sig = make_signal()
        sig["status"] = TRIGGERED
        sig["entryPrice"] = 110.0
        sig["triggeredAt"] = T0 + MS_15M
        later = [candle(1, 110, 110.5, 104.8, 105.2)]
        sig = update_signal(sig, later, T0 + 2 * MS_15M)
        assert sig["status"] == LOSS
        assert sig["rMultiple"] == -1.0

    def test_same_candle_tp_and_sl_is_ambiguous(self):
        sig = make_signal()
        sig["status"] = TRIGGERED
        sig["entryPrice"] = 110.0
        sig["triggeredAt"] = T0 + MS_15M
        later = [candle(1, 110, 123.0, 104.5, 120.0)]  # both levels in one candle
        sig = update_signal(sig, later, T0 + 2 * MS_15M, client=None, allow_1m=False)
        assert sig["status"] == AMBIGUOUS
        assert sig["rMultiple"] is None  # ambiguous is never counted as a win


class TestExpiry:
    def test_untriggered_signal_expires(self):
        sig = make_signal()  # expiryAt = T0 + 12 candles
        candles = [candle(i, 109, 109.6, 108.6, 109.2) for i in range(1, 14)]
        sig = update_signal(sig, candles, T0 + 14 * MS_15M)
        assert sig["status"] == EXPIRED

    def test_trigger_beats_expiry_inside_candle(self):
        sig = make_signal()
        # candle closing just after expiryAt but trading through the trigger
        i = 13
        c = candle(i, 109, 111.0, 108.9, 110.5)
        c = Candle(c.openTime, c.open, c.high, c.low, c.close, c.volume,
                   sig["expiryAt"] + 1, c.quoteVolume, c.trades)
        sig = update_signal(sig, [c], c.closeTime + 1)
        assert sig["status"] == TRIGGERED


class TestMaxHolding:
    def test_cancelled_after_max_duration(self):
        sig = make_signal()
        sig["status"] = TRIGGERED
        sig["entryPrice"] = 110.0
        sig["triggeredAt"] = T0 + MS_15M
        # 17 candles drifting sideways, never hitting TP/SL
        candles = [candle(i, 110, 110.9, 109.2, 110.4) for i in range(2, 20)]
        sig = update_signal(sig, candles, T0 + 20 * MS_15M)
        assert sig["status"] == CANCELLED
        assert sig["outcome"] == "max_holding_duration_exceeded"


class TestOneMinuteResolution:
    def test_1m_data_resolves_ambiguous_candle(self):
        from conftest import FakeClient
        sig = make_signal()
        sig["status"] = TRIGGERED
        sig["entryPrice"] = 110.0
        sig["triggeredAt"] = T0 + MS_15M
        later = [candle(1, 110, 123.0, 104.5, 120.0)]
        # 1m path: dips to SL first, then rallies to TP -> LOSS
        minutes = []
        for j in range(15):
            o = 110.0 if j == 0 else 105.0
            minutes.append(mk(j, o, o + 0.3, o - 0.3, o - 0.1, 500.0, ms=MS_1M,
                              t0=later[0].openTime))
        minutes[0] = Candle(minutes[0].openTime, 110.0, 110.2, 104.8, 104.9,
                            500.0, minutes[0].closeTime, 500, 10)
        client = FakeClient({}, extra_1m={"TESTUSDT": minutes})
        sig = update_signal(sig, later, T0 + 2 * MS_15M, client=client, allow_1m=True)
        assert sig["status"] == LOSS


class TestImmutability:
    def test_historical_parameters_never_rewritten(self):
        old = [make_signal(status="WIN", rMultiple=2.5)]
        new = [dict(old[0])]
        new[0]["status"] = "WIN"
        assert check_immutability(old, new) == []
        new[0]["triggerPrice"] = 999.0  # repaint attempt
        errors = check_immutability(old, new)
        assert any("triggerPrice" in e for e in errors)

    def test_update_outcomes_only_touches_allowed_fields(self):
        sig = make_signal()
        before = dict(sig)
        candles = [candle(1, 109, 109.5, 108.5, 109.2)]
        sig = update_outcomes([sig], {"TESTUSDT": candles}, T0 + 2 * MS_15M)[0]
        for field in ("triggerPrice", "stopLoss", "takeProfit", "entryZone",
                      "riskReward", "generatedAt", "expiryAt"):
            assert sig[field] == before[field]
        assert sig["currentPrice"] == pytest.approx(109.2)  # allowed to refresh
