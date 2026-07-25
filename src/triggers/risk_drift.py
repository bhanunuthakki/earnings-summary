"""risk_drift — C8: drift alerts over the append-only risk-snapshot history.

``portfolio_risk_snapshot_history`` (0185/0186) has been append-only since the
program review, but nothing read the history BACK until this sensor: "did my
book's risk posture move vs 30 days ago" was structurally unanswerable off the
single-row latest-view table the render surfaces still read. This module is
the first consumer of that drift substrate.

Book-level, deterministic, zero LLM
------------------------------------
Unlike the per-ticker ``triggers.base.Trigger`` protocol sensors (kpi_inflection,
material_news, ...), a risk-posture drift is a whole-book observation with no
natural ticker to scan against — it does not implement that Protocol (there is
no ``scan(ticker, db)`` to fan out over). It instead mirrors the shape
``execution/refresh_portfolio_risk_snapshot.py``'s own ``_fire_deadman`` helper
already established for 'data_feed_stale' (0183): a plain function that reads
the substrate, computes findings, and calls ``alerts.store.fire_alert``
directly, keyed on the book-level sentinel ticker ``'PORTFOLIO'`` (the same
convention 0171/0183 use — ``alerts.ticker`` is NOT NULL, so a book-level
alert can never literally carry ``ticker=NULL``).

What drifts
-----------
Four scalar metrics read straight off ``portfolio_risk_snapshot_history``
columns (spy_beta, growth_tilt, top1_weight_pct, top5_weight_pct), plus the C3
book-level business-factor legs (``risk_factors.book_factor_vector``), snapshotted
onto each capture's additive ``factor_vector_json`` column (0197) by
:func:`append_factor_vector`, called as a post-write hook from the risk writer
right after ``write_snapshot`` succeeds — see that script for the wiring.
Older captures (pre-0197) simply carry no factor vector, so a name's factor
legs join the baseline population only once enough post-0197 captures exist;
until then :func:`compute_drift_findings` silently skips factor-leg drift
(no baseline, no fire — same "not enough history" degrade as the scalar legs).

For each metric, drift is ``|latest - mean(trailing_30d_baseline)|``, baseline
EXCLUDING the latest capture itself. Thresholds (below) are deliberately
conservative — a drift alert the owner learns to ignore trains muting, and
that failure mode is worse than being a day late on a real move.

Dedup that still lets a WORSENING drift re-fire
-------------------------------------------------
Naive dedup keyed on (metric, direction) would suppress a persisting drift
after day 1 forever, even as it gets materially worse. Naive dedup keyed on
the exact magnitude would re-fire every single day (yesterday's beta and
today's are never bit-identical). :func:`_bucket_magnitude` buckets the drift
into "how many threshold-multiples crossed" — the signature stays stable
while the drift sits in the same band (no daily re-fire) but changes (and
re-fires) the moment it crosses into the next band (the drift getting worse
IS new information worth surfacing again).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from alerts.store import compute_signature_sha, find_by_signature, fire_alert
from identity import DEFAULT_USER_ID

log = logging.getLogger(__name__)

TRIGGER_KIND: Final[str] = "risk_drift"
# Book-level sentinel ticker — see module docstring; matches the 0171/0183
# convention (``alerts.ticker`` is NOT NULL).
TICKER_SENTINEL: Final[str] = "PORTFOLIO"

_HISTORY_TABLE: Final[str] = "portfolio_risk_snapshot_history"
_FACTOR_COL: Final[str] = "factor_vector_json"
_BASELINE_WINDOW_DAYS: Final[int] = 30

# Per-metric drift thresholds. Units match the stored column: spy_beta and
# growth_tilt are raw beta/tilt numbers (e.g. 1.15); top1/top5 are PERCENT
# (top1_weight_pct=20.0 means 20%); factor legs are weight-fraction units
# (risk_factors.BookFactorVector.vector = sum(weight_fraction x loading),
# weight_fraction in 0..1) so "10pp" there means 0.10. Tuned conservative per
# the C8 plan — an alert the owner ignores trains muting.
SPY_BETA_DRIFT_THRESHOLD: Final[float] = 0.15
GROWTH_TILT_DRIFT_THRESHOLD: Final[float] = 0.15
TOP1_DRIFT_THRESHOLD_PCT: Final[float] = 5.0
TOP5_DRIFT_THRESHOLD_PCT: Final[float] = 10.0
FACTOR_LEG_DRIFT_THRESHOLD: Final[float] = 0.10

# The scalar metrics read straight off history columns, paired with their
# threshold — one list so scan/baseline/findings all stay in lockstep with
# adding a metric being a one-line change.
#
# DELIBERATELY EXCLUDES max_drawdown_pct / current_drawdown_pct /
# drawdown_recovered / days_to_recovery. Those four are computed locally in
# refresh_portfolio_risk_snapshot.py from analytics.performance.points, which
# inherits that tracker endpoint's window semantics — legacy defaults to the
# snapshot-derived OBSERVED window, while the typed /api/v1 transport
# (PORTFOLIO_TRACKER_V1_READS, default OFF) defaults to trailing-365d with a
# provider MODELED transaction walk-back filling the pre-observation span
# (verified: 73 points at +0.0664% vs 362 points at +8.62%, same book/day).
# The shipped v1 client rebases to the series' own earliest_observed_date
# (byte-equivalent to legacy, pinned by portfolio-tracker's own
# test_performance_v1_rebases_to_earliest_observed) so this is inert today —
# but if that ever slips, or if drawdown drift monitoring is added here
# later, it needs its own transport-provenance guard (there is currently no
# column on portfolio_risk_snapshot_history to distinguish "real drift" from
# "the read transport changed under us" — see the C8/consolidation-session
# coordination thread, 2026-07-24/25). Do not add a drawdown metric to this
# tuple without reading that context first.
_SCALAR_METRICS: Final[tuple[tuple[str, float], ...]] = (
    ("spy_beta", SPY_BETA_DRIFT_THRESHOLD),
    ("growth_tilt", GROWTH_TILT_DRIFT_THRESHOLD),
    ("top1_weight_pct", TOP1_DRIFT_THRESHOLD_PCT),
    ("top5_weight_pct", TOP5_DRIFT_THRESHOLD_PCT),
)
_FACTOR_METRIC_PREFIX: Final[str] = "factor:"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _HistoryRow:
    """One ``portfolio_risk_snapshot_history`` row, narrowed to what drift
    needs: the scalar metrics (``None`` for a column that was NULL at capture
    time) and the parsed C3 factor vector (``None`` when the capture predates
    0197 or carries no factors)."""

    captured_at: str
    metrics: dict[str, float | None]
    factor_vector: dict[str, float] | None


@dataclass(frozen=True, slots=True)
class DriftFinding:
    """One metric whose latest capture breached its drift threshold against
    the trailing baseline. ``metric`` is the bare column name for a scalar
    (e.g. ``"spy_beta"``) or ``"factor:<label>"`` for a C3 business-factor leg."""

    metric: str
    latest: float
    baseline_mean: float
    baseline_n: int
    direction: str  # "up" | "down"
    magnitude: float
    threshold: float


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    """Best-effort read-write connection, matching every sibling store's
    degrade-don't-crash contract. ``None`` on a missing DB or any open error."""
    if db_path is None:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn
    except sqlite3.Error as exc:
        log.debug({"event": "risk_drift_open_failed", "error": str(exc)})
        return None


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(r[1]) for r in rows}


