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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from report.models import (
    MissingReason,
    SectionStatus,
    SignalRow,
    SignalsSection,
)
from report.render_clock import render_today
from report.sections._common import has_table, open_repo_db

log = logging.getLogger(__name__)

_SIGNAL_STALE_AFTER_DAYS = 200
_SUMMARY_LIMIT = 3
_MetricKind = Literal["financial", "kpi", "segment"]
_SignalType = Literal[
    "trend", "inflection", "anomaly", "yoy_acceleration", "seasonal", "correlation"
]
_SignalSeverity = Literal["green", "yellow", "red"]
_InvestmentDirection = Literal["favorable", "unfavorable", "ambiguous"]
_SignalFreshness = Literal["fresh", "stale", "unknown"]


def build(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
    as_of: date | None = None,
) -> SignalsSection:
    """Build the §3.5 SignalsSection for one ticker.

    Returns an OK section even when the table is missing — the renderer's
    contract is to omit the section entirely when no rows surface, not to
    show a "missing data" callout for a feature this ticker simply hasn't
    been profiled for yet.
    """
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return SignalsSection(
            status=SectionStatus.MISSING_DATA,
            missing=MissingReason(
                stage="INFRA(portfolio.db)",
                fix_command="alembic upgrade head",
                detail="no data/portfolio.db found",
            ),
        )
    try:
        if not has_table(db_conn, "timeseries_signals"):
            return SignalsSection(status=SectionStatus.OK)
        rows = _fetch_signal_rows(db_conn, ticker, as_of=as_of or render_today())
    finally:
        if conn is None:
            db_conn.close()

    red, yellow, green = _bucket_and_sort(rows)
    all_signals = red + yellow + green
    ranked = _rank(all_signals)
    summary = _summary_candidates(ranked)
    return SignalsSection(
        status=SectionStatus.OK,
        red_signals=red,
        yellow_signals=yellow,
        green_signals=green,
        summary_signals=summary[:_SUMMARY_LIMIT],
        all_signals=ranked,
    )


def _fetch_signal_rows(conn: sqlite3.Connection, ticker: str, *, as_of: date) -> list[SignalRow]:
    """Pull every timeseries_signals row for the ticker, hydrated into SignalRow."""
    conn.row_factory = sqlite3.Row
    rs = conn.execute(
        """
        SELECT metric_name, metric_kind, signal_type, severity,
               narrative, value_json, computed_at
        FROM timeseries_signals
        WHERE ticker = ?
        """,
        (ticker.upper(),),
    ).fetchall()

    out: list[SignalRow] = []
    for r in rs:
        metric_kind = str(r["metric_kind"])
        signal_type = str(r["signal_type"])
        severity = str(r["severity"])
        if metric_kind not in ("financial", "kpi", "segment"):
            continue
        if signal_type not in (
            "trend",
            "inflection",
            "anomaly",
            "yoy_acceleration",
            "seasonal",
            "correlation",
        ):
            continue
        if severity not in ("green", "yellow", "red"):
            continue
        try:
            payload = cast("dict[str, object]", json.loads(str(r["value_json"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        magnitude = _magnitude_for(signal_type, payload)
        source_period = _source_period(payload)
        out.append(
            SignalRow(
                metric_name=str(r["metric_name"]),
                metric_kind=metric_kind,
                signal_type=signal_type,
                severity=severity,
                narrative=r["narrative"],
                value_summary=_value_summary(signal_type, payload),
                severity_magnitude=magnitude,
                investment_direction=_investment_direction(payload),
                statistical_significance=_statistical_significance(payload),
                freshness=_freshness(source_period, as_of),
                source_period=source_period,
                computed_at=_parse_datetime(r["computed_at"]),
                is_thesis_kpi=payload.get("is_thesis_kpi") is True,
            )
        )
    return out


def _investment_direction(payload: dict[str, object]) -> _InvestmentDirection:
    direction = payload.get("investment_direction")
    if direction in ("favorable", "unfavorable", "ambiguous"):
        return direction
    return "ambiguous"


def _object_list(value: object) -> list[object] | None:
    """Narrow a validated JSON array without allowing ``Unknown`` downstream."""
    return cast("list[object]", value) if isinstance(value, list) else None


def _object_mapping(value: object) -> dict[str, object] | None:
    """Narrow a validated JSON object at the report payload boundary."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def _statistical_significance(payload: dict[str, object]) -> bool | None:
    significance = payload.get("statistical_significance")
    return significance if isinstance(significance, bool) else None


def _source_period(payload: dict[str, object]) -> date | None:
    raw = payload.get("source_period")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _parse_datetime(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _freshness(source_period: date | None, as_of: date) -> _SignalFreshness:
    """Evaluate the source period against the report's explicit render date."""
    if source_period is None:
        return "unknown"
    return "stale" if source_period < as_of - timedelta(days=_SIGNAL_STALE_AFTER_DAYS) else "fresh"


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


def _rank(rows: list[SignalRow]) -> list[SignalRow]:
    """Rank all disclosed signals; the summary applies a stricter admission gate."""
    freshness_rank = {"fresh": 3, "unknown": 2, "stale": 1}
    significance_rank = {True: 3, None: 2, False: 1}
    direction_rank = {"unfavorable": 3, "ambiguous": 2, "favorable": 1}
    severity_rank = {"red": 3, "yellow": 2, "green": 1}

    def key(row: SignalRow) -> tuple[int, int, int, int, int, int, float, str]:
        return (
            freshness_rank[row.freshness],
            significance_rank[row.statistical_significance],
            direction_rank[row.investment_direction],
            int(row.is_thesis_kpi),
            severity_rank[row.severity],
            row.source_period.toordinal() if row.source_period is not None else 0,
            abs(row.severity_magnitude or 0.0),
            row.metric_name.lower(),
        )

    ordered = sorted(rows, key=key, reverse=True)
    return [row.model_copy(update={"rank": index}) for index, row in enumerate(ordered, start=1)]


def _summary_candidates(rows: list[SignalRow]) -> list[SignalRow]:
    """Admit only current, statistically credible evidence to the compact summary.

    Statistical outliers without a current source period or significance remain
    available in the all-signals disclosure. No signal type currently carries a
    deterministic domain rule that overrides this gate.
    """
    return [
        row for row in rows if row.freshness == "fresh" and row.statistical_significance is True
    ]


def _magnitude_for(signal_type: str, payload: dict[str, object]) -> float | None:
    """Pull the per-signal magnitude scalar from the JSON payload.

    Each primitive's payload shape (see src/timeseries/primitives.py) is
    keyed by signal_type — we extract whatever the writer used in its
    severity calculation so the within-tier sort agrees with what bumped
    the row into that tier in the first place.
    """
    if signal_type == "anomaly":
        anomalies = _object_list(payload.get("anomalies"))
        if anomalies:
            zs: list[float] = []
            for a in anomalies:
                anomaly = _object_mapping(a)
                if anomaly is None:
                    continue
                z = anomaly.get("zscore")
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
        anomalies = _object_list(payload.get("anomalies"))
        if not anomalies:
            return None
        # Most-recent anomaly + max |z| in the window.
        zs: list[float] = []
        last_z: float | None = None
        for a in anomalies:
            anomaly = _object_mapping(a)
            if anomaly is None:
                continue
            z = anomaly.get("zscore")
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
            return f"magnitude={float(mag):.2f}\u03c3{when}"
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
