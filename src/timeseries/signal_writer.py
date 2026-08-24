"""Persist time-series primitive outputs into the timeseries_signals table.

The Phase 1 primitives (src/timeseries/primitives.py) produce stateless JSON
payloads. This module is the bridge that runs them against each metric a
ticker cares about, computes a severity bucket from the payload, renders a
short English narrative, and upserts one row per (ticker, metric, signal)
into the DB. Downstream consumers (the §3.5 renderer, TS-aware LLM prompts)
read the table instead of re-running primitives on every render.

Public surface: ``compute_and_persist_signals(ticker, db, *, run_id=None)``.
Returns the count of rows written / refreshed. Best-effort: a missing
holdings JSON, an empty series, or a primitive returning
``insufficient_data`` is silently skipped — we only persist signals we could
compute. Schema mismatches (missing table) raise so the caller learns the
migration hasn't run.

Metric scope per ticker:
  - ``_DEFAULT_FINANCIALS`` (revenue, OI, FCF, gross margin, OI margin) —
    a universal basket so every ticker gets baseline coverage even when no
    holdings JSON exists.
  - ``chart_priorities`` from ``micro_thesis/holdings/<T>.json`` — each
    string is normalized via ``_chart_label_to_line_item`` and dispatched
    to financial / kpi / segment loaders by best-match.
  - ``tier_1_kpis[*].name`` from the same JSON — passed as kpi_name to
    ``load_kpi_series``.

Severity buckets (see ``_severity`` for the per-signal rules):
  green  — within normal regime
  yellow — worth a glance (mild trend break, near-threshold anomaly)
  red    — material change: thesis input, drives a brief callout
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from timeseries.loaders import (
    load_financial_series,
    load_kpi_series,
    load_segment_series,
)
from timeseries.primitives import (
    Observation,
    detect_inflection,
    detect_trend,
    rolling_zscore_anomalies,
    seasonal_decompose,
    yoy_acceleration,
)
from user_state.kpi_polarity import Polarity, infer_polarity_from_table

log = logging.getLogger(__name__)


# Default basket every ticker gets profiled on. Keep aligned with
# execution/analyze_timeseries._FINANCIAL_LINE_ITEMS so the CLI + the writer
# don't drift on what "baseline coverage" means.
_DEFAULT_FINANCIALS: tuple[str, ...] = (
    "revenue",
    "operating_income",
    "free_cash_flow",
    "gross_profit",
    "net_income",
)


# Per-signal severity thresholds. Tuned for noisy quarterly series — the
# bar for "red" is calibrated so a P1 ticker doesn't surface > 1-2 red
# signals per quarter in steady state. Tweak in concert with the renderer.
_ZSCORE_RED = 2.5
_ZSCORE_YELLOW = 1.5
_INFLECTION_RED_MAGNITUDE = 0.5

# Number of trailing quarters used to flag "fresh" inflection. An inflection
# detected 8+ quarters ago is historical context, not a current signal.
_INFLECTION_FRESH_QUARTERS = 2


@dataclass(frozen=True)
class _MetricSpec:
    """One metric to profile: how to load it + how to label the resulting rows."""

    metric_name: str
    metric_kind: str  # 'financial' | 'kpi' | 'segment'
    loader: str  # 'financial' | 'kpi' | 'segment'
    # Loader args needed for the segment case (segment_name, metric):
    segment_args: tuple[str, str] | None = None
    # A holdings tier-1 KPI is explicitly thesis-linked. Other metrics may be
    # useful statistical context, but must not imply thesis relevance.
    is_thesis_kpi: bool = False


def compute_and_persist_signals(
    ticker: str,
    db: sqlite3.Connection,
    *,
    repo_root: Path | None = None,
    run_id: str | None = None,
) -> int:
    """Run every primitive over every in-scope metric, persist rows.

    Returns the number of rows written/refreshed (each metric x primitive
    combination that produced a usable payload counts once). Re-running
    overwrites existing rows in place — the unique constraint on
    (ticker, metric_name, metric_kind, signal_type) enforces "one current
    row per signal" semantics, and the writer uses
    ``INSERT ... ON CONFLICT REPLACE`` so each refresh is idempotent.

    ``repo_root`` is the project root (used to find holdings JSON +
    portfolio.db for the segment loader). Defaults to the cwd's
    grandparent, which is the convention every CLI in execution/ uses.

    Raises ``sqlite3.OperationalError`` when the table is missing — the
    caller should run ``alembic upgrade head`` before invoking this.
    """
    ticker = ticker.upper()
    root = repo_root if repo_root is not None else Path.cwd()
    metric_specs = _collect_metric_specs(ticker, root)
    if not metric_specs:
        log.debug({"event": "signal_writer_no_metrics", "ticker": ticker})
        return 0

    now = datetime.now(UTC).replace(tzinfo=None)
    rows_written = 0
    for spec in metric_specs:
        series = _load_series(spec, ticker=ticker, repo_root=root)
        if not series:
            continue
        for signal_type, value, severity, narrative in _iter_signals(spec, series):
            _upsert(
                db,
                ticker=ticker,
                metric_name=spec.metric_name,
                metric_kind=spec.metric_kind,
                signal_type=signal_type,
                value_json=value,
                severity=severity,
                narrative=narrative,
                computed_at=now,
                run_id=run_id,
            )
            rows_written += 1

    db.commit()
    log.info(
        {
            "event": "signals_persisted",
            "ticker": ticker,
            "rows_written": rows_written,
            "metrics_profiled": len(metric_specs),
            "run_id": run_id,
        }
    )
    return rows_written


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------


def _collect_metric_specs(ticker: str, repo_root: Path) -> list[_MetricSpec]:
    """Build the per-ticker list of (metric, kind, loader) tuples to profile.

    Pulls from:
      1. ``_DEFAULT_FINANCIALS`` — universal baseline
      2. ``chart_priorities`` in holdings JSON — analyst-flagged
      3. ``tier_1_kpis[*].name`` in holdings JSON — thesis-critical KPIs

    Duplicates collapse on (metric_name, metric_kind) so a chart_priority
    that overlaps the default basket only profiles once.
    """
    specs: list[_MetricSpec] = []
    seen: set[tuple[str, str]] = set()

    def _add(spec: _MetricSpec) -> None:
        key = (spec.metric_name, spec.metric_kind)
        if key in seen:
            return
        seen.add(key)
        specs.append(spec)

    for li in _DEFAULT_FINANCIALS:
        _add(_MetricSpec(metric_name=li, metric_kind="financial", loader="financial"))

    holdings = _load_holdings(ticker, repo_root)
    if holdings is None:
        return specs

    raw_priorities = _object_list(holdings.get("chart_priorities"))
    if raw_priorities is not None:
        for label in raw_priorities:
            if not isinstance(label, str):
                continue
            mapped = _chart_label_to_line_item(label)
            if mapped is not None:
                _add(_MetricSpec(metric_name=mapped, metric_kind="financial", loader="financial"))

    raw_kpis = _object_list(holdings.get("tier_1_kpis"))
    if raw_kpis is not None:
        for raw_kpi in raw_kpis:
            kpi = _object_mapping(raw_kpi)
            if kpi is None:
                continue
            name = kpi.get("name")
            if isinstance(name, str) and name.strip():
                _add(
                    _MetricSpec(
                        metric_name=name.strip(),
                        metric_kind="kpi",
                        loader="kpi",
                        is_thesis_kpi=True,
                    )
                )

    return specs


def _load_holdings(ticker: str, repo_root: Path) -> dict[str, object] | None:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        return cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning({"event": "holdings_load_failed", "ticker": ticker, "error": str(exc)})
        return None


def _object_list(value: object) -> list[object] | None:
    """Narrow parsed JSON arrays at the holdings boundary."""
    return cast("list[object]", value) if isinstance(value, list) else None


def _object_mapping(value: object) -> dict[str, object] | None:
    """Narrow parsed JSON objects at the holdings boundary."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


