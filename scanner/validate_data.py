"""Standalone JSON validation CLI (used by the GitHub Actions workflow).

    python -m scanner.validate_data

Exits non-zero if any /data JSON file is unparseable or if the signals
payload fails integrity validation. Invalid data must never reach the
dashboard/commit stage.
"""
from __future__ import annotations

import json
import sys

from .config import Config, repo_root
from .validation import validate_signals_payload
from .signals import ACTIVE_STATUSES


def main() -> int:
    cfg = Config.load()
    errors: list[str] = []
    data = repo_root() / "data"
    files = ["signals.json", "performance.json", "system-status.json", "market-snapshots.json"]
    payloads: dict[str, object] = {}
    for name in files:
        path = data / name
        if not path.exists():
            errors.append(f"{name}: missing")
            continue
        try:
            payloads[name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{name}: invalid JSON ({exc})")

    if "signals.json" in payloads:
        import time
        errs = validate_signals_payload(payloads["signals.json"], cfg, int(time.time() * 1000))
        errors.extend(f"signals.json: {e}" for e in errs)
        signals = payloads["signals.json"].get("signals", [])
        statuses = [s.get("status") for s in signals]
        print(f"signals.json: {len(signals)} signals, "
              f"{sum(1 for s in statuses if s in ACTIVE_STATUSES)} active")
        allowed = {"WAITING_TRIGGER", "TRIGGERED", "WIN", "LOSS", "EXPIRED", "AMBIGUOUS", "CANCELLED"}
        bad = [s for s in statuses if s not in allowed]
        if bad:
            errors.append(f"signals.json: unknown statuses {sorted(set(bad))}")

    if "system-status.json" in payloads:
        st = payloads["system-status.json"]
        if not isinstance(st.get("health"), str):
            errors.append("system-status.json: health missing")

    if errors:
        for e in errors:
            print(f"[VALIDATION ERROR] {e}", file=sys.stderr)
        return 1
    print("data validation OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
