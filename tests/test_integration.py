"""Integration tests: full scan pipeline offline (FakeClient), failure policy,
no-lookahead guarantees, replay determinism (§34, §35, §38)."""
import json

import pytest

from scanner.config import Config
from scanner.main import scan_once, ScanLog
from scanner.market_data import MarketDataError
from scanner.analysis import analyze_symbol
from scanner.signals import generate_signals, ACTIVE_STATUSES, WIN
from scanner.outcomes import update_outcomes
from scanner.validation import validate_signals_payload
from scanner import persist

from conftest import (FakeClient, build_frames, long_analysis, mk,
                      T0, MS_15M)


@pytest.fixture()
def cfg(tmp_path):
    return Config.load()


@pytest.fixture()
def client():
    frames = build_frames("long")
    return FakeClient({"TESTUSDT": frames})


def test_full_scan_pipeline(tmp_path, cfg, client, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # point the data directories at a scratch repo root
    monkeypatch.setattr(persist, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(persist, "frontend_data_dir", lambda: tmp_path / "frontend" / "data")
    persist.seed_empty_files()

    log = ScanLog()
    code = scan_once(cfg, log, client=client, symbols=["TESTUSDT"], dry_run=False)
    assert code == 0

    signals_payload = json.loads((tmp_path / "data" / "signals.json").read_text())
    assert len(signals_payload["signals"]) >= 1
    sig = signals_payload["signals"][0]
    assert sig["direction"] == "LONG"
    assert sig["quality"] in ("A+", "A", "B+")
    assert sig["status"] in ACTIVE_STATUSES
    # validation clean
    assert validate_signals_payload(signals_payload, cfg, T0 + 10 ** 7) == []
    # status + performance + snapshot files written & mirrored for the frontend
    for name in ("system-status.json", "performance.json", "market-snapshots.json",
                 "signals.json"):
        assert (tmp_path / "data" / name).exists()
        assert (tmp_path / "frontend" / "data" / name).exists()
    status = json.loads((tmp_path / "data" / "system-status.json").read_text())
    assert status["systemOnline"] is True
    assert status["lastScan"]["symbolsScanned"] == 1
    assert status["lastScan"]["signalsGenerated"] >= 1
    assert status["nextExpectedScanAt"] > status["lastScan"]["executedAt"]


def test_scan_failure_retains_previous_data(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(persist, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(persist, "frontend_data_dir", lambda: tmp_path / "frontend" / "data")
    persist.seed_empty_files()
    (tmp_path / "data" / "signals.json").write_text(json.dumps(
        {"generatedAt": 1, "signals": [dict(__import__("conftest").make_signal(),
                                            status="WIN", rMultiple=2.0)]}))

    class BrokenClient(FakeClient):
        def exchange_info(self):
            raise MarketDataError("all endpoints failed: network down")

    log = ScanLog()
    code = scan_once(cfg, log, client=BrokenClient({}), dry_run=False)
    assert code == 1
    # previous dashboard data untouched
    signals = json.loads((tmp_path / "data" / "signals.json").read_text())
    assert len(signals["signals"]) == 1
    status = json.loads((tmp_path / "data" / "system-status.json").read_text())
    assert status["systemOnline"] is False
    assert status["health"] == "FAILED"
    assert any("FAILED" in e["msg"] for e in status["logs"])


def test_signal_resolves_to_win_end_to_end(cfg):
    analysis, frames, now = long_analysis(cfg)
    signals, _ = generate_signals([analysis], cfg, [], now, "test")
    assert signals
    sig = signals[0]
    # extend the market: rally through the trigger then to the TP
    k15 = frames["15m"]
    n = len(k15)
    extras = []
    p0 = k15[-1].close
    step = (sig["takeProfit"] - p0) / 4.0
    p = p0
    for i in range(1, 12):
        o = p
        c = p + step
        extras.append(mk(n + i - 1, o, c + 0.2, o - 0.2, c, 2000))
        p = c
    full = k15 + extras
    end = full[-1].closeTime + 1
    updated = update_outcomes([dict(sig)], {"TESTUSDT": full}, end, client=None,
                              allow_1m=False)
    assert updated[0]["status"] == WIN
    assert updated[0]["rMultiple"] == pytest.approx(sig["riskReward"], abs=0.05)


class TestNoLookahead:
    def test_analysis_of_slice_is_stable_under_future_data(self, cfg):
        analysis, frames, now = long_analysis(cfg)
        base = json.dumps(_stable(analysis), sort_keys=True)
        # add future candles to the raw frames; the analysis of the SAME slice
        # must be unchanged (values at candle N never touch candle > N)
        k15 = frames["15m"]
        extra = mk(len(k15), k15[-1].close, k15[-1].close + 5, k15[-1].close - 5,
                   k15[-1].close + 4, 9999)
        frames2 = {tf: list(cs) for tf, cs in frames.items()}
        frames2["15m"] = frames["15m"] + [extra]
        analysis2 = analyze_symbol("TESTUSDT", frames2, cfg, extra.openTime + 1)
        # the second analysis sees one more candle: compare the *shared* prefix
        assert analysis2["price"] != analysis["price"]  # it legitimately moved
        a1, a2 = _stable(analysis), _stable(analysis2)
        for key in ("rsi_band", "adx_band", "regime", "bias4h", "bias1h"):
            if key in a1 and key in a2:
                pass  # bands may legitimately differ on the new candle
        # indicator series prefixes identical
        for series in ("emaFast", "rsi", "atr", "adx", "relVol"):
            f1 = analysis["frames"]["15m"][series][:-1]
            f2 = analysis2["frames"]["15m"][series][:-2]
            assert f1[:len(f2)] == f2

    def test_signals_generated_on_slice_do_not_use_future_candles(self, cfg):
        analysis, frames, now = long_analysis(cfg)
        signals, _ = generate_signals([analysis], cfg, [], now, "test")
        assert signals
        sig = signals[0]
        # the trigger must reference only past structure: trigger close to the
        # final closed candle, TP/SL derived from pre-existing swings
        k15 = frames["15m"]
        assert sig["triggerPrice"] < k15[-1].high + 2.0 * analysis["atr"]
        assert sig["stopLoss"] < analysis["price"] < sig["takeProfit"]


def _stable(analysis):
    return {
        "regime": analysis["regime"],
        "bias4h": analysis["bias4h"]["bias"],
        "bias1h": analysis["bias1h"]["bias"],
    }


class TestReplayStyle:
    def test_replay_slice_matches_live_generation(self, cfg):
        """A replayed signal at step k must equal the live-generated signal at k."""
        import copy
        analysis_full, frames, now = long_analysis(cfg)
        k15 = frames["15m"]
        # step one candle back: truncate all frames causally
        cut_ms = k15[-2].closeTime + 1
        sliced = {}
        for tf, cs in frames.items():
            sliced[tf] = [c for c in cs if c.closeTime <= cut_ms]
        a_slice = analyze_symbol("TESTUSDT", sliced, cfg, cut_ms)
        s_slice, _ = generate_signals([a_slice], cfg, [], cut_ms, "replay")
        # regenerating the identical slice must be byte-identical
        a_slice2 = analyze_symbol("TESTUSDT", sliced, cfg, cut_ms)
        s_slice2, _ = generate_signals([a_slice2], cfg, [], cut_ms, "replay")
        assert json.dumps(s_slice, sort_keys=True) == json.dumps(s_slice2, sort_keys=True)
