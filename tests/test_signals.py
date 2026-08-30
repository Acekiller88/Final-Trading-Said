"""Signal-model tests: LONG/SHORT validation, hard rejections, dedupe (§34)."""
import pytest

from scanner.config import Config
from scanner.signals import (generate_signals, try_setup, setup_hash,
                             signal_id, WAITING_TRIGGER)

from conftest import (build_frames, long_analysis, short_analysis, make_signal,
                      T0, MS_15M)


@pytest.fixture()
def cfg():
    return Config.load()


class TestLongModel:
    def test_synthetic_smc_reversal_produces_a_plus_long(self, cfg):
        analysis, _, _ = long_analysis(cfg)
        payload, reasons = try_setup("long", analysis, cfg)
        assert payload is not None, reasons
        assert payload["quality"] in ("A+", "A", "B+")
        assert payload["score"] >= 85
        assert payload["riskReward"] >= 2.5
        assert payload["stopLoss"] < payload["entryZone"][0]
        assert payload["takeProfit"] > payload["trigger"]
        assert payload["liquiditySweep"]["type"] == "bullish"
        assert payload["structure"]["event"].startswith(("CHoCH", "BOS"))
        assert payload["fvg"] or payload["orderBlock"]

    def test_generate_signals_builds_full_object(self, cfg):
        analysis, _, now = long_analysis(cfg)
        signals, rejects = generate_signals([analysis], cfg, [], now, "test")
        assert len(signals) == 1
        sig = signals[0]
        for field in ("id", "symbol", "direction", "timeframe", "quality", "score",
                      "generatedAt", "triggerPrice", "entryZone", "currentPrice",
                      "stopLoss", "takeProfit", "riskReward", "marketRegime",
                      "htf4hBias", "htf1hBias", "structure", "liquiditySweep",
                      "bos", "choch", "displacement", "fvg", "orderBlock",
                      "rsi", "adx", "atr", "relativeVolume", "status", "expiryAt"):
            assert field in sig, f"missing {field}"
        assert sig["status"] == WAITING_TRIGGER
        assert sig["direction"] == "LONG"
        assert sig["expiryAt"] == now + 12 * MS_15M

    def test_deterministic_output(self, cfg):
        analysis, _, now = long_analysis(cfg)
        a, _ = generate_signals([analysis], cfg, [], now, "test")
        b, _ = generate_signals([analysis], cfg, [], now, "test")
        import json
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class TestShortModel:
    def test_mirrored_market_produces_short(self, cfg):
        analysis, _, _ = short_analysis(cfg)
        payload, reasons = try_setup("short", analysis, cfg)
        assert payload is not None, reasons
        assert payload["riskReward"] >= 2.5
        assert payload["stopLoss"] > payload["entryZone"][1]
        assert payload["takeProfit"] < payload["trigger"]
        assert payload["liquiditySweep"]["type"] == "bearish"


class TestHardRejections:
    def test_htf_conflict_rejects(self, cfg):
        analysis, _, _ = long_analysis(cfg)
        analysis["bias4h"]["bias"] = "strong_bearish"
        analysis["bias1h"]["bias"] = "bearish"
        payload, reasons = try_setup("long", analysis, cfg)
        assert payload is None and "HTF conflict" in reasons[0]

    def test_ranging_regime_rejects(self, cfg):
        analysis, _, _ = long_analysis(cfg)
        analysis["regime"] = "RANGING"
        payload, reasons = try_setup("long", analysis, cfg)
        assert payload is None and "RANGING" in reasons[0]

    def test_low_relative_volume_rejects(self, cfg):
        analysis, _, _ = long_analysis(cfg)
        analysis["relVolume"] = 1.0
        payload, reasons = try_setup("long", analysis, cfg)
        assert payload is None and "relVol" in reasons[0]

    def test_rsi_out_of_band_rejects(self, cfg):
        analysis, _, _ = long_analysis(cfg)
        analysis["rsi"] = 80.0  # overextended
        payload, reasons = try_setup("long", analysis, cfg)
        assert payload is None and "RSI" in reasons[0]

    def test_missing_indicators_reject(self, cfg):
        analysis, _, _ = long_analysis(cfg)
        analysis["adx"] = None
        payload, reasons = try_setup("long", analysis, cfg)
        assert payload is None

    def test_score_below_threshold_never_published(self, cfg):
        analysis, _, now = long_analysis(cfg)
        # poison the scoring via partial SMC (drop FVG/OB context)
        analysis["frames"]["15m"]["fvgs"] = []
        analysis["frames"]["15m"]["orderBlocks"] = []
        payload, reasons = try_setup("long", analysis, cfg)
        if payload is None:
            assert reasons
        else:
            assert payload["score"] >= 80  # anything published must clear the bar


class TestDedupeAndCooldowns:
    def test_duplicate_active_same_direction_suppressed(self, cfg):
        analysis, _, now = long_analysis(cfg)
        existing = [make_signal(direction="LONG", symbol=analysis["symbol"],
                                generatedAt=now - 5 * MS_15M)]
        signals, rejects = generate_signals([analysis], cfg, existing, now, "test")
        assert signals == []
        assert any("duplicate active" in r["reason"] for r in rejects)

    def test_symbol_cooldown_suppresses_same_direction(self, cfg):
        analysis, _, now = long_analysis(cfg)
        # a resolved SHORT within the cooldown window must not re-fire as SHORT
        # (the LONG on the opposite side remains allowed -- sameDirectionOnly)
        existing = [make_signal(direction="SHORT", symbol=analysis["symbol"],
                                status="WIN", rMultiple=-1.0,
                                generatedAt=now - 30 * 60_000)]
        signals, rejects = generate_signals([analysis], cfg, existing, now, "test")
        assert all(s["direction"] != "SHORT" for s in signals)  # model rejects SHORT here
        # same-direction cooldown also blocks the LONG when recent
        existing2 = [make_signal(direction="LONG", symbol=analysis["symbol"],
                                 status="WIN", rMultiple=3.0,
                                 generatedAt=now - 30 * 60_000)]
        signals2, rejects2 = generate_signals([analysis], cfg, existing2, now, "test")
        assert signals2 == []
        assert any("cooldown" in r["reason"] for r in rejects2)

    def test_max_active_signals_cap(self, cfg):
        analysis, _, now = long_analysis(cfg)
        existing = [make_signal(direction="LONG", symbol=f"SYM{i}USDT",
                                generatedAt=now - 10 * 60 * 60_000)
                    for i in range(12)]
        signals, _ = generate_signals([analysis], cfg, existing, now, "test")
        assert signals == []

    def test_zero_forced_signals_when_no_setup(self, cfg):
        # an uptrend market with no sweep/CHoCH in the window -> no SHORT signal
        analysis, _, now = long_analysis(cfg)
        signals, rejects = generate_signals([analysis], cfg, [], now, "test")
        assert all(s["direction"] == "LONG" for s in signals)
        assert any("HTF" in r["reason"] for r in rejects if r["direction"] == "short")


class TestIds:
    def test_setup_hash_stable(self):
        a = setup_hash("BTCUSDT", "long", 100.0, 95.0, 110.0)
        b = setup_hash("BTCUSDT", "long", 100.0, 95.0, 110.0)
        c = setup_hash("BTCUSDT", "long", 100.01, 95.0, 110.0)
        assert a == b and a != c

    def test_signal_id_format(self):
        sid = signal_id("BTCUSDT", "long", T0, 100.0)
        assert sid.startswith("SIG-") and len(sid) == 14
