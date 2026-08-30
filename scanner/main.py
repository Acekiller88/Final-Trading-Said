"""Scanner entry point.

Local:   python -m scanner.main [--max-symbols 20] [--symbols BTCUSDT ...]
CI:      python -m scanner.main            (full 100-symbol scan, writes JSON)

Failure policy (spec §38): if Binance data cannot be retrieved the scanner
marks the scan degraded/failed, logs the failure, RETAINS the previous valid
dashboard data, and never fabricates signals. Exit codes: 0 = ok/degraded
partial, 1 = hard failure.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .config import Config, repo_root
from .market_data import (MarketDataClient, MarketDataError,
                          drop_incomplete, Candle)
from .universe import build_universe, symbol_list
from .analysis import analyze_symbol
from .signals import generate_signals, ACTIVE_STATUSES
from .outcomes import update_outcomes
from .performance import compute_performance
from .validation import validate_signal, validate_signals_payload, check_immutability
from . import persist


class ScanLog:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def _add(self, level: str, msg: str) -> None:
        entry = {"ts": int(time.time() * 1000), "level": level, "msg": msg}
        self.entries.append(entry)
        print(f"[{level.upper()}] {msg}", flush=True)

    def info(self, msg: str) -> None: self._add("info", msg)
    def warn(self, msg: str) -> None: self._add("warn", msg)
    def error(self, msg: str) -> None: self._add("error", msg)

    def tail(self, n: int) -> list[dict]:
        return self.entries[-n:]


def _scheduled_now(interval_min: int) -> int:
    now = int(time.time() * 1000)
    interval_ms = interval_min * 60_000
    return now - (now % interval_ms)


def make_client(cfg: Config) -> MarketDataClient:
    ds = cfg.get("dataSource", {})
    return MarketDataClient(
        endpoints=ds.get("failoverEndpoints", []),
        timeout=ds.get("requestTimeoutSeconds", 12),
        max_retries=ds.get("maxRetries", 3),
        backoff=ds.get("retryBackoffSeconds", 1.5),
        max_requests=ds.get("maxRequestsPerScan", 700),
    )


def collect_klines(client: MarketDataClient, symbols: list[str], cfg: Config, log: ScanLog):
    limits = cfg.get("dataSource.klineLimits", {})
    ok: dict[str, dict] = {}
    failed: list[str] = []
    candle_map: dict[str, list[Candle]] = {}
    for i, sym in enumerate(symbols):
        frames: dict[str, list[Candle]] = {}
        try:
            for tf in ("4h", "1h", "15m"):
                raw = client.klines(sym, tf, limits.get(tf, 300))
                closed = drop_incomplete(raw)
                frames[tf] = closed
            ok[sym] = frames
            candle_map[sym] = frames["15m"]
        except MarketDataError as exc:
            failed.append(sym)
            log.warn(f"klines failed for {sym}: {exc}")
        if i % 20 == 19:
            log.info(f"klines collected for {i + 1}/{len(symbols)} symbols")
        time.sleep(0.03)  # politeness delay
    return ok, failed, candle_map


def scan_once(cfg: Config, log: ScanLog, client: MarketDataClient | None = None,
              max_symbols: int | None = None, symbols: list[str] | None = None,
              dry_run: bool = False) -> int:
    t0 = time.monotonic()
    now_ms = int(time.time() * 1000)
    scheduled_ms = _scheduled_now(cfg.get("scan.intervalMinutes", 15))
    client = client or make_client(cfg)

    previous_signals_payload = persist.load_json("signals.json", {"generatedAt": 0, "signals": []})
    previous_signals: list[dict] = previous_signals_payload.get("signals", [])
    previous_status = persist.load_json("system-status.json", {})
    previous_logs: list[dict] = list(previous_status.get("logs", []))[-cfg.get("retention.logEntries", 200):]
    snapshots: list = persist.load_json("market-snapshots.json", [])

    log.info(f"scan start v{__version__} scheduled={scheduled_ms} actual={now_ms} "
             f"(jitter {(now_ms - scheduled_ms) / 1000:.1f}s)")

    # ---------------------------------------------------- universe & data
    universe: dict | None = None
    failure_reason: str | None = None
    try:
        if symbols:
            universe = {"symbols": [{"symbol": s, "quoteVolume": 0, "rank": i + 1}
                                    for i, s in enumerate(symbols)],
                        "source": "manual", "market": "manual", "generatedAt": now_ms,
                        "counts": {"selected": len(symbols)}}
            scan_symbols = list(symbols)
        else:
            universe = build_universe(client, cfg, log)
            scan_symbols = symbol_list(universe)
            if max_symbols:
                scan_symbols = scan_symbols[:max_symbols]
    except MarketDataError as exc:
        log.error(f"universe build failed: {exc}")
        failure_reason = f"universe build failed: {exc}"
        scan_symbols = []

    stats = client.stats.as_dict()
    ep = client.endpoint_info()
    stats.update(endpointUsed=ep.get("name"), marketUsed=ep.get("market"))

    if failure_reason or not scan_symbols:
        status = _write_failed_status(log, previous_status, previous_logs, now_ms, scheduled_ms,
                                      stats, t0, failure_reason or "empty universe")
        if not dry_run:
            persist.write_data_file("system-status.json", status)
        return 1

    klines, failed_symbols, candle_map = collect_klines(client, scan_symbols, cfg, log)
    analyses = []
    for sym, frames in klines.items():
        a = analyze_symbol(sym, frames, cfg, now_ms)
        if a["ok"]:
            analyses.append(a)
        else:
            log.warn(f"{sym}: {a['reasons'][0]}")
    stats = client.stats.as_dict()
    ep = client.endpoint_info()
    stats.update(endpointUsed=ep.get("name"), marketUsed=ep.get("market"))

    if not analyses:
        log.error("no valid symbol data -- marking scan FAILED, retaining previous data")
        status = _write_failed_status(log, previous_status, previous_logs, now_ms, scheduled_ms,
                                      stats, t0, "no valid market data")
        if not dry_run:
            persist.write_data_file("system-status.json", status)
        return 1

    # ---------------------------------------------------- signals & outcomes
    new_signals, rejects = generate_signals(analyses, cfg, previous_signals, now_ms,
                                            data_source=universe.get("source"))
    reject_counts: dict[str, int] = {}
    for r in rejects:
        reject_counts[r["reason"]] = reject_counts.get(r["reason"], 0) + 1
    for reason, count in sorted(reject_counts.items(), key=lambda kv: -kv[1]):
        log.info(f"rejected {count}x: {reason}")
    for sig in new_signals:
        log.info(f"SIGNAL {sig['symbol']} {sig['direction']} {sig['quality']} "
                 f"score={sig['score']} trigger={sig['triggerPrice']} "
                 f"TP={sig['takeProfit']} SL={sig['stopLoss']} RR={sig['riskReward']}")
    if not new_signals:
        log.info("no qualifying setups this scan -- zero signals generated")

    allow_1m = bool(cfg.get("lifecycle.resolveAmbiguousWith1m", True))
    merged_previous = update_outcomes(previous_signals, candle_map, now_ms, client, allow_1m)

    # immutability guard: revert any accidental mutation of historical fields
    immutability_errors = check_immutability(previous_signals, merged_previous)
    for err in immutability_errors:
        log.error(f"immutability violation reverted: {err}")
    if immutability_errors:
        old_by_id = {s["id"]: s for s in previous_signals}
        merged_previous = [old_by_id.get(s["id"], s) for s in merged_previous]

    # validate new signals before publishing; invalid ones never reach the dashboard
    published_new: list[dict] = []
    for sig in new_signals:
        errs = validate_signal(sig, cfg)
        if errs:
            log.error(f"invalid new signal {sig['symbol']} dropped: {errs}")
        else:
            published_new.append(sig)

    all_signals = merged_previous + published_new
    payload_errors = validate_signals_payload({"signals": all_signals}, cfg, now_ms)
    for err in payload_errors:
        log.error(f"payload validation: {err}")

    performance = compute_performance(all_signals, cfg, now_ms)

    # ---------------------------------------------------- status & snapshots
    duration_ms = int((time.monotonic() - t0) * 1000)
    freshness = _freshness(analyses, now_ms)
    health = "HEALTHY"
    if failed_symbols or universe.get("market") not in ("futures", "manual") or freshness["label"] != "LIVE":
        health = "DEGRADED"
    api_health = "HEALTHY" if stats["errors"] == 0 else (
        "DEGRADED" if stats["errors"] < len(scan_symbols) else "FAILED")

    breadth = {"TRENDING_UP": 0, "TRENDING_DOWN": 0, "RANGING": 0, "MIXED": 0}
    for a in analyses:
        breadth[a["regime"]] = breadth.get(a["regime"], 0) + 1

    status = {
        "systemOnline": True,
        "health": health,
        "version": __version__,
        "lastScan": {
            "scheduledAt": scheduled_ms,
            "executedAt": now_ms,
            "jitterSeconds": round((now_ms - scheduled_ms) / 1000, 1),
            "durationMs": duration_ms,
            "status": "OK" if health == "HEALTHY" else "DEGRADED",
            "universeSize": universe.get("counts", {}).get("selected", len(scan_symbols)),
            "universeSource": universe.get("source"),
            "market": universe.get("market"),
            "symbolsScanned": len(scan_symbols),
            "symbolsValid": len(analyses),
            "dataFailures": len(failed_symbols),
            "failedSymbols": failed_symbols[:25],
            "candidates": len(new_signals) + sum(1 for r in rejects if "risk model" in r["reason"] or "score" in r["reason"]),
            "signalsGenerated": len(published_new),
            "rejects": reject_counts,
            "apiStats": stats,
            "apiHealth": api_health,
            "dataFreshness": freshness,
            "activeSignals": sum(1 for s in all_signals if s["status"] in ACTIVE_STATUSES),
        },
        "lastSuccessfulScan": now_ms,
        "nextExpectedScanAt": scheduled_ms + cfg.get("scan.intervalMinutes", 15) * 60_000,
        "logs": (previous_logs + log.entries)[-cfg.get("retention.logEntries", 200):],
    }
    snapshot = {
        "ts": now_ms, "universeSize": universe.get("counts", {}).get("selected"),
        "source": universe.get("source"), "breadth": breadth,
        "activeSignals": status["lastScan"]["activeSignals"],
        "signalsGenerated": len(published_new),
        "avgScore": round(sum(s["score"] for s in published_new) / len(published_new), 1) if published_new else None,
        "topVolume": universe.get("symbols", [])[:10],
    }
    snapshots.append(snapshot)
    snapshots = snapshots[-cfg.get("retention.marketSnapshots", 288):]

    if dry_run:
        log.info("dry run -- no files written")
        return 0

    persist.write_data_file("signals.json", {"generatedAt": now_ms, "signals": all_signals})
    persist.write_data_file("performance.json", performance)
    persist.write_data_file("system-status.json", status)
    persist.write_data_file("market-snapshots.json", snapshots)
    log.info(f"SCAN COMPLETE {len(scan_symbols)} symbols scanned, {len(analyses)} valid, "
             f"{len(failed_symbols)} data failures, {len(published_new)} signals generated "
             f"({duration_ms} ms)")
    global _STATUS
    _STATUS = status
    return 0


def _write_failed_status(log: ScanLog, previous_status: dict, previous_logs: list,
                         now_ms: int, scheduled_ms: int, stats: dict, t0, reason: str) -> dict:
    log.error("scan FAILED -- retaining previous dashboard data")
    status = {
        "systemOnline": False,
        "health": "FAILED",
        "lastScan": {
            "scheduledAt": scheduled_ms, "executedAt": now_ms,
            "durationMs": int((time.monotonic() - t0) * 1000), "status": "FAILED",
            "reason": reason, "apiStats": stats,
        },
        "lastSuccessfulScan": previous_status.get("lastSuccessfulScan"),
        "nextExpectedScanAt": scheduled_ms + 15 * 60_000,
        "logs": (previous_logs + log.entries)[-200:],
    }
    global _STATUS
    _STATUS = status
    return status


def _freshness(analyses: list[dict], now_ms: int) -> dict:
    newest = max((a.get("last15mCloseTime") or 0) for a in analyses)
    age = now_ms - newest if newest else None
    if age is None:
        label = "UNKNOWN"
    elif age <= 16 * 60_000:
        label = "LIVE"
    elif age <= 30 * 60_000:
        label = "DELAYED"
    else:
        label = "STALE"
    return {"label": label, "newestCandleCloseMs": newest,
            "ageSeconds": round(age / 1000, 1) if age is not None else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="15-minute crypto signal scanner")
    parser.add_argument("--config", default=None, help="path to strategy.json")
    parser.add_argument("--symbols", nargs="*", help="override universe (local testing)")
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--json-output", action="store_true", help="print result JSON")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    errs = cfg.validate()
    if errs:
        for e in errs:
            print(f"[CONFIG ERROR] {e}", file=sys.stderr)
        return 1
    if args.validate_only:
        from .validate_data import main as validate_main
        return validate_main()

    persist.seed_empty_files()
    log = ScanLog()
    code = scan_once(cfg, log, symbols=args.symbols,
                     max_symbols=args.max_symbols, dry_run=args.dry_run)
    if args.json_output:
        print(json.dumps(_STATUS, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
