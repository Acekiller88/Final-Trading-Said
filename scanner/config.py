"""Centralised configuration loader.

Loads /config/strategy.json, deep-merges it over safe built-in defaults so a
partially specified file never crashes the engine, and exposes dotted-path
lookups. All strategy parameters live here -- never hard-coded in the engine.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "scan": {"intervalMinutes": 15, "timezone": "Asia/Kuala_Lumpur"},
    "dataSource": {
        "failoverEndpoints": [
            {"name": "binance-futures", "market": "futures", "base": "https://fapi.binance.com/fapi/v1"},
        ],
        "requestTimeoutSeconds": 12,
        "maxRetries": 3,
        "retryBackoffSeconds": 1.5,
        "maxRequestsPerScan": 700,
        "klineLimits": {"4h": 260, "1h": 300, "15m": 500},
    },
    "universe": {
        "maxSymbols": 100,
        "minQuoteVolume24h": 3_000_000,
        "excludeSymbolPatterns": ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"],
        "excludeBaseAssets": ["USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDP", "AEUR", "EUR", "PAXG"],
        "minListingAgeDays": 30,
    },
    "indicators": {
        "emaFast": 20, "emaMid": 50, "emaSlow": 200,
        "rsiPeriod": 14, "adxPeriod": 14, "atrPeriod": 14,
        "relVolumeLookback": 20, "vwapWindow": 48,
    },
    "structure": {
        "swingLookback": 2, "minSwingAgeBars": 3, "sweepMaxAtrMultiple": 1.0,
        "displacementBodyAtrMultiple": 1.5, "equalLevelAtrTolerance": 0.10,
        "maxStructureSwings": 40,
    },
    "smc": {"fvgMinGapAtrMultiple": 0.10, "obLookback": 10, "setupWindowBars": 12},
    "signalModel": {
        "rsiLongMin": 50, "rsiLongMax": 72, "rsiShortMin": 28, "rsiShortMax": 50,
        "minAdx15m": 18, "minRelVolume": 1.20, "minAtrPercent": 0.10, "maxAtrPercent": 3.00,
    },
    "risk": {
        "minRr": 2.5, "preferredRr": 3.0,
        "stopBufferAtrMultiple": 0.5, "triggerBufferAtrMultiple": 0.10,
        "entryZoneAtrMultiple": 0.5, "maxStopAtrMultiple": 3.0,
        "maxRunupAtrMultiple": 3.0, "minStructureRoomAtrMultiple": 1.0,
    },
    "scoring": {
        "minScore": 80, "aPlusThreshold": 90, "aThreshold": 85, "bPlusThreshold": 80,
        "weights": {
            "htfAlignment": 20, "marketStructure": 20, "liquiditySmc": 20,
            "momentum": 15, "volume": 10, "volatility": 5, "riskReward": 10,
        },
    },
    "lifecycle": {"triggerExpiryCandles": 12, "tradeExpiryCandles": 16, "resolveAmbiguousWith1m": True, "candleMs": 900_000},
    "dedupe": {"symbolCooldownMinutes": 240, "sameDirectionOnly": True, "maxActiveSignalsTotal": 12},
    "retention": {"marketSnapshots": 288, "logEntries": 200},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Read-only accessor over the merged strategy configuration."""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = _deep_merge(DEFAULTS, data or {})
        self.path = path

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else repo_root() / "config" / "strategy.json"
        data: dict[str, Any] = {}
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
        return cls(data, p)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None:
            raise KeyError(f"missing config key: {dotted}")
        return value

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def validate(self) -> list[str]:
        errors: list[str] = []
        s = self.get("scoring", {})
        if not (0 < s.get("minScore", 0) <= 100):
            errors.append("scoring.minScore must be within 1..100")
        if not (s.get("bPlusThreshold", 0) <= s.get("aThreshold", 0) <= s.get("aPlusThreshold", 0)):
            errors.append("scoring thresholds must satisfy bPlus <= A <= A+")
        risk = self.get("risk", {})
        if risk.get("minRr", 0) <= 0 or risk.get("preferredRr", 0) < risk.get("minRr", 0):
            errors.append("risk.minRr/preferredRr invalid")
        endpoints = self.get("dataSource.failoverEndpoints", [])
        if not endpoints:
            errors.append("dataSource.failoverEndpoints must not be empty")
        return errors


def repo_root() -> Path:
    """Repository root = parent of the ``scanner`` package directory."""
    return Path(__file__).resolve().parents[1]
