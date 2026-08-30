"""Dynamic top-100 universe builder.

Rules (documented in README §Market Universe):
1. Retrieve active contracts from exchangeInfo.
2. Futures: keep ``contractType == PERPETUAL`` + ``quoteAsset == USDT`` +
   ``status == TRADING`` (spot fallback: TRADING + USDT quote).
3. Exclude inactive/delisted symbols (status != TRADING).
4. Exclude non-perpetual contracts (delivery futures) -- futures filter above.
5. Exclude leveraged-token style symbols (UP/DOWN/BULL/BEAR patterns).
6. Exclude stablecoin/stable-like base assets and precious-metal tokens.
7. Exclude very young listings (< universe.minListingAgeDays, when the API
   supplies an onboard/listing date) and extremely low 24h quote volume.
8. Rank by 24h quote volume (desc) and select the top ``maxSymbols``.

Nothing is hard-coded: the universe is recomputed from live API data on every
scan and the result is recorded in the scan metadata.
"""
from __future__ import annotations

import time

from .config import Config
from .market_data import MarketDataClient, MarketDataError


def _matches_exclusion(symbol: str, patterns: list[str]) -> bool:
    upper = symbol.upper()
    return any(p.upper() in upper for p in patterns)


def build_universe(client: MarketDataClient, cfg: Config, log) -> dict:
    """Return {symbols: [{symbol, quoteVolume, rank}], source, counts, warning?}."""
    ucfg = cfg.get("universe")
    patterns = ucfg.get("excludeSymbolPatterns", [])
    excluded_assets = {a.upper() for a in ucfg.get("excludeBaseAssets", [])}
    min_vol = float(ucfg.get("minQuoteVolume24h", 0))
    max_symbols = int(ucfg.get("maxSymbols", 100))
    min_age_days = int(ucfg.get("minListingAgeDays", 0))

    info = client.exchange_info()
    ep = client.endpoint_info()
    market = ep.get("market", "futures")
    now_ms = int(time.time() * 1000)

    candidates: dict[str, int | None] = {}  # symbol -> onboardDate ms (None = unknown)
    total_active = 0
    for s in info.get("symbols", []):
        symbol = s.get("symbol", "")
        status = s.get("status")
        if status != "TRADING":
            continue
        total_active += 1
        if s.get("quoteAsset") != "USDT":
            continue
        base = (s.get("baseAsset") or "").upper()
        if base in excluded_assets:
            continue
        if market == "futures":
            if s.get("contractType") != "PERPETUAL":       # rule 4: perps only
                continue
            if s.get("onboardDate") and min_age_days > 0:
                if now_ms - int(s.get("onboardDate")) < min_age_days * 86_400_000:
                    continue
        if _matches_exclusion(symbol, patterns):            # rule 5: leveraged tokens
            continue
        candidates[symbol] = None

    tickers = client.ticker_24h()
    volumes: list[tuple[str, float]] = []
    for t in tickers:
        symbol = t.get("symbol")
        if symbol not in candidates:
            continue
        try:
            qv = float(t.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qv <= 0 or qv != qv:
            continue
        volumes.append((symbol, qv))

    # rule 7: liquidity floor, then rank by 24h quote volume (rule 8)
    volumes = [(s, v) for s, v in volumes if v >= min_vol]
    volumes.sort(key=lambda x: (-x[1], x[0]))  # deterministic tie-break by symbol
    selected = volumes[:max_symbols]

    universe = {
        "symbols": [{"symbol": s, "quoteVolume": round(v, 2), "rank": i + 1}
                    for i, (s, v) in enumerate(selected)],
        "source": ep.get("name"),
        "market": market,
        "generatedAt": now_ms,
        "counts": {
            "activeContracts": total_active,
            "usdtPerpCandidates": len(candidates),
            "afterVolumeFloor": len(volumes),
            "selected": len(selected),
        },
    }
    if market != "futures":
        universe["warning"] = (
            "Futures API endpoints unavailable from this host; using the Binance "
            "spot market-data mirror as a degraded data source. Signals remain "
            "real market data but reflect spot order flow."
        )
        log.warn(universe["warning"])
    log.info(f"universe: {len(selected)} symbols selected from "
             f"{len(candidates)} USDT perp candidates ({ep.get('name')})")
    return universe


def symbol_list(universe: dict) -> list[str]:
    return [entry["symbol"] for entry in universe.get("symbols", [])]