def _parse_captured_at(raw: str) -> datetime:
    """Parse the history table's stored ISO timestamp. Naive-UTC throughout
    this repo's convention; strips any offset defensively rather than raising
    on a legacy aware-stamped row."""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _row_to_history(select_cols: list[str], row: tuple[object, ...]) -> _HistoryRow:
    values = dict(zip(select_cols, row, strict=True))
    metrics: dict[str, float | None] = {}
    for metric, _ in _SCALAR_METRICS:
        v = values.get(metric)
        metrics[metric] = (
            float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        )
    factor_vector: dict[str, float] | None = None
    raw_fv = values.get(_FACTOR_COL)
    if isinstance(raw_fv, str) and raw_fv.strip():
        try:
            parsed = json.loads(raw_fv)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            # JSON-boundary cast: the isinstance check just confirmed the
            # runtime shape; the cast tells pyright we've validated
            # dict[str, object] and can iterate without further narrowing
            # (project convention — see e.g. dashboard.evidence_drawer).
            parsed_map = cast("dict[str, object]", parsed)
            coerced: dict[str, float] = {}
            for k, v in parsed_map.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    coerced[str(k)] = float(v)
            factor_vector = coerced or None
    return _HistoryRow(
        captured_at=str(values["captured_at"]), metrics=metrics, factor_vector=factor_vector
    )


