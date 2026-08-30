"""Performance engine (spec §21-22).

Win Rate = Wins / (Wins + Losses). WAITING / EXPIRED / AMBIGUOUS / CANCELLED
are NEVER in the denominator, and every displayed metric carries its sample
size. Profit factor = sum(R of wins) / |sum(R of losses)| where a loss is
-1R by definition of a structure stop.
"""
from __future__ import annotations

from collections import defaultdict

from .signals import (WAITING_TRIGGER, TRIGGERED, WIN, LOSS, EXPIRED,
                      AMBIGUOUS, CANCELLED)


def _pct(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _streaks(results: list[str]) -> tuple[int, int]:
    max_w = max_l = cur_w = cur_l = 0
    for r in results:
        if r == WIN:
            cur_w += 1
            cur_l = 0
        elif r == LOSS:
            cur_l += 1
            cur_w = 0
        else:
            cur_w = cur_l = 0
        max_w = max(max_w, cur_w)
        max_l = max(max_l, cur_l)
    return max_w, max_l


def _duration_ms(sig: dict, from_ms: int, to_ms: int) -> int | None:
    start = sig.get(from_ms)
    end = sig.get(to_ms)
    if start and end and end >= start:
        return end - start
    return None


def _bucket_by_score(score: float, cfg) -> str:
    s = cfg.get("scoring", {})
    if score >= s.get("aPlusThreshold", 90):
        return "A+"
    if score >= s.get("aThreshold", 85):
        return "A"
    if score >= s.get("bPlusThreshold", 80):
        return "B+"
    return "below B+"


def _agg(items: list[dict]) -> dict:
    wins = [s for s in items if s["status"] == WIN]
    losses = [s for s in items if s["status"] == LOSS]
    r_wins = [s.get("rMultiple") for s in wins if s.get("rMultiple") is not None]
    r_losses = [s.get("rMultiple") for s in losses if s.get("rMultiple") is not None]
    gross_win = sum(r_wins)
    gross_loss = abs(sum(r_losses))
    tp_times = [s["closedAt"] - s["triggeredAt"] for s in wins
                if s.get("triggeredAt") and s.get("closedAt")]
    sl_times = [s["closedAt"] - s["triggeredAt"] for s in losses
                if s.get("triggeredAt") and s.get("closedAt")]
    return {
        "total": len(items),
        "waiting": sum(1 for s in items if s["status"] == WAITING_TRIGGER),
        "triggered": sum(1 for s in items if s["status"] in (TRIGGERED, WIN, LOSS, AMBIGUOUS, CANCELLED)),
        "wins": len(wins),
        "losses": len(losses),
        "expired": sum(1 for s in items if s["status"] == EXPIRED),
        "ambiguous": sum(1 for s in items if s["status"] == AMBIGUOUS),
        "cancelled": sum(1 for s in items if s["status"] == CANCELLED),
        "winRate": _pct(len(wins), len(wins) + len(losses)),
        "resolvedTrades": len(wins) + len(losses),
        "avgRr": round(sum(s["riskReward"] for s in items) / len(items), 3) if items else None,
        "avgWinR": round(gross_win / len(r_wins), 3) if r_wins else None,
        "profitFactor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,  # undefined until a loss occurs
        "avgScore": round(sum(s["score"] for s in items) / len(items), 1) if items else None,
        "avgTimeToTpMs": round(sum(tp_times) / len(tp_times)) if tp_times else None,
        "avgTimeToSlMs": round(sum(sl_times) / len(sl_times)) if sl_times else None,
    }


def compute_performance(signals: list[dict], cfg, now_ms: int,
                        since_ms: int | None = None,
                        until_ms: int | None = None) -> dict:
    """Aggregate metrics; optional [since, until] window on generatedAt."""
    window = [s for s in signals
              if (since_ms is None or s.get("generatedAt", 0) >= since_ms)
              and (until_ms is None or s.get("generatedAt", 0) <= until_ms)]

    base = _agg(window)
    resolved = [s for s in window if s["status"] in (WIN, LOSS)]
    resolved.sort(key=lambda s: s.get("closedAt") or s.get("generatedAt", 0))
    max_w, max_l = _streaks([s["status"] for s in resolved])
    base.update({"maxWinningStreak": max_w, "maxLosingStreak": max_l})

    def breakdown(key_fn) -> dict:
        groups: dict[str, list[dict]] = defaultdict(list)
        for s in window:
            groups[key_fn(s)].append(s)
        return {k: _agg(v) for k, v in sorted(groups.items())}

    base["byDirection"] = breakdown(lambda s: s["direction"])
    base["byQuality"] = breakdown(lambda s: s.get("quality", _bucket_by_score(s.get("score", 0), cfg)))
    base["byRegime"] = breakdown(lambda s: s.get("marketRegime", "unknown"))
    base["bySymbol"] = breakdown(lambda s: s["symbol"])
    base["byScoreRange"] = breakdown(lambda s: _bucket_by_score(s.get("score", 0), cfg))
    base["window"] = {"since": since_ms, "until": until_ms, "signalsInWindow": len(window)}
    base["generatedAt"] = now_ms
    base["disclaimer"] = (
        "Win rate = wins / (wins + losses); waiting/expired/ambiguous/cancelled "
        "signals are excluded from the denominator. The confluence score is a "
        "quality ranking, not a probability. Past performance does not "
        "guarantee future results."
    )
    return base
