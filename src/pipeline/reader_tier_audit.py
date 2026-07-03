"""Reader-vs-tier audit + source-disagreement reconciliation (S-hardening PR2).

Two verbs over the ~162k FMP+SEC duplicated ``financial_facts`` keys the EDGAR
backfill created:

``audit_readers``
    Sample duplicated keys, compute the canonical tier-winner
    (``timeseries.loaders.load_financial_series`` — the ``(source_quality_tier,
    id)`` contract), and compare it against the materialized quarterly-financials
    reader (``report.sections.financials._load_quarterly`` / ``_load_annual``).
    A divergence means a reader regressed off the tier contract; it writes a
    ``READER_TIER_MISMATCH`` ``validation_issues`` row so the drift is visible in
    the Provenance console rather than silently serving an FMP number where SEC
    should win. Sample-based (not a full 1.1M-row scan) so it can run in a
    pipeline / CI-of-data step without a full-table walk on a render path.

``reconcile_source_disagreements``
    Triage the OPEN ``source_disagreement`` issues (the validation engine's
    cross-source sweep). For each, parse the two source values out of
    ``raw_value``; when the relative delta is at or below ``threshold_pct``
    (default 1%) AND one side is SEC, auto-resolve in SEC's favor with a
    ``resolution_note`` recording the call; a larger delta is left OPEN for a
    human. Returns counts so the caller (execution/reconcile_source_disagreements
    .py) can print a summary and the Provenance console can surface the residual
    open count.

Both are best-effort and read-mostly: ``audit_readers`` only INSERTs issue rows;
``reconcile_source_disagreements`` only UPDATEs the issues it auto-resolves. The
fact tables are never mutated.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from models.validation import Severity, ValidationRule
from pipeline.kpi_persistence import record_validation_issue
from timeseries.loaders import load_financial_series

log = logging.getLogger(__name__)

# Quarterly vs annual period-type families — mirrors report.sections._common so
# the audit reads each key on the same axis the financials reader renders it.
_QUARTERLY_FPTS: frozenset[str] = frozenset({"Q1", "Q2", "Q3", "Q4"})
_ANNUAL_FPTS: frozenset[str] = frozenset({"FY", "annual"})

# raw_value shape the source-disagreement writer (pipeline.validation_engine.
# _check_source_disagreement) emits:
#   "<line_item> @ <YYYY-MM-DD>: <a_st>=<a_val> vs <b_st>=<b_val> (<pct>%)"
_DISAGREEMENT_RE = re.compile(
    r"^(?P<item>\S+) @ (?P<date>\d{4}-\d{2}-\d{2}): "
    r"(?P<a_st>\w+)=(?P<a_val>-?[\d.eE+]+) vs "
    r"(?P<b_st>\w+)=(?P<b_val>-?[\d.eE+]+) "
    r"\((?P<pct>[\d.]+)%\)$"
)

_SEC_SOURCE_TYPE = "sec_xbrl"

# Default relative-delta cutoff (percent) below which an FMP-vs-SEC disagreement
# is treated as rounding/restatement noise and auto-resolved in SEC's favor.
DEFAULT_RECONCILE_THRESHOLD_PCT = 1.0


@dataclass(frozen=True, slots=True)
class DuplicatedKey:
    """One (ticker, period_end, fiscal_period_type, line_item) with ≥2 source
    types — a candidate for reader-vs-tier drift."""

    ticker: str
    period_end: str  # YYYY-MM-DD
    fiscal_period_type: str
    line_item: str


@dataclass(slots=True)
class ReaderAuditResult:
    """Outcome of :func:`audit_readers`."""

    keys_examined: int = 0
    mismatches: int = 0
    issues_written: int = 0
    # (ticker, line_item, period_end, canonical_value, reader_value) per mismatch.
    detail: list[tuple[str, str, str, float, float]] = field(
        default_factory=list[tuple[str, str, str, float, float]]
    )


@dataclass(slots=True)
class ReconcileResult:
    """Outcome of :func:`reconcile_source_disagreements`."""

    examined: int = 0
    auto_resolved: int = 0
    left_open: int = 0
    unparsed: int = 0


def sample_duplicated_keys(
    conn: sqlite3.Connection, *, limit: int = 200, ticker: str | None = None
) -> list[DuplicatedKey]:
    """Sample logical keys carrying ≥2 distinct ``documents.source_type`` values.

    Ordered newest-period-first so the sample favors the keys most likely to be
    read (recent quarters), capped at ``limit`` to keep the audit off a
    full-table scan. ``ticker`` restricts the sample when set."""
    sql = """
        SELECT ff.ticker,
               substr(ff.period_end, 1, 10) AS pe,
               ff.fiscal_period_type,
               ff.line_item
        FROM financial_facts ff
        JOIN documents d ON d.id = ff.source_doc_id
        {ticker_clause}
        GROUP BY ff.ticker, pe, ff.fiscal_period_type, ff.line_item
        HAVING COUNT(DISTINCT d.source_type) >= 2
        ORDER BY pe DESC
        LIMIT ?
    """
    params: list[object] = []
    ticker_clause = ""
    if ticker is not None:
        ticker_clause = "WHERE ff.ticker = ?"
        params.append(ticker.upper())
    params.append(limit)
    try:
        rows = conn.execute(sql.format(ticker_clause=ticker_clause), params).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "reader_tier_audit_sample_failed", "error": str(exc)})
        return []
    return [
        DuplicatedKey(
            ticker=str(r[0]),
            period_end=str(r[1]),
            fiscal_period_type=str(r[2]),
            line_item=str(r[3]),
        )
        for r in rows
    ]


def _canonical_value(db_path: Path, key: DuplicatedKey) -> float | None:
    """The tier-winner value the canonical loader picks for ``key``.

    Reads the same axis (quarterly vs annual period types) the financials reader
    renders the key on so the two are directly comparable."""
    period_types = (
        tuple(_ANNUAL_FPTS) if key.fiscal_period_type in _ANNUAL_FPTS else tuple(_QUARTERLY_FPTS)
    )
    series = load_financial_series(
        key.ticker, key.line_item, db_path=db_path, period_types=period_types
    )
    for obs in series:
        if str(obs.period_end)[:10] == key.period_end:
            return float(obs.value)
    return None


def _financials_reader_value(conn: sqlite3.Connection, key: DuplicatedKey) -> float | None:
    """The value ``report.sections.financials`` materializes for ``key``.

    Imported lazily so the audit module doesn't pull the report layer at import
    time. Maps the financials pivot column back to the fact line_item."""
    import report.sections.financials as fin_section

    col_map = fin_section._COL_TO_FACT_LINE_ITEM  # pyright: ignore[reportPrivateUsage]
    load_annual = fin_section._load_annual  # pyright: ignore[reportPrivateUsage]
    load_quarterly = fin_section._load_quarterly  # pyright: ignore[reportPrivateUsage]

    fact_to_col = {v: k for k, v in col_map.items()}
    col = fact_to_col.get(key.line_item)
    if col is None:
        return None  # not a column §3 renders — nothing to compare
    rows = (
        load_annual(conn, key.ticker)
        if key.fiscal_period_type in _ANNUAL_FPTS
        else load_quarterly(conn, key.ticker)
    )
    for r in rows:
        if str(r.get("period_end"))[:10] == key.period_end:
            raw = r.get(col)
            if raw is None:
                return None
            try:
                return float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
    return None


def _values_diverge(a: float, b: float) -> bool:
    """True when a and b differ by more than a hair (relative), so float
    round-trip noise from the TEXT-stored values isn't flagged."""
    scale = max(abs(a), abs(b))
    if scale == 0:
        return abs(a - b) > 1e-6
    return abs(a - b) / scale > 1e-4


