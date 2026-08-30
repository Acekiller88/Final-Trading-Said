"""JSON persistence: atomic writes to /data (source of truth) mirrored into
/frontend/data for static hosting on Cloudflare Pages. Retention applied to
market snapshots and logs; signal history is kept forever (persistent).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .config import repo_root

DATA_FILES = ("signals.json", "performance.json", "system-status.json", "market-snapshots.json")


def data_dir() -> Path:
    return repo_root() / "data"


def frontend_data_dir() -> Path:
    return repo_root() / "frontend" / "data"


def load_json(name: str, default):
    path = data_dir() / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_data_file(name: str, payload) -> None:
    atomic_write_json(data_dir() / name, payload)
    atomic_write_json(frontend_data_dir() / name, payload)


def seed_empty_files() -> None:
    defaults = {
        "signals.json": {"generatedAt": 0, "signals": []},
        "performance.json": {"generatedAt": 0},
        "system-status.json": {"systemOnline": False, "health": "INITIALISING",
                               "lastSuccessfulScan": None, "logs": []},
        "market-snapshots.json": [],
    }
    for name, payload in defaults.items():
        if not (data_dir() / name).exists():
            write_data_file(name, payload)
