"""Read-only data accessor for the Company tab's "Sector context" card
(docs/design/comparable_sets_bottoms_up.md §11, Phase 3 — the first render
consumer of ``comp_set_metrics_daily``).

Mirrors ``p3_data.py``'s accessor shape (best-effort: missing table/no
frozen set -> ``None``, never raises) but lives in its own module since it
composes THREE data layers the P3 accessors don't touch: the
``comparable_sets``/``comparable_set_members``/``comp_set_metrics_daily``
tables (Phase 1/2), the subject's own per-member snapshot computed with the
SAME construction as every comp-set member (``compute.comp_set_metrics``, so
"subject vs comp-set" is an apples-to-apples comparison, not a re-derivation),
and the owner-ratified ``sector_benchmark_map`` (Phase 3).

Hide-don't-stub: a ticker with no frozen comparable set yet (most of the pool,
on a phased rollout) returns ``None`` and the Company tab renders nothing new
— this is an additive card, not a "not built yet" callout on every report.
Once a set exists, coverage/staleness are always shown, never hidden (§5.5) —
this module never drops a thin or stale row, only the renderer decides how to
present it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from report.render_clock import render_today
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

log = logging.getLogger(__name__)

# Daily cadence (§8) -- more than ~3 missed runs is worth flagging as stale
# rather than silently presenting week-old numbers as current.
STALE_AFTER_DAYS = 10

# Human labels + display kind for each metric this module surfaces. 'x' = bare
# multiple (15.2x), 'pct' = percentage (already-scaled for rev_yoy, a raw
# fraction for fcf_yield_ttm -- see _fmt_metric_value).
_METRIC_LABELS: dict[str, str] = {
    "pe_ttm": "P/E (TTM)",
    "ev_ebitda_ttm": "EV/EBITDA (TTM)",
    "p_b": "Price/Book",
    "p_tbv": "Price/Tangible book",
    "rev_yoy": "Revenue YoY",
    "fcf_yield_ttm": "FCF yield (TTM)",
}
_PCT_METRICS = frozenset({"rev_yoy", "fcf_yield_ttm"})
_OPERATING_PRIMARY = ("pe_ttm", "ev_ebitda_ttm")
_FINANCIAL_PRIMARY = ("p_b", "p_tbv")
_SECONDARY_ALWAYS = ("rev_yoy", "fcf_yield_ttm")


@dataclass(frozen=True)
class CompSetMetricLine:
    """One metric's subject-vs-comp-set-vs-scope reading for the card."""

    metric: str
    label: str
    is_pct: bool
    subject_value: float | None
    median_value: float | None
    aggregate_value: float | None
    n_members: int
    n_valid_median: int
    coverage_pct_median: float
    flags: tuple[str, ...] = ()
    secondary: bool = False


@dataclass(frozen=True)
class CompSetScopeSummary:
    """Pool-wide industry/sector slice reading (§6) -- the multiples
    benchmark per §4.1, point 2 (NOT derived from the ETF's holdings)."""

    scope_type: str  # 'industry'|'sector'
    scope_key: str
    as_of_date: date | None
    n_members: int
    pe_ttm_median: float | None
    stale: bool


@dataclass(frozen=True)
class CompSetMemberRef:
    ticker: str
    name: str | None
    membership_reason: str
    context_only: bool


@dataclass(frozen=True)
class CompSetContextSection:
    ticker: str
    comparable_set_id: str
    method_version: int
    metric_class: str  # 'operating'|'financial'|'reit'
    as_of_date: date | None
    stale: bool
    n_members: int
    primary_metrics: tuple[CompSetMetricLine, ...]
    secondary_metrics: tuple[CompSetMetricLine, ...]
    industry: str | None
    sector: str | None
    industry_scope: CompSetScopeSummary | None
    sector_scope: CompSetScopeSummary | None
    benchmark_etf: str | None
    benchmark_sector_etf: str | None
    benchmark_note: str
    members: tuple[CompSetMemberRef, ...]


# ---------------------------------------------------------------------------
# small, best-effort DB helpers (duplicated from p3_data.py's idiom per repo
# convention -- shared logic this small is meant to be duplicated, not
# modularized, so each accessor can evolve its edge cases independently)
# ---------------------------------------------------------------------------