# ---------------------------------------------------------------------------
# Substrate read — latest capture + trailing baseline
# ---------------------------------------------------------------------------


def load_drift_inputs(
    db_path: Path | str | None,
    *,
    user_id: str = DEFAULT_USER_ID,
    now: datetime | None = None,
) -> tuple[_HistoryRow | None, list[_HistoryRow]]:
    """The latest ``portfolio_risk_snapshot_history`` row plus every OTHER row
    in the trailing ``_BASELINE_WINDOW_DAYS`` before it (baseline, newest
    first). ``(None, [])`` when the table/DB is unavailable or empty — the
    caller degrades to "no drift computed", never raises.

    The baseline window is anchored on the LATEST capture's own
    ``captured_at`` (not ``now``) so a batch re-run against an older fixture,
    or a writer that ran late, still computes a sensible trailing window
    rather than one anchored on wall-clock time.
    """
    _ = now  # reserved for test injection symmetry with sibling sensors; unused here
    conn = _open(db_path)
    if conn is None:
        return None, []
    try:
        if not _has_table(conn, _HISTORY_TABLE):
            return None, []
        cols = _table_columns(conn, _HISTORY_TABLE)
        select_cols = ["captured_at", *[m for m, _ in _SCALAR_METRICS]]
        has_factor_col = _FACTOR_COL in cols
        if has_factor_col:
            select_cols.append(_FACTOR_COL)

        try:
            latest_raw = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM {_HISTORY_TABLE} "
                "WHERE user_id = ? ORDER BY captured_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            log.debug({"event": "risk_drift_latest_query_failed", "error": str(exc)})
            return None, []
        if latest_raw is None:
            return None, []
        latest = _row_to_history(select_cols, tuple(latest_raw))

        try:
            window_start = (
                _parse_captured_at(latest.captured_at) - timedelta(days=_BASELINE_WINDOW_DAYS)
            ).isoformat(timespec="seconds")
        except ValueError:
            return latest, []

        try:
            baseline_raw = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM {_HISTORY_TABLE} "
                "WHERE user_id = ? AND captured_at < ? AND captured_at >= ? "
                "ORDER BY captured_at DESC",
                (user_id, latest.captured_at, window_start),
            ).fetchall()
        except sqlite3.Error as exc:
            log.debug({"event": "risk_drift_baseline_query_failed", "error": str(exc)})
            return latest, []
        baseline = [_row_to_history(select_cols, tuple(r)) for r in baseline_raw]
        return latest, baseline
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Drift math (pure — no DB, no LLM; the whole reason this is unit-testable)
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> tuple[float, int] | None:
    if not values:
        return None
    return sum(values) / len(values), len(values)


def _scalar_baseline_values(baseline: list[_HistoryRow], metric: str) -> list[float]:
    """Non-None baseline observations for one scalar metric. A small helper
    (rather than an inline filtered comprehension) so the None-check narrows
    the SAME expression it guards — ``r.metrics.get(metric) is not None``
    filtering a separately-indexed ``r.metrics[metric]`` doesn't narrow under
    static analysis even though it's correct at runtime."""
    out: list[float] = []
    for r in baseline:
        v = r.metrics.get(metric)
        if v is not None:
            out.append(v)
    return out


def _factor_baseline_values(baseline: list[_HistoryRow], factor: str) -> list[float]:
    """Non-None baseline observations for one C3 factor leg — same narrowing
    rationale as :func:`_scalar_baseline_values`."""
    out: list[float] = []
    for r in baseline:
        fv = r.factor_vector
        if fv is not None and factor in fv:
            out.append(fv[factor])
    return out


