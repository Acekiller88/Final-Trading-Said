"""Build frontend/dashboard-offline.html — fully self-contained dashboard.

Embeds the latest /data JSON snapshot directly into a single HTML file so the
dashboard can be opened by double-click from disk (file://), from a USB stick,
or inside sandboxed previews — no server, no network fetches for data. Live
prices from Binance are still attempted in-browser and degrade gracefully.

Rebuilt automatically on every scan (see .github/workflows/scanner.yml), so
downloading the latest copy from GitHub always gives current data.

    python -m scanner.build_offline
"""
from __future__ import annotations

import json
import re
import time

from .config import repo_root


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def build() -> None:
    fe = repo_root() / "frontend"
    data = repo_root() / "data"
    html = (fe / "index.html").read_text(encoding="utf-8")
    css = (fe / "styles.css").read_text(encoding="utf-8")
    js = (fe / "app.js").read_text(encoding="utf-8")

    payload = {
        "builtAt": int(time.time() * 1000),
        "signals": _read_json(data / "signals.json", {"generatedAt": 0, "signals": []}),
        "status": _read_json(data / "system-status.json", None),
        "snapshots": _read_json(data / "market-snapshots.json", []),
        "performance": _read_json(data / "performance.json", None),
    }
    # safe for embedding inside a <script> tag
    blob = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")

    banner = (
        '<div style="margin:0;padding:9px 16px;background:#3b2f11;color:#ffd166;'
        "font:600 12px/1.4 'Segoe UI',system-ui,sans-serif;text-align:center;\">"
        "OFFLINE SNAPSHOT MODE — data embedded at build time. Live prices still "
        "refresh in-browser when reachable. Host over HTTP for auto-refreshing data.</div>"
    )
    html = html.replace("<body>", "<body>\n" + banner, 1)

    style_block = "<style>\n" + css.replace("</", "<\\/") + "\n</style>"
    html = re.sub(r'<link rel="stylesheet" href="styles\.css">',
                  lambda _m: style_block, html, count=1)

    inject = ("<script>window.__EMBEDDED_DATA__ = " + blob + ";</script>\n"
              "<script>\n" + js.replace("</script>", "<\\/script>") + "\n</script>")
    html = re.sub(r'<script src="app\.js"></script>', lambda _m: inject, html, count=1)

    (fe / "dashboard-offline.html").write_text(html, encoding="utf-8")
    print(f"dashboard-offline.html built ({len(html) // 1024} KB, "
          f"{len(payload['signals'].get('signals', []))} signals embedded)")


if __name__ == "__main__":
    build()