# Display labels in holdings JSON ("Revenue", "Operating income") need to
# map to the financial_facts.line_item canonical name. Best-match table,
# kept narrow on purpose — anything not listed is skipped silently rather
# than guessed (a guess that hits the wrong line_item is worse than a miss).
_CHART_LABEL_MAP: dict[str, str] = {
    "revenue": "revenue",
    "operating income": "operating_income",
    "operating margin": "operating_income",  # the level series; margin is derived
    "free cash flow": "free_cash_flow",
    "fcf": "free_cash_flow",
    "net income": "net_income",
    "gross profit": "gross_profit",
    "gross margin": "gross_profit",
    "operating cash flow": "operating_cash_flow",
    "capex": "capital_expenditure",
    "capital expenditure": "capital_expenditure",
}


def _chart_label_to_line_item(label: str) -> str | None:
    """Map a human chart-priority label to a financial_facts line_item.

    Returns None when the label doesn't unambiguously match — a segment-
    level chart priority like "GCP revenue growth (YoY)" deliberately
    falls through (segments are profiled via the KPI path when registered).
    """
    norm = label.strip().lower()
    # Drop trailing parenthetical / units
    for sep in (" (", " /", " — ", " -"):
        if sep in norm:
            norm = norm.split(sep)[0].strip()
    return _CHART_LABEL_MAP.get(norm)