def compute_drift_findings(latest: _HistoryRow, baseline: list[_HistoryRow]) -> list[DriftFinding]:
    """Pure drift computation: latest vs trailing-baseline mean, per metric.

    A metric with no baseline observations (all-NULL history, or — for a
    factor leg — no baseline capture that carried that factor at all) is
    silently skipped: there is nothing to compare against, and a "drift"
    against an empty baseline is not a real signal. Returns findings in a
    stable order (scalar metrics first in ``_SCALAR_METRICS`` order, then
    factor legs sorted by label) so callers/tests get deterministic output.
    """
    findings: list[DriftFinding] = []
    for metric, threshold in _SCALAR_METRICS:
        latest_val = latest.metrics.get(metric)
        if latest_val is None:
            continue
        base = _mean(_scalar_baseline_values(baseline, metric))
        if base is None:
            continue
        mean, n = base
        magnitude = abs(latest_val - mean)
        if magnitude < threshold:
            continue
        findings.append(
            DriftFinding(
                metric=metric,
                latest=latest_val,
                baseline_mean=mean,
                baseline_n=n,
                direction="up" if latest_val > mean else "down",
                magnitude=magnitude,
                threshold=threshold,
            )
        )

    if latest.factor_vector:
        for factor in sorted(latest.factor_vector):
            latest_val = latest.factor_vector[factor]
            base = _mean(_factor_baseline_values(baseline, factor))
            if base is None:
                continue
            mean, n = base
            magnitude = abs(latest_val - mean)
            if magnitude < FACTOR_LEG_DRIFT_THRESHOLD:
                continue
            findings.append(
                DriftFinding(
                    metric=f"{_FACTOR_METRIC_PREFIX}{factor}",
                    latest=latest_val,
                    baseline_mean=mean,
                    baseline_n=n,
                    direction="up" if latest_val > mean else "down",
                    magnitude=magnitude,
                    threshold=FACTOR_LEG_DRIFT_THRESHOLD,
                )
            )
    return findings


