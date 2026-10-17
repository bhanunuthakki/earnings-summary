"""Panel latency telemetry and activation-count aggregate routes."""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime
from typing import cast

from comments_server_context import ServerContext
from flask import Flask, request


def register_panel_metrics_routes(app: Flask, ctx: ServerContext) -> None:
    bump_activation_count = ctx.bump_activation_count
    open_db = ctx.open_db
    activation_cache_modes = ctx.activation_cache_modes
    metric_cache_modes = ctx.metric_cache_modes
    panel_metrics = ctx.panel_metrics

    @app.route("/api/metrics/panel", methods=["POST", "OPTIONS"])
    def panel_metrics_post():
        """Record one client-side panel timing sample:
        ``{panel, cache, fetch_ms, render_ms, total_ms, status?}``.
        Fire-and-forget from the shell — always 204 on accepted shape."""
        if request.method == "OPTIONS":
            return ("", 204)
        payload = cast("dict[str, object]", request.get_json(silent=True) or {})
        panel = str(payload.get("panel") or "").strip()
        cache = str(payload.get("cache") or "").strip()
        if not panel or cache not in metric_cache_modes:
            return ({"error": "panel + cache (cold|swr|prefetch|revalidate) required"}, 400)

        def _ms(key: str) -> float | None:
            value = payload.get(key)
            if isinstance(value, (int, float)) and 0 <= float(value) < 600_000:
                return round(float(value), 1)
            return None

        status_raw = payload.get("status")
        panel_metrics.append(
            {
                "panel": panel[:40],
                "cache": cache,
                "fetch_ms": _ms("fetch_ms"),
                "render_ms": _ms("render_ms"),
                "total_ms": _ms("total_ms"),
                "status": int(status_raw) if isinstance(status_raw, (int, float)) else None,
                "at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        if cache in activation_cache_modes:
            bump_activation_count(panel[:40])
        return ("", 204)

    @app.route("/api/metrics/panel", methods=["GET"])
    def panel_metrics_get():
        """Aggregate of the in-memory samples: per (panel, cache-path) count +
        p50/p95 total ms, plus the overall perceived-latency headline. Resets
        on server restart by design."""
        groups: dict[tuple[str, str], list[float]] = {}
        for s in panel_metrics:
            total = s.get("total_ms")
            if not isinstance(total, (int, float)):
                continue
            groups.setdefault((str(s["panel"]), str(s["cache"])), []).append(float(total))

        def _p(values: list[float], q: float) -> float:
            # Nearest-rank percentile: always an observed value, never the
            # beyond-max extrapolation statistics.quantiles produces on small n.
            ordered = sorted(values)
            idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
            return round(ordered[idx], 1)

        rows = [
            {
                "panel": panel,
                "cache": cache,
                "n": len(vals),
                "p50_ms": _p(vals, 0.50),
                "p95_ms": _p(vals, 0.95),
            }
            for (panel, cache), vals in sorted(groups.items())
        ]
        # The headline: what a tab activation FEELS like (cold first hits vs
        # the cache-served paths). `revalidate` is background work — excluded.
        perceived = [v for (_panel, c), vals in groups.items() if c != "revalidate" for v in vals]
        # Durable per-panel activation totals (navigation_ia §5) — unlike the
        # latency ring these survive restarts; absent table reads as empty
        # (fresh DB before the first POST creates it).
        activations: dict[str, int] = {}
        try:
            conn = open_db()
            try:
                cur = conn.execute(
                    "SELECT panel_id, SUM(count) AS n FROM panel_activation_counts"
                    " WHERE day >= date('now', '-30 days') GROUP BY panel_id ORDER BY n DESC"
                )
                activations = {str(r["panel_id"]): int(r["n"]) for r in cur.fetchall()}
            finally:
                conn.close()
        except sqlite3.Error:
            activations = {}
        return {
            "rows": rows,
            "samples": len(panel_metrics),
            "perceived_p50_ms": _p(perceived, 0.50) if perceived else None,
            "perceived_p95_ms": _p(perceived, 0.95) if perceived else None,
            "activations_30d": activations,
        }
