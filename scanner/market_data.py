"""Binance public market-data client (stdlib only -- zero dependencies).

* Public market data ONLY. No API key, no account endpoints, no trading.
* Automatic endpoint failover: the canonical ``fapi.binance.com`` host is
  geo-blocked (HTTP 451) from some clouds (e.g. certain GitHub Actions / US
  IPs). The configured failover chain first retries the canonical futures
  host, then a futures API mirror, then -- as a clearly flagged degraded
  fallback -- the official Binance spot market-data mirror.
* Retries with exponential backoff on timeouts / 5xx / rate limits (429).
* Deterministic client errors (invalid symbol / delisted) raise immediately
  without retry or failover.
* Never fabricates data: on unrecoverable failure the caller receives an
  exception and must mark the scan degraded/failed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1m": 60_000}


class MarketDataError(Exception):
    """Unrecoverable market-data failure after retries/failover."""

    def __init__(self, message: str, rate_limited: bool = False, client_error: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.client_error = client_error


class SymbolUnavailableError(MarketDataError):
    """Symbol delisted / temporarily unavailable (deterministic 4xx)."""

    def __init__(self, message: str):
        super().__init__(message, client_error=True)


@dataclass
class Candle:
    openTime: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closeTime: int
    quoteVolume: float
    trades: int

    @property
    def closed(self) -> bool:
        return self.closeTime < int(time.time() * 1000)


def _to_f(value) -> float:
    out = float(value)
    if out != out or out in (float("inf"), float("-inf")):
        raise ValueError("non-finite price")
    return out


def parse_klines(rows: list) -> list[Candle]:
    """Parse Binance kline rows into Candle objects; drop malformed rows."""
    out: list[Candle] = []
    for row in rows:
        try:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            candle = Candle(
                openTime=int(row[0]),
                open=_to_f(row[1]), high=_to_f(row[2]),
                low=_to_f(row[3]), close=_to_f(row[4]),
                volume=_to_f(row[5]),
                closeTime=int(row[6]),
                quoteVolume=_to_f(row[7]),
                trades=int(row[8]) if len(row) > 8 and str(row[8]) != "" else 0,
            )
            if not (candle.low <= candle.high and candle.low > 0):
                continue
            if candle.openTime <= 0 or candle.closeTime <= candle.openTime:
                continue
            out.append(candle)
        except (ValueError, TypeError, IndexError):
            continue
    out.sort(key=lambda c: c.openTime)
    return out


def drop_incomplete(candles: list[Candle], now_ms: int | None = None) -> list[Candle]:
    """Return only fully-closed candles (closed when closeTime has passed).

    This is the first line of defence against lookahead: signal logic never
    sees an in-progress candle.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return [c for c in candles if c.closeTime < now]


@dataclass
class ClientStats:
    requests: int = 0
    errors: int = 0
    retries: int = 0
    rateLimitHits: int = 0
    lastError: str | None = None
    endpointUsed: str | None = None
    marketUsed: str | None = None

    def as_dict(self) -> dict:
        return {
            "requests": self.requests, "errors": self.errors, "retries": self.retries,
            "rateLimitHits": self.rateLimitHits, "lastError": self.lastError,
            "endpointUsed": self.endpointUsed, "marketUsed": self.marketUsed,
        }


class MarketDataClient:
    """Small resilient GET client for Binance public market data."""

    def __init__(self, endpoints: list[dict], timeout: float = 12.0,
                 max_retries: int = 3, backoff: float = 1.5, max_requests: int = 700):
        if not endpoints:
            raise ValueError("at least one endpoint required")
        self.endpoints = endpoints
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.max_requests = max_requests
        self.stats = ClientStats()
        self._endpoint_idx = 0

    # ------------------------------------------------------------------ core
    def _get(self, path: str, params: dict | None = None) -> dict | list:
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        last_exc: Exception | None = None
        for hop in range(len(self.endpoints)):
            idx = (self._endpoint_idx + hop) % len(self.endpoints)
            ep = self.endpoints[idx]
            url = ep["base"].rstrip("/") + path + query
            for attempt in range(self.max_retries + 1):
                if self.stats.requests >= self.max_requests:
                    raise MarketDataError("request budget exhausted for this scan")
                self.stats.requests += 1
                try:
                    result = self._fetch(url)
                    self._endpoint_idx = idx
                    return result
                except SymbolUnavailableError:
                    self.stats.errors += 1
                    raise  # deterministic failure: retrying/failover cannot help
                except MarketDataError as exc:
                    last_exc = exc
                    self.stats.errors += 1
                    self.stats.lastError = str(exc)
                    if exc.rate_limited:
                        self.stats.rateLimitHits += 1
                    if attempt < self.max_retries:
                        self.stats.retries += 1
                        time.sleep(self.backoff * (attempt + 1) * (2.0 if exc.rate_limited else 1.0))
        raise MarketDataError(f"all endpoints failed: {last_exc}")

    def _fetch(self, url: str) -> dict | list:
        request = urllib.request.Request(url, headers={
            "User-Agent": "crypto-signal-scanner/1.0 (public market data only)",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            if exc.code in (429, 418):
                raise MarketDataError(f"HTTP {exc.code} rate limited: {body}", rate_limited=True) from exc
            if exc.code in (400, 404, 422):
                raise SymbolUnavailableError(f"HTTP {exc.code}: {body}") from exc
            if exc.code == 451:
                raise MarketDataError(f"HTTP 451 geo-restricted host={url.split('//')[1].split('/')[0]}") from exc
            raise MarketDataError(f"HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketDataError(f"network error: {exc}") from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MarketDataError(f"malformed JSON: {exc}") from exc
        if isinstance(data, dict) and data.get("code") not in (None, 0, 200):
            if data.get("code") == -1121:
                raise SymbolUnavailableError(str(data.get("msg", "invalid symbol")))
            raise MarketDataError(f"api error {data.get('code')}: {data.get('msg')}")
        return data

    # --------------------------------------------------------------- api surface
    def exchange_info(self) -> dict:
        return self._get("/exchangeInfo")

    def ticker_24h(self) -> list:
        data = self._get("/ticker/24hr")
        if not isinstance(data, list):
            raise MarketDataError("ticker/24hr returned unexpected shape")
        return data

    def klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval {interval}")
        rows = self._get("/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(rows, list):
            raise MarketDataError("klines returned unexpected shape")
        return parse_klines(rows)

    def klines_since(self, symbol: str, interval: str, start_ms: int) -> list[Candle]:
        """Fetch candles from start_ms onward (used by the outcome engine)."""
        rows = self._get("/klines", {"symbol": symbol, "interval": interval,
                                     "startTime": start_ms, "limit": 1000})
        if not isinstance(rows, list):
            raise MarketDataError("klines returned unexpected shape")
        return parse_klines(rows)

    def endpoint_info(self) -> dict:
        ep = self.endpoints[self._endpoint_idx]
        return {"name": ep.get("name"), "market": ep.get("market"), "base": ep.get("base")}