def _bucket_magnitude(magnitude: float, threshold: float) -> int:
    """How many threshold-multiples the drift has crossed — the coarse bucket
    dedup keys on (see module docstring for why this is neither "re-fire
    forever" nor "never re-fire")."""
    if threshold <= 0:
        return 0
    return int(magnitude // threshold)


def signature_key_evidence(finding: DriftFinding) -> dict[str, object]:
    """The dedup key: (metric, direction, bucketed magnitude). Deliberately
    excludes the raw ``latest``/``baseline_mean`` values — those wobble by
    small amounts run to run even when the drift's severity band hasn't
    actually changed, which would defeat the bucketing."""
    return {
        "metric": finding.metric,
        "direction": finding.direction,
        "magnitude_bucket": _bucket_magnitude(finding.magnitude, finding.threshold),
    }


# ---------------------------------------------------------------------------
# Memo + evidence
# ---------------------------------------------------------------------------


def _compose_memo(finding: DriftFinding) -> str:
    verb = "risen" if finding.direction == "up" else "fallen"
    label = (
        finding.metric[len(_FACTOR_METRIC_PREFIX) :]
        if finding.metric.startswith(_FACTOR_METRIC_PREFIX)
        else finding.metric
    )
    return (
        f"{label} has {verb} to {finding.latest:.3f}, vs a {finding.baseline_n}-capture "
        f"{_BASELINE_WINDOW_DAYS}-day baseline of {finding.baseline_mean:.3f} "
        f"(|Δ| {finding.magnitude:.3f}, threshold {finding.threshold:.3f})."
    )


def build_evidence(finding: DriftFinding, latest: _HistoryRow) -> dict[str, object]:
    return {
        "summary": _compose_memo(finding),
        "metric": finding.metric,
        "direction": finding.direction,
        "latest": finding.latest,
        "baseline_mean": finding.baseline_mean,
        "baseline_n": finding.baseline_n,
        "magnitude": finding.magnitude,
        "threshold": finding.threshold,
        "window_days": _BASELINE_WINDOW_DAYS,
        "latest_captured_at": latest.captured_at,
    }


# ---------------------------------------------------------------------------
# Fire — the sensor entry point
# ---------------------------------------------------------------------------


def scan_and_fire(
    db_path: Path | str | None,
    *,
    user_id: str = DEFAULT_USER_ID,
    now: datetime | None = None,
) -> list[int]:
    """Compute drift findings off the latest snapshot vs its trailing
    baseline and fire ONE alert per breached, not-already-fired metric.

    Returns the ids of newly fired alerts (``[]`` on no drift, fewer than one
    baseline observation, a missing substrate, or any internal failure — this
    never raises; a failed drift scan is "no alert today", matching every
    other sensor's degrade contract, and it must never sink the risk writer
    it rides after).
    """
    try:
        latest, baseline = load_drift_inputs(db_path, user_id=user_id, now=now)
    except Exception as exc:  # substrate read must never propagate
        log.warning({"event": "risk_drift_load_failed", "error": str(exc)})
        return []
    if latest is None or not baseline:
        return []

    try:
        findings = compute_drift_findings(latest, baseline)
    except Exception as exc:
        log.warning({"event": "risk_drift_compute_failed", "error": str(exc)})
        return []
    if not findings:
        return []

    fired_at = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
    fired_ids: list[int] = []
    for finding in findings:
        key_evidence = signature_key_evidence(finding)
        sig = compute_signature_sha(TRIGGER_KIND, TICKER_SENTINEL, key_evidence)
        try:
            if find_by_signature(signature_sha=sig, db_path=db_path) is not None:
                continue  # same metric/direction/magnitude-band already fired and still live
            evidence = build_evidence(finding, latest)
            row = fire_alert(
                user_id=user_id,
                ticker=TICKER_SENTINEL,
                trigger_kind=TRIGGER_KIND,
                fired_at=fired_at,
                evidence_json=json.dumps(evidence, sort_keys=True, default=str),
                signature_sha=sig,
                db_path=db_path,
            )
            fired_ids.append(row.id)
            log.info(
                {
                    "event": "risk_drift_fired",
                    "metric": finding.metric,
                    "direction": finding.direction,
                    "magnitude": finding.magnitude,
                }
            )
        except Exception as exc:
            log.warning(
                {"event": "risk_drift_fire_failed", "metric": finding.metric, "error": str(exc)}
            )
    return fired_ids


# ---------------------------------------------------------------------------
# Writer extension — snapshot the C3 book-level factor vector (0197 additive
# column), called as a post-write hook from the risk writer.
# ---------------------------------------------------------------------------


def append_factor_vector(
    db_path: Path | str | None,
    repo_root: Path,
    *,
    user_id: str = DEFAULT_USER_ID,
) -> bool:
    """Best-effort: stamp the CURRENT C3 book-level factor vector
    (``risk_factors.book_factor_vector`` — a pure DB read, zero LLM, zero live
    tracker call) onto the just-written latest ``portfolio_risk_snapshot_history``
    row's additive ``factor_vector_json`` column (0197).

    Guarded at every layer per this module's degrade-don't-crash contract:
    ``risk_factors`` failing to import, the table/column being absent (pre-0197
    DB), no weighted holding carrying a persisted C3 loading yet, or any sqlite
    error all degrade to ``False`` — never raises, and never touches the
    scalar metric columns ``write_snapshot`` already wrote. Returns ``True``
    only on an actual write.
    """
    try:
        from risk_factors import book_factor_vector
    except ImportError as exc:
        log.debug({"event": "risk_drift_factor_vector_import_failed", "error": str(exc)})
        return False

    try:
        vector_result = book_factor_vector(db_path, repo_root)
    except Exception as exc:  # C3 read is best-effort here — never blocks the writer
        log.debug({"event": "risk_drift_factor_vector_read_failed", "error": str(exc)})
        return False
    if not vector_result.vector:
        return False

    conn = _open(db_path)
    if conn is None:
        return False
    try:
        if not _has_table(conn, _HISTORY_TABLE):
            return False
        cols = _table_columns(conn, _HISTORY_TABLE)
        if _FACTOR_COL not in cols:
            return False
        row = conn.execute(
            f"SELECT id FROM {_HISTORY_TABLE} WHERE user_id = ? ORDER BY captured_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return False
        history_id = int(row[0])
        payload = json.dumps(vector_result.vector, sort_keys=True)
        conn.execute(
            f"UPDATE {_HISTORY_TABLE} SET {_FACTOR_COL} = ? WHERE id = ?",
            (payload, history_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as exc:
        log.warning({"event": "risk_drift_factor_vector_write_failed", "error": str(exc)})
        return False
    finally:
        conn.close()


__all__ = [
    "FACTOR_LEG_DRIFT_THRESHOLD",
    "GROWTH_TILT_DRIFT_THRESHOLD",
    "SPY_BETA_DRIFT_THRESHOLD",
    "TICKER_SENTINEL",
    "TOP1_DRIFT_THRESHOLD_PCT",
    "TOP5_DRIFT_THRESHOLD_PCT",
    "TRIGGER_KIND",
    "DriftFinding",
    "append_factor_vector",
    "build_evidence",
    "compute_drift_findings",
    "load_drift_inputs",
    "scan_and_fire",
    "signature_key_evidence",
]