def audit_readers(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    db_path: Path,
    limit: int = 200,
    ticker: str | None = None,
    dry_run: bool = False,
) -> ReaderAuditResult:
    """Sample duplicated keys and flag any reader-vs-tier mismatch.

    For each sampled key, compare the financials reader's materialized value to
    the canonical tier-winner (:func:`_canonical_value`). On divergence, write a
    ``READER_TIER_MISMATCH`` validation_issues row (severity WARN) whose
    ``raw_value`` names the key + both values so the console popover reads it.
    ``db_path`` is the same DB ``conn`` is open on (the canonical loader opens
    its own read connection). ``dry_run`` counts mismatches without writing
    issue rows. Returns the tally."""
    result = ReaderAuditResult()
    for key in sample_duplicated_keys(conn, limit=limit, ticker=ticker):
        canonical = _canonical_value(db_path, key)
        if canonical is None:
            continue
        reader_val = _financials_reader_value(conn, key)
        result.keys_examined += 1
        if reader_val is None:
            continue
        if not _values_diverge(canonical, reader_val):
            continue
        result.mismatches += 1
        result.detail.append((key.ticker, key.line_item, key.period_end, canonical, reader_val))
        if dry_run:
            continue
        try:
            record_validation_issue(
                conn,
                run_id=run_id,
                source_doc_id=None,
                ticker=key.ticker,
                severity=Severity.WARN,
                rule=ValidationRule.READER_TIER_MISMATCH,
                raw_value=(
                    f"{key.line_item} @ {key.period_end} [{key.fiscal_period_type}]: "
                    f"financials reader={reader_val} vs tier-winner={canonical}"
                ),
                expected="reader materializes the (source_quality_tier, id) winner",
            )
            result.issues_written += 1
        except sqlite3.Error as exc:
            log.warning(
                {"event": "reader_tier_audit_write_failed", "key": str(key), "error": str(exc)}
            )
    if not dry_run:
        conn.commit()
    return result


