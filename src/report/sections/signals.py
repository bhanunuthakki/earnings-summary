"""§3.5 Signals — renderer-shaped projection of `timeseries_signals`.

The writer at `src/timeseries/signal_writer.py` persists one row per
(ticker, metric_name, metric_kind, signal_type) on every quarterly refresh.
Until this section, those rows were captured but never surfaced in a brief.

Contract:
  - Best-effort: missing DB / missing table → empty section, status OK.
    The renderer omits the whole section when all three tiers are empty,
    so an unmigrated ticker just silently lacks §3.5.
  - Filters to current row per signal — the writer already enforces the
    (ticker, metric, kind, signal_type) unique key, so a plain SELECT
    suffices.
  - Sorts within tier by a signal-specific magnitude scalar pulled from
    value_json: |zscore| for anomaly, |slope_pct_of_mean| for trend,
    |magnitude| for inflection, |most_recent_delta| for yoy_acceleration,
    seasonal_strength for seasonal. Falls back to None (sorted last in
    tier) when the payload shape doesn't carry one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import cast

from report.models import (
    MissingReason,
    SectionStatus,
    SignalRow,
    SignalsSection,
)
from report.sections._common import has_table, open_repo_db

log = logging.getLogger(__name__)


def build(ticker: str, repo_root: Path) -> SignalsSection:
    """Build the §3.5 SignalsSection for one ticker.

    Returns an OK section even when the table is missing — the renderer's
    contract is to omit the section entirely when no rows surface, not to
    show a "missing data" callout for a feature this ticker simply hasn't
    been profiled for yet.
    """
    conn = open_repo_db(repo_root)
    if conn is None:
        return SignalsSection(
            status=SectionStatus.MISSING_DATA,
            missing=MissingReason(
                stage="INFRA(portfolio.db)",
                fix_command="alembic upgrade head",
                detail="no data/portfolio.db found",
            ),
        )
    try:
        if not has_table(conn, "timeseries_signals"):
            return SignalsSection(status=SectionStatus.OK)
        rows = _fetch_signal_rows(conn, ticker)
    finally:
        conn.close()

    red, yellow, green = _bucket_and_sort(rows)
    return SignalsSection(
        status=SectionStatus.OK,
        red_signals=red,
        yellow_signals=yellow,
        green_signals=green,
    )


def _fetch_signal_rows(conn: sqlite3.Connection, ticker: str) -> list[SignalRow]:
    """Pull every timeseries_signals row for the ticker, hydrated into SignalRow."""
    conn.row_factory = sqlite3.Row
    rs = conn.execute(
        """
        SELECT metric_name, metric_kind, signal_type, severity,
               narrative, value_json
        FROM timeseries_signals
        WHERE ticker = ?
        """,
        (ticker.upper(),),
    ).fetchall()

    out: list[SignalRow] = []
    for r in rs:
        signal_type = str(r["signal_type"])
        severity = str(r["severity"])
        if severity not in ("green", "yellow", "red"):
            continue
        try:
            payload = cast("dict[str, object]", json.loads(str(r["value_json"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        magnitude = _magnitude_for(signal_type, payload)
        out.append(
            SignalRow(
                metric_name=str(r["metric_name"]),
                metric_kind=cast("str", r["metric_kind"]),  # CHECK constraint guarantees enum
                signal_type=cast("str", signal_type),
                severity=cast("str", severity),
                narrative=r["narrative"],
                value_summary=_value_summary(signal_type, payload),
                severity_magnitude=magnitude,
            )
        )
    return out


def _bucket_and_sort(
    rows: list[SignalRow],
) -> tuple[list[SignalRow], list[SignalRow], list[SignalRow]]:
    """Bucket by severity, then sort within tier by |magnitude| descending."""

    def _sort_key(r: SignalRow) -> tuple[int, float]:
        # Larger magnitudes first; None rows go to the end (massive negative
        # number after negation = sorts last when descending).
        if r.severity_magnitude is None:
            return (1, 0.0)
        return (0, -abs(r.severity_magnitude))

    red = sorted([r for r in rows if r.severity == "red"], key=_sort_key)
    yellow = sorted([r for r in rows if r.severity == "yellow"], key=_sort_key)
    green = sorted([r for r in rows if r.severity == "green"], key=_sort_key)
    return red, yellow, green


def _magnitude_for(signal_type: str, payload: dict[str, object]) -> float | None:
    """Pull the per-signal magnitude scalar from the JSON payload.

    Each primitive's payload shape (see src/timeseries/primitives.py) is
    keyed by signal_type — we extract whatever the writer used in its
    severity calculation so the within-tier sort agrees with what bumped
    the row into that tier in the first place.
    """
    if signal_type == "anomaly":
        anomalies = payload.get("anomalies")
        if isinstance(anomalies, list) and anomalies:
            zs: list[float] = []
            for a in anomalies:
                if not isinstance(a, dict):
                    continue
                z = a.get("zscore")
                if isinstance(z, (int, float)):
                    zs.append(abs(float(z)))
            if zs:
                return max(zs)
        return None
    if signal_type == "trend":
        slope = payload.get("slope_pct_of_mean")
        return float(slope) if isinstance(slope, (int, float)) else None
    if signal_type == "inflection":
        mag = payload.get("magnitude")
        return float(mag) if isinstance(mag, (int, float)) else None
    if signal_type == "yoy_acceleration":
        delta = payload.get("most_recent_delta")
        return float(delta) if isinstance(delta, (int, float)) else None
    if signal_type == "seasonal":
        strength = payload.get("seasonal_strength")
        return float(strength) if isinstance(strength, (int, float)) else None
    return None


def _value_summary(signal_type: str, payload: dict[str, object]) -> str | None:
    """Render the one-line numeric hint that appears under a signal card.

    Returns None when the payload doesn't carry a usable scalar (the
    renderer suppresses the hint line). Format aims for "z=2.8", "slope=-12%/yr",
    "Δ=-180bps" — short enough to live in a small chip beside the narrative.
    """
    if signal_type == "anomaly":
        anomalies = payload.get("anomalies")
        if not isinstance(anomalies, list) or not anomalies:
            return None
        # Most-recent anomaly + max |z| in the window.
        zs: list[float] = []
        last_z: float | None = None
        for a in anomalies:
            if not isinstance(a, dict):
                continue
            z = a.get("zscore")
            if isinstance(z, (int, float)):
                last_z = float(z)
                zs.append(abs(last_z))
        if not zs:
            return None
        return f"z={last_z:+.2f} (max |z|={max(zs):.2f}, {len(anomalies)} pts)"
    if signal_type == "trend":
        slope = payload.get("slope_pct_of_mean")
        direction = payload.get("direction")
        if isinstance(slope, (int, float)):
            dir_s = f", {direction}" if isinstance(direction, str) else ""
            return f"slope={float(slope):+.1%} of mean{dir_s}"
        return None
    if signal_type == "inflection":
        mag = payload.get("magnitude")
        period = payload.get("inflection_period")
        if isinstance(mag, (int, float)):
            when = f" @ {period}" if isinstance(period, str) else ""
            return f"magnitude={float(mag):.2f}σ{when}"
        return None
    if signal_type == "yoy_acceleration":
        yoy = payload.get("most_recent_yoy")
        delta = payload.get("most_recent_delta")
        parts: list[str] = []
        if isinstance(yoy, (int, float)):
            parts.append(f"YoY={float(yoy):+.1%}")
        if isinstance(delta, (int, float)):
            parts.append(f"Δ={float(delta):+.2%}")
        return ", ".join(parts) if parts else None
    if signal_type == "seasonal":
        strength = payload.get("seasonal_strength")
        method = payload.get("method")
        if isinstance(strength, (int, float)):
            method_s = f", {method}" if isinstance(method, str) else ""
            return f"seasonal_strength={float(strength):.2f}{method_s}"
        return None
    return None