def _load_series(spec: _MetricSpec, *, ticker: str, repo_root: Path) -> list[Observation]:
    """Dispatch to the right loader given the spec."""
    if spec.loader == "financial":
        return load_financial_series(ticker=ticker, line_item=spec.metric_name, repo_root=repo_root)
    if spec.loader == "kpi":
        return load_kpi_series(ticker=ticker, kpi_name=spec.metric_name, repo_root=repo_root)
    if spec.loader == "segment" and spec.segment_args is not None:
        seg, metric = spec.segment_args
        return load_segment_series(
            ticker=ticker, segment_name=seg, metric=metric, repo_root=repo_root
        )
    return []


# ---------------------------------------------------------------------------
# Signal computation + severity
# ---------------------------------------------------------------------------


def _iter_signals(
    spec: _MetricSpec, series: list[Observation]
) -> Iterable[tuple[str, str, str, str | None]]:
    """Yield (signal_type, value_json, severity, narrative) for each
    primitive that produced a usable result. Skips ``insufficient_data``."""
    source_period = series[-1].period_end.date().isoformat()
    polarity = infer_polarity_from_table(spec.metric_name)

    trend = detect_trend(series)
    if "insufficient_data" not in trend:
        sev, narr = _severity_trend(spec, trend)
        yield "trend", _serialize_signal(spec, trend, source_period, polarity, "trend"), sev, narr

    infl = detect_inflection(series)
    if "insufficient_data" not in infl and infl.get("inflection_period"):
        sev, narr = _severity_inflection(spec, infl, len(series))
        yield (
            "inflection",
            _serialize_signal(spec, infl, source_period, polarity, "inflection"),
            sev,
            narr,
        )

    anomalies = rolling_zscore_anomalies(series, window=8, threshold=_ZSCORE_YELLOW)
    if anomalies:
        # Persist ALL anomalies as one row whose payload is the list — the
        # renderer wants per-quarter context. Severity reflects the most
        # extreme zscore in the list.
        sev, narr = _severity_anomalies(spec, anomalies)
        yield (
            "anomaly",
            _serialize_signal(spec, {"anomalies": anomalies}, source_period, polarity, "anomaly"),
            sev,
            narr,
        )

    yoy = yoy_acceleration(series)
    if "insufficient_data" not in yoy:
        sev, narr = _severity_yoy_acceleration(spec, yoy)
        yield (
            "yoy_acceleration",
            _serialize_signal(spec, yoy, source_period, polarity, "yoy_acceleration"),
            sev,
            narr,
        )

    decomp = seasonal_decompose(series, period=4)
    if "insufficient_data" not in decomp:
        # Strip the per-period arrays for storage — they're verbose and the
        # renderer doesn't surface them. Keep only summary stats.
        summary = {
            "n": decomp.get("n"),
            "period": decomp.get("period"),
            "method": decomp.get("method"),
            "seasonal_strength": decomp.get("seasonal_strength"),
        }
        sev, narr = _severity_seasonal(spec, summary)
        yield (
            "seasonal",
            _serialize_signal(spec, summary, source_period, polarity, "seasonal"),
            sev,
            narr,
        )