def _open(db_path: Path | str) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        conn = connect_sqlite(p, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        log.warning({"event": "comp_set_context_open_failed", "error": str(exc)})
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def _first_record(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, list):
        items = cast("list[object]", raw)
        raw = items[0] if items else None
    if not isinstance(raw, dict):
        return None
    return cast("dict[str, object]", raw)


def _str_or_none(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _industry_sector_for_ticker(repo_root: Path, ticker: str) -> tuple[str | None, str | None]:
    rec = _first_record(
        repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    )
    if rec is None:
        return None, None
    return _str_or_none(rec.get("industry")), _str_or_none(rec.get("sector"))


def _int_or(d: dict[str, object], key: str, default: int = 0) -> int:
    v = d.get(key)
    return int(cast("int", v)) if isinstance(v, (int, float)) else default


def _float_or(d: dict[str, object], key: str, default: float = 0.0) -> float:
    v = d.get(key)
    return float(cast("float", v)) if isinstance(v, (int, float)) else default


def _parse_date(raw: object) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# comp_set_metrics_daily readers
# ---------------------------------------------------------------------------


def _latest_as_of(
    conn: sqlite3.Connection, scope_type: str, scope_key: str, method_version: int
) -> date | None:
    row = conn.execute(
        "SELECT MAX(as_of_date) AS d FROM comp_set_metrics_daily "
        "WHERE scope_type = ? AND scope_key = ? AND method_version = ?",
        (scope_type, scope_key, method_version),
    ).fetchone()
    if row is None or row["d"] is None:
        return None
    return _parse_date(row["d"])


def _metric_rows_at(
    conn: sqlite3.Connection,
    scope_type: str,
    scope_key: str,
    as_of: date,
    method_version: int,
) -> dict[tuple[str, str], dict[str, object]]:
    """(metric, stat_type) -> {value, n_members, n_valid, coverage_pct, method_flags}."""
    rows = conn.execute(
        "SELECT metric, stat_type, value, n_members, n_valid, coverage_pct, method_flags "
        "FROM comp_set_metrics_daily "
        "WHERE scope_type = ? AND scope_key = ? AND as_of_date = ? AND method_version = ?",
        (scope_type, scope_key, as_of.isoformat(), method_version),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, object]] = {}
    for r in rows:
        flags: object = {}
        try:
            flags = json.loads(r["method_flags"]) if r["method_flags"] else {}
        except json.JSONDecodeError:
            flags = {}
        out[(str(r["metric"]), str(r["stat_type"]))] = {
            "value": r["value"],
            "n_members": int(r["n_members"]),
            "n_valid": int(r["n_valid"]),
            "coverage_pct": float(r["coverage_pct"]),
            "method_flags": flags if isinstance(flags, dict) else {},
        }
    return out


def _open_members(conn: sqlite3.Connection, comparable_set_id: str) -> list[tuple[str, str, bool]]:
    rows = conn.execute(
        "SELECT member_ticker, membership_reason, context_only FROM comparable_set_members "
        "WHERE comparable_set_id = ? AND valid_to IS NULL "
        "ORDER BY context_only ASC, member_ticker ASC",
        (comparable_set_id,),
    ).fetchall()
    return [
        (str(r["member_ticker"]), str(r["membership_reason"]), bool(r["context_only"]))
        for r in rows
    ]


def _member_names(conn: sqlite3.Connection, tickers: list[str]) -> dict[str, str]:
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    rows = conn.execute(
        f"SELECT ticker, name FROM tracked_companies WHERE ticker IN ({placeholders}) AND name <> ''",
        tickers,
    ).fetchall()
    return {str(r["ticker"]).upper(): str(r["name"]) for r in rows}


def _build_metric_line(
    metric: str,
    subject_rows: dict[tuple[str, str], dict[str, object]],
    scope_rows: dict[tuple[str, str], dict[str, object]],
    *,
    secondary: bool,
) -> CompSetMetricLine:
    med = scope_rows.get((metric, "median"), {})
    agg = scope_rows.get((metric, "aggregate"), {})
    subj_med = subject_rows.get((metric, "median"), {})
    subj_agg = subject_rows.get((metric, "aggregate"), {})
    subject_value = subj_med.get("value")
    if subject_value is None:
        subject_value = subj_agg.get("value")
    flags: list[str] = []
    for src in (med, agg):
        mf = src.get("method_flags")
        if isinstance(mf, dict):
            flags.extend(str(k) for k in cast("dict[str, object]", mf))
    n_members = _int_or(med, "n_members") or _int_or(agg, "n_members")
    return CompSetMetricLine(
        metric=metric,
        label=_METRIC_LABELS.get(metric, metric),
        is_pct=metric in _PCT_METRICS,
        subject_value=cast("float | None", subject_value),
        median_value=cast("float | None", med.get("value")),
        aggregate_value=cast("float | None", agg.get("value")),
        n_members=n_members,
        n_valid_median=_int_or(med, "n_valid"),
        coverage_pct_median=_float_or(med, "coverage_pct"),
        flags=tuple(sorted(set(flags))),
        secondary=secondary,
    )


def _scope_summary(
    conn: sqlite3.Connection, scope_type: str, scope_key: str, method_version: int
) -> CompSetScopeSummary | None:
    as_of = _latest_as_of(conn, scope_type, scope_key, method_version)
    if as_of is None:
        return None
    rows = _metric_rows_at(conn, scope_type, scope_key, as_of, method_version)
    med = rows.get(("pe_ttm", "median"), {})
    stale = (render_today() - as_of) > timedelta(days=STALE_AFTER_DAYS)
    return CompSetScopeSummary(
        scope_type=scope_type,
        scope_key=scope_key,
        as_of_date=as_of,
        n_members=_int_or(med, "n_members"),
        pe_ttm_median=cast("float | None", med.get("value")),
        stale=stale,
    )


def load_comp_set_context(
    ticker: str, *, db_path: Path | str, repo_root: Path
) -> CompSetContextSection | None:
    """The Company tab's "Sector context" card data, or ``None`` when
    ``ticker`` has no frozen comparable set yet (hide-don't-stub — most of
    the pool, on a phased rollout)."""
    from compute.comp_set_metrics import compute_comparable_set_metrics
    from compute.comparable_sets import METHOD_VERSION, ComparableSetMember
    from compute.comparable_sets import comparable_set_id as _set_id
    from compute.sector_benchmark_map import get_benchmark_proxy

    ticker = ticker.upper()
    conn = _open(db_path)
    if conn is None or not _table_exists(conn, "comparable_sets"):
        return None
    try:
        set_id = _set_id(ticker, METHOD_VERSION)
        set_row = conn.execute(
            "SELECT metric_class FROM comparable_sets WHERE comparable_set_id = ?", (set_id,)
        ).fetchone()
        if set_row is None:
            return None
        metric_class = str(set_row["metric_class"])

        as_of = _latest_as_of(conn, "comparable_set", set_id, METHOD_VERSION)
        member_tuples = _open_members(conn, set_id)
        n_members = sum(1 for _t, _r, ctx in member_tuples if not ctx)

        scope_rows: dict[tuple[str, str], dict[str, object]] = {}
        subject_rows: dict[tuple[str, str], dict[str, object]] = {}
        if as_of is not None:
            scope_rows = _metric_rows_at(conn, "comparable_set", set_id, as_of, METHOD_VERSION)
            try:
                subj_metric_rows = compute_comparable_set_metrics(
                    repo_root,
                    [ComparableSetMember(ticker, "subject", context_only=False)],
                    metric_class,
                    as_of,
                )
                for r in subj_metric_rows:
                    subject_rows[(r.metric, r.stat_type)] = {
                        "value": r.value,
                        "n_members": r.n_members,
                        "n_valid": r.n_valid,
                        "coverage_pct": r.coverage_pct,
                        "method_flags": r.method_flags,
                    }
            except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
                log.warning(
                    {
                        "event": "comp_set_context_subject_snapshot_failed",
                        "ticker": ticker,
                        "error": str(exc),
                    }
                )

        stale = as_of is None or (render_today() - as_of) > timedelta(days=STALE_AFTER_DAYS)

        primary_keys = _OPERATING_PRIMARY if metric_class == "operating" else _FINANCIAL_PRIMARY
        primary_metrics = tuple(
            _build_metric_line(m, subject_rows, scope_rows, secondary=False)
            for m in primary_keys
            if metric_class != "reit"
        )
        secondary_keys = list(_SECONDARY_ALWAYS)
        if metric_class == "financial":
            secondary_keys = ["pe_ttm", *secondary_keys]
        secondary_metrics = tuple(
            _build_metric_line(m, subject_rows, scope_rows, secondary=True)
            for m in secondary_keys
            if metric_class != "reit"
        )

        industry, sector = _industry_sector_for_ticker(repo_root, ticker)
        industry_scope = (
            _scope_summary(conn, "industry", industry, METHOD_VERSION) if industry else None
        )
        sector_scope = _scope_summary(conn, "sector", sector, METHOD_VERSION) if sector else None

        proxy = get_benchmark_proxy(industry)
        if proxy is None:
            benchmark_etf, benchmark_sector_etf, note = (
                None,
                None,
                "no ratified benchmark for this industry yet",
            )
        else:
            benchmark_etf = proxy.etf
            benchmark_sector_etf = proxy.sector_etf
            note = proxy.note

        member_names = _member_names(conn, [t for t, _r, _c in member_tuples])
        members = tuple(
            CompSetMemberRef(
                ticker=t,
                name=member_names.get(t),
                membership_reason=reason,
                context_only=ctx,
            )
            for t, reason, ctx in member_tuples
        )

        return CompSetContextSection(
            ticker=ticker,
            comparable_set_id=set_id,
            method_version=METHOD_VERSION,
            metric_class=metric_class,
            as_of_date=as_of,
            stale=stale,
            n_members=n_members,
            primary_metrics=primary_metrics,
            secondary_metrics=secondary_metrics,
            industry=industry,
            sector=sector,
            industry_scope=industry_scope,
            sector_scope=sector_scope,
            benchmark_etf=benchmark_etf,
            benchmark_sector_etf=benchmark_sector_etf,
            benchmark_note=note,
            members=members,
        )
    finally:
        conn.close()