def _naive_utc_now() -> str:
    """Naive-UTC 'YYYY-MM-DD HH:MM:SS' stamp — the repo storage convention."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def reconcile_source_disagreements(
    conn: sqlite3.Connection,
    *,
    resolved_by: str = "reader_tier_reconcile",
    threshold_pct: float = DEFAULT_RECONCILE_THRESHOLD_PCT,
    dry_run: bool = False,
) -> ReconcileResult:
    """Triage OPEN ``source_disagreement`` issues; auto-resolve the near-agreements.

    A row whose parsed relative delta is <= ``threshold_pct`` AND names SEC on
    one side is auto-resolved (``resolved_at`` / ``resolved_by`` /
    ``resolution_note`` set in one UPDATE, scoped to ``resolved_at IS NULL``) in
    SEC's favor — the reader already serves the SEC value post-PR1, so the
    disagreement is rounding/restatement noise. A larger delta, or a
    non-SEC-vs-anything disagreement, is LEFT OPEN for a human. ``dry_run``
    counts without writing. Returns the tally; the residual ``left_open`` is what
    the Provenance console surfaces."""
    result = ReconcileResult()
    try:
        rows = conn.execute(
            "SELECT id, raw_value FROM validation_issues WHERE rule = ? AND resolved_at IS NULL",
            (ValidationRule.SOURCE_DISAGREEMENT.value,),
        ).fetchall()
    except sqlite3.Error as exc:
        log.warning({"event": "reconcile_disagreements_read_failed", "error": str(exc)})
        return result

    stamp = _naive_utc_now()
    for row in rows:
        result.examined += 1
        raw = str(row[1] or "")
        m = _DISAGREEMENT_RE.match(raw)
        if m is None:
            result.unparsed += 1
            result.left_open += 1
            continue
        pct = float(m["pct"])
        sources = {m["a_st"], m["b_st"]}
        sec_involved = _SEC_SOURCE_TYPE in sources
        if pct <= threshold_pct and sec_involved:
            note = (
                f"auto-resolved in SEC's favor: {pct:.2f}% <= {threshold_pct:.2f}% "
                f"relative delta between {m['a_st']} and {m['b_st']}; the tier-aware "
                f"readers already serve the SEC row (reader_tier_audit PR2)."
            )
            if not dry_run:
                try:
                    conn.execute(
                        "UPDATE validation_issues "
                        "SET resolved_at = ?, resolved_by = ?, resolution_note = ? "
                        "WHERE id = ? AND resolved_at IS NULL",
                        (stamp, resolved_by, note, int(row[0])),
                    )
                except sqlite3.Error as exc:
                    log.warning(
                        {"event": "reconcile_resolve_failed", "id": row[0], "error": str(exc)}
                    )
                    result.left_open += 1
                    continue
            result.auto_resolved += 1
        else:
            result.left_open += 1
    if not dry_run:
        conn.commit()
    return result


def open_source_disagreement_count(conn: sqlite3.Connection) -> int:
    """Number of OPEN ``source_disagreement`` issues — what the Provenance
    console surfaces after reconciliation. 0 when the table is absent."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM validation_issues WHERE rule = ? AND resolved_at IS NULL",
            (ValidationRule.SOURCE_DISAGREEMENT.value,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0