def _serialize_signal(
    spec: _MetricSpec,
    payload: dict[str, object],
    source_period: str,
    polarity: Polarity | None,
    signal_type: str,
) -> str:
    """Persist statistical output with its investment interpretation.

    Primitives describe what happened statistically. Direction is a distinct,
    polarity-aware interpretation; a metric without a known polarity remains
    ambiguous rather than inheriting a bullish/bearish meaning from its slope.
    """
    persisted = dict(payload)
    persisted["source_period"] = source_period
    persisted["is_thesis_kpi"] = spec.is_thesis_kpi
    if polarity is not None:
        persisted["polarity"] = polarity.value
    persisted["investment_direction"] = classify_investment_direction(
        signal_type, persisted, polarity
    )
    return json.dumps(persisted, default=str)


def classify_investment_direction(
    signal_type: str,
    payload: dict[str, object],
    polarity: Polarity | None,
) -> str:
    """Return favorable, unfavorable, or ambiguous without overstating evidence."""
    if polarity is None:
        return "ambiguous"
    if signal_type == "trend" and payload.get("statistical_significance") is not True:
        # A non-significant slope may be informative context, never an adverse
        # investment conclusion without a separately encoded domain rule.
        return "ambiguous"

    signed_change = _signed_change(signal_type, payload)
    if signed_change is None or signed_change == 0:
        return "ambiguous"
    higher_is_better = polarity is Polarity.HIGHER_IS_BETTER
    favorable = signed_change > 0 if higher_is_better else signed_change < 0
    return "favorable" if favorable else "unfavorable"


def _signed_change(signal_type: str, payload: dict[str, object]) -> float | None:
    """Extract the signed movement relevant to a polarity interpretation."""
    candidate: object | None = None
    if signal_type == "trend":
        candidate = payload.get("slope")
    elif signal_type == "anomaly":
        anomalies = _object_list(payload.get("anomalies"))
        if anomalies:
            latest = _object_mapping(anomalies[-1])
            if latest is not None:
                candidate = latest.get("zscore")
    elif signal_type == "inflection":
        prior = payload.get("prior_slope")
        post = payload.get("post_slope")
        if isinstance(prior, (int, float)) and isinstance(post, (int, float)):
            return float(post) - float(prior)
    elif signal_type == "yoy_acceleration":
        candidate = payload.get("most_recent_delta")
    if isinstance(candidate, (int, float)):
        return float(candidate)
    return None


def _severity_trend(spec: _MetricSpec, payload: dict[str, object]) -> tuple[str, str]:
    direction = str(payload.get("direction", "flat"))
    sig = bool(payload.get("statistical_significance", False))
    slope_pct = float(cast("float", payload.get("slope_pct_of_mean", 0.0)))
    # Decelerating + statistically significant on a thesis-critical metric is
    # the canonical "watch this" signal. Inflecting is always at least yellow.
    if direction == "inflecting":
        sev = "red"
    elif direction == "decelerating" and sig:
        sev = "yellow"
    elif direction == "accelerating" and sig:
        sev = "green"
    else:
        sev = "green"
    narrative = (
        f"{_pretty(spec.metric_name)}: {direction} trend "
        f"(slope {slope_pct:+.1%} of mean, "
        f"{'significant' if sig else 'not significant'})."
    )
    return sev, narrative


def _severity_inflection(
    spec: _MetricSpec, payload: dict[str, object], series_len: int
) -> tuple[str, str]:
    magnitude = float(cast("float", payload.get("magnitude", 0.0)))
    idx = int(cast("int", payload.get("inflection_index", 0)))
    quarters_ago = max(0, series_len - 1 - idx)
    is_fresh = quarters_ago <= _INFLECTION_FRESH_QUARTERS
    if magnitude >= _INFLECTION_RED_MAGNITUDE and is_fresh:
        sev = "red"
    elif magnitude >= _INFLECTION_RED_MAGNITUDE:
        sev = "yellow"
    else:
        sev = "green"
    period = payload.get("inflection_period")
    narrative = (
        f"{_pretty(spec.metric_name)}: inflection detected at {period} "
        f"(magnitude {magnitude:.2f}\u03c3, {quarters_ago}Q ago)."
    )
    return sev, narrative


