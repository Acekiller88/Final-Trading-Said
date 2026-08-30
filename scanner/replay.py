"""Replay / backtest mode (spec §35).

Replays history candle-by-candle: at each step t the engine receives ONLY
candles with openTime <= step candle (sliced 4H/1H/15M frames), so signal
generation is exactly what it would have been live -- no lookahead by
construction. Outcomes are then evaluated with the subsequent candles (the
point of a backtest).

Usage:
    python -m scanner.replay --symbols BTCUSDT ETHUSDT --candles 700
    python -m scanner.replay --symbols BTCUSDT --determinism-check

Notes:
* Deterministic: same data + config -> byte-identical signals (verified by
  --determinism-check).
* Sample-size honesty: short replays produce few/no signals because the
  quality gate intentionally rejects most setups.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .config import Config
from .market_data import MarketDataClient, drop_incomplete
from .analysis import analyze_symbol
from .signals import generate_signals, ACTIVE_STATUSES
from .outcomes import update_outcomes
from .performance import compute_performance
from .main import make_client
from .persist import atomic_write_json, data_dir

WARMUP_15M = 240  # bars needed before the first evaluation step (EMA200 etc.)


def fetch_history(client: MarketDataClient, symbol: str, limits: dict) -> dict:
    frames = {}
    for tf in ("4h", "1h", "15m"):
        frames[tf] = drop_incomplete(client.klines(symbol, tf, limits.get(tf, 500)))
    return frames


def slice_frames(frames: dict, until_open_ms: int) -> dict:
    """Causal slice: only candles that had opened (and closed) by until_open_ms.

    A 4H candle whose openTime <= until_open_ms may still be unclosed at that
    moment; to stay strictly causal we require closeTime <= until_open_ms +
    its own duration is handled by drop_incomplete at replay time -- here we
    keep candles with closeTime < until_close_ms (end of the 15M step candle).
    """
    until_close_ms = until_open_ms + 900_000  # end of the step's 15m candle
    out = {}
    for tf, candles in frames.items():
        out[tf] = [c for c in candles if c.closeTime <= until_close_ms]
    return out


def replay_symbol(symbol: str, frames: dict, cfg: Config) -> list[dict]:
    """Walk the 15M series; generate + resolve signals exactly as live."""
    candles_15m = frames["15m"]
    signals: list[dict] = []
    start = max(WARMUP_15M, cfg.get("indicators.emaSlow", 200) + 40)
    for t in range(start, len(candles_15m)):
        step_candle = candles_15m[t]
        now_ms = step_candle.closeTime + 1
        sliced = slice_frames(frames, step_candle.openTime)
        analysis = analyze_symbol(symbol, sliced, cfg, now_ms)
        if not analysis["ok"]:
            continue
        new_signals, _rejects = generate_signals([analysis], cfg, signals, now_ms,
                                                 data_source="replay")
        signals.extend(new_signals)
        # advance lifecycle with candles closed by now (strictly causal)
        signals = update_outcomes(signals, {symbol: candles_15m}, now_ms,
                                  client=None, allow_1m=False)
    return signals


def run_replay(cfg: Config, symbols: list[str], candles: int, log=print) -> dict:
    client = make_client(cfg)
    limits = cfg.get("dataSource.klineLimits", {})
    limits = {k: max(v, candles) for k, v in limits.items()}
    limits["15m"] = max(limits.get("15m", 500), candles)
    results: list[dict] = []
    for sym in symbols:
        t0 = time.monotonic()
        frames = fetch_history(client, sym, limits)
        need = {"4h": cfg.get("indicators.emaSlow", 200) + 10,
                "1h": cfg.get("indicators.emaSlow", 200) + 10,
                "15m": WARMUP_15M + 10}
        short = [tf for tf, n in need.items() if len(frames.get(tf, [])) < n]
        if short:
            log(f"{sym}: skipped, insufficient history {short}")
            continue
        signals = replay_symbol(sym, frames, cfg)
        took = round(time.monotonic() - t0, 1)
        log(f"{sym}: replayed {len(frames['15m'])} 15m candles -> {len(signals)} signals ({took}s)")
        results.extend(signals)
    performance = compute_performance(results, cfg, int(time.time() * 1000))
    report = {
        "mode": "replay",
        "symbols": symbols,
        "candles": candles,
        "generatedAt": int(time.time() * 1000),
        "signals": results,
        "performance": performance,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="historical replay / backtest")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--candles", type=int, default=700, help="15m candles to replay")
    parser.add_argument("--config", default=None)
    parser.add_argument("--out", default=None, help="report path (default data/replay-report.json)")
    parser.add_argument("--determinism-check", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    report = run_replay(cfg, args.symbols, args.candles)
    if args.determinism_check:
        report2 = run_replay(cfg, args.symbols, args.candles, log=lambda *_: None)
        a = json.dumps(report["signals"], sort_keys=True)
        b = json.dumps(report2["signals"], sort_keys=True)
        if a != b:
            print("[DETERMINISM FAIL] replay produced different signals on identical data",
                  file=__import__("sys").stderr)
            return 1
        print("determinism check OK (identical output on identical input)")

    out = Path(args.out) if args.out else data_dir() / "replay-report.json"
    atomic_write_json(out, report)
    perf = report["performance"]
    print(f"replay report -> {out}")
    print(f"signals: {perf['total']}  wins: {perf['wins']}  losses: {perf['losses']}  "
          f"win rate: {perf['winRate']}% (n={perf['resolvedTrades']})  "
          f"profit factor: {perf['profitFactor']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