def _severity_anomalies(spec: _MetricSpec, anomalies: list[dict[str, object]]) -> tuple[str, str]:
    max_abs = max(abs(float(cast("float", a["zscore"]))) for a in anomalies)
    most_recent = anomalies[-1]
    if max_abs >= _ZSCORE_RED:
        sev = "red"
    elif max_abs >= _ZSCORE_YELLOW:
        sev = "yellow"
    else:
        sev = "green"
    narrative = (
        f"{_pretty(spec.metric_name)}: {len(anomalies)} anomalous "
        f"quarter(s) vs trailing window; most recent "
        f"{most_recent.get('period_end')} z={float(cast('float', most_recent['zscore'])):+.2f}."
    )
    return sev, narrative


def _severity_yoy_acceleration(spec: _MetricSpec, payload: dict[str, object]) -> tuple[str, str]:
    most_recent_yoy = float(cast("float", payload.get("most_recent_yoy", 0.0)))
    most_recent_delta = float(cast("float", payload.get("most_recent_delta", 0.0)))
    trend = str(payload.get("trend", "flat"))
    # YoY crossing zero (positive → negative or vice versa) inside the latest
    # delta is the canonical "thesis-input" signal. Sustained deceleration
    # without crossing is yellow; everything else green.
    crossed_zero = (most_recent_yoy - most_recent_delta) * most_recent_yoy < 0.0
    if crossed_zero or (trend == "decelerating" and abs(most_recent_delta) > 0.02):
        sev = "yellow"
    else:
        sev = "green"
    narrative = (
        f"{_pretty(spec.metric_name)}: YoY {most_recent_yoy:+.1%} "
        f"(Δ {most_recent_delta:+.2%} QoQ, {trend})."
    )
    return sev, narrative


def _severity_seasonal(spec: _MetricSpec, payload: dict[str, object]) -> tuple[str, str]:
    strength = float(cast("float", payload.get("seasonal_strength", 0.0)))
    # Seasonality is informational — never red. Strong seasonality (>0.6)
    # flags as yellow so the renderer knows YoY-only framing is preferable
    # to QoQ for this series.
    sev = "yellow" if strength >= 0.6 else "green"
    narrative = (
        f"{_pretty(spec.metric_name)}: seasonal_strength={strength:.2f} "
        f"(method={payload.get('method')})."
    )
    return sev, narrative


def _pretty(metric_name: str) -> str:
    """Human-friendly metric label for narrative text — title-case
    underscores into spaces. Names that already contain spaces (KPI names)
    are passed through unchanged."""
    if "_" not in metric_name:
        return metric_name
    return metric_name.replace("_", " ").title()


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------


_UPSERT_SQL = """
INSERT INTO timeseries_signals
    (ticker, metric_name, metric_kind, signal_type,
     value_json, severity, narrative, computed_at, run_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(ticker, metric_name, metric_kind, signal_type)
DO UPDATE SET
    value_json = excluded.value_json,
    severity = excluded.severity,
    narrative = excluded.narrative,
    computed_at = excluded.computed_at,
    run_id = excluded.run_id
"""


def _upsert(
    db: sqlite3.Connection,
    *,
    ticker: str,
    metric_name: str,
    metric_kind: str,
    signal_type: str,
    value_json: str,
    severity: str,
    narrative: str | None,
    computed_at: datetime,
    run_id: str | None,
) -> None:
    # Stringify the datetime ourselves — Python 3.12 deprecated the implicit
    # datetime → sqlite TEXT adapter. The shape matches what the rest of the
    # codebase writes for *_at columns (isoformat with space separator).
    db.execute(
        _UPSERT_SQL,
        (
            ticker,
            metric_name,
            metric_kind,
            signal_type,
            value_json,
            severity,
            narrative,
            computed_at.isoformat(sep=" ", timespec="seconds"),
            run_id,
        ),
    )
