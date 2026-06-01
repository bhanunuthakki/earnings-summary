"""§2 Thesis & tier-1 KPI ledger.

Reads the holdings JSON for narrative + break conditions, then joins to
kpi_definitions / kpi_facts in the DB to render historical KPI values where
they're tracked. KPIs without facts in the DB show empty history with a
status='unknown' — the holdings JSON remains the contract.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast

from report.models import (
    BreakRuleEvaluation,
    BreakRuleObservation,
    KpiLedgerRow,
    SectionStatus,
    SoftRuleEvaluation,
    ThesisSection,
)
from report.sections._common import has_table, missing, open_repo_db
from report.sections._ts_signals import (
    format_signals_as_prompt_block,
    load_signals_for_metrics,
)


def build(ticker: str, repo_root: Path) -> ThesisSection:
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not holdings_path.exists():
        return ThesisSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="INGEST(holdings)",
                fix_command=f"create {holdings_path.relative_to(repo_root)}",
            ),
        )
    with open(holdings_path, encoding="utf-8") as f:
        holdings = json.load(f)

    overall, evaluations, soft_evaluations, evaluated_at = _load_break_rule_state(
        ticker, repo_root
    )
    ledger = _build_ledger(ticker, repo_root, holdings, evaluations)
    thesis_text, stub_warning = _split_stub_warning(holdings)

    return ThesisSection(
        status=SectionStatus.OK if ledger else SectionStatus.PARTIAL,
        thesis_full=thesis_text,
        last_updated=_parse_date(holdings.get("last_updated")),
        stub_warning=stub_warning,
        break_conditions=list(holdings.get("break_conditions") or []),
        competitive_watchlist=list(holdings.get("competitive_watchlist") or []),
        qualitative_breakers=list(holdings.get("thesis_breakers_qualitative") or []),
        kpi_ledger=ledger,
        overall_breach_status=overall,
        break_rule_evaluations=evaluations,
        soft_rule_evaluations=soft_evaluations,
        last_evaluated_at=evaluated_at,
        ts_context_md=_ts_context_md(ticker, repo_root, holdings),
    )


def _ts_context_md(
    ticker: str, repo_root: Path, holdings: dict[str, object]
) -> str:
    """Render persisted signals for tier-1 KPIs + headline P&L lines.

    The block is the high-leverage TS surface for §2: the thesis
    statement should respect the current trend state of every Tier-1 KPI
    and the universal revenue / OI / FCF cuts. Empty string when no
    signals have been computed for any of the requested metrics.
    """
    names: list[str] = ["revenue", "operating_income", "free_cash_flow"]
    seen: set[str] = set(names)
    raw_kpis = holdings.get("tier_1_kpis")
    if isinstance(raw_kpis, list):
        for k in cast("list[object]", raw_kpis):
            if not isinstance(k, dict):
                continue
            name = cast("dict[str, object]", k).get("name")
            if isinstance(name, str) and name.strip() and name not in seen:
                names.append(name.strip())
                seen.add(name)
    grouped = load_signals_for_metrics(ticker, names, repo_root=repo_root)
    flat = [s for metric in grouped.values() for s in metric]
    return format_signals_as_prompt_block(
        flat, heading="Time-Series Context (last computed)"
    )


_STUB_STATUS_VALUES = frozenset({
    "stub_regenerated_from_corruption",
    "needs_user_review",
    "stub",
})

# Stale inline marker from commit 7b56cae's repair pass — strip it from the thesis
# text and lift it into a separate banner so the thesis paragraph reads cleanly.
_INLINE_STUB_MARKER_RX = re.compile(
    r"\s*\.?\s*STUB(?:\s+regenerated\s+from\s+corrupted\s+source)?\s*(?:[—-]\s*)?"
    r"needs?\s+user\s+review\.?\s*$",
    re.IGNORECASE,
)


def _split_stub_warning(holdings: dict[str, object]) -> tuple[str | None, str | None]:
    """Return (clean_thesis, banner) extracting any stub-status marker.

    Looks at (a) the explicit `_status` field, (b) any trailing
    `STUB ... needs user review.` sentence inlined into the thesis text. The
    cleaned thesis is what § renders; the banner is what shows above it.
    """
    status_field = holdings.get("_status")
    explicit = (
        str(status_field).strip()
        if isinstance(status_field, str) and str(status_field).strip().lower() in _STUB_STATUS_VALUES
        else None
    )

    raw_thesis = holdings.get("thesis")
    if not isinstance(raw_thesis, str):
        return (None, _stub_banner(explicit) if explicit else None)

    cleaned = _INLINE_STUB_MARKER_RX.sub("", raw_thesis).rstrip()
    if cleaned and not cleaned.endswith("."):
        cleaned += "."
    inline_match = cleaned != raw_thesis.rstrip()

    if explicit or inline_match:
        return (cleaned, _stub_banner(explicit or "stub_regenerated_from_corruption"))
    return (raw_thesis, None)


def _stub_banner(status_value: str) -> str:
    """Human-readable banner text for the stub-warning callout."""
    if status_value == "stub_regenerated_from_corruption":
        return (
            "This thesis was regenerated as a stub after the original holdings JSON "
            "was committed corrupt. KPI names, break thresholds, and tier ordering "
            "are placeholders — please review and replace before treating as ground truth."
        )
    if status_value == "needs_user_review":
        return "This thesis is a stub pending user review."
    return "This thesis is a stub — review before treating as ground truth."


def _parse_date(s: object) -> date | None:
    if not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _build_ledger(
    ticker: str,
    repo_root: Path,
    holdings: dict[str, object],
    evaluations: list[BreakRuleEvaluation],
) -> list[KpiLedgerRow]:
    """Tier order preserved (1 → 2 → 3); within each tier sorted alphabetically.

    KPI status sources, in priority order:
      1. Matching break-rule evaluation (by kpi_name) — propagate ok→green,
         warn→yellow, breach→red.
      2. Has kpi_facts history → green (data is flowing, but no rule fired).
      3. Otherwise → unknown.
    """
    # Index break-rule evaluations by KPI name for O(1) lookup. Worst status wins
    # if multiple rules name the same KPI.
    by_kpi: dict[str, str] = {}  # kpi_name (lowercased) → worst status seen
    rank = {"ok": 0, "warn": 1, "breach": 2}
    for ev in evaluations:
        key = ev.kpi_name.strip().lower()
        prev = by_kpi.get(key)
        if prev is None or rank.get(ev.status, 0) > rank.get(prev, 0):
            by_kpi[key] = ev.status

    rows: list[KpiLedgerRow] = []
    for tier_key, tier_label in (
        ("tier_1_kpis", "tier_1"),
        ("tier_2_kpis", "tier_2"),
        ("tier_3_kpis", "tier_3"),
    ):
        kpis = holdings.get(tier_key) or []
        if not isinstance(kpis, list):
            continue
        tier_rows: list[KpiLedgerRow] = []
        for k in kpis:
            if not isinstance(k, dict):
                continue
            name = str(k.get("name", ""))
            history, latest_excerpt = _kpi_history(ticker, repo_root, name)
            tier_rows.append(
                KpiLedgerRow(
                    name=name,
                    tier=tier_label,  # type: ignore[arg-type]
                    unit=str(k.get("unit")) if k.get("unit") else None,
                    source_hint=str(k.get("source")) if k.get("source") else None,
                    break_condition=str(k.get("break")) if k.get("break") else None,
                    history=history,
                    current_status=_status_for(name, history, by_kpi),
                    latest_source_excerpt=latest_excerpt,
                )
            )
        tier_rows.sort(key=lambda r: r.name.lower())
        rows.extend(tier_rows)
    return rows


def _status_for(
    name: str,
    history: list[tuple[str, float | None]],
    by_kpi: dict[str, str],
) -> Literal["green", "yellow", "red", "unknown"]:
    """Classify a tier-N KPI by (a) break-rule outcome if one names it, else (b) data presence."""
    rule_status = by_kpi.get(name.strip().lower())
    if rule_status == "breach":
        return "red"
    if rule_status == "warn":
        return "yellow"
    if rule_status == "ok":
        return "green"
    # No matching break rule: signal whether we have any data at all.
    if any(v is not None for _, v in history):
        return "green"
    return "unknown"


_BreachStatusLiteral = Literal["ok", "warn", "breach", "unknown"]


def _load_break_rule_state(
    ticker: str, repo_root: Path
) -> tuple[
    _BreachStatusLiteral,
    list[BreakRuleEvaluation],
    list[SoftRuleEvaluation],
    datetime | None,
]:
    """Pull the most recent thesis_evaluations row for `ticker` and parse it.

    Returns ('unknown', [], [], None) when the evaluator has never run for this
    ticker — the report still renders, the section just shows the missing-data
    callout for break-rules.

    `soft_rule_results_json` was added in migration 0053. On a DB that predates
    the migration the column is absent; we detect that and treat soft results
    as empty so the §2 renderer continues to work.
    """
    conn = open_repo_db(repo_root)
    if conn is None or not has_table(conn, "thesis_evaluations"):
        if conn is not None:
            conn.close()
        return ("unknown", [], [], None)
    cursor = conn.cursor()
    has_soft_col = any(
        c["name"] == "soft_rule_results_json"
        for c in cursor.execute("PRAGMA table_info(thesis_evaluations)").fetchall()
    )
    soft_select = ", soft_rule_results_json" if has_soft_col else ""
    cursor.execute(
        f"""
        SELECT evaluated_at, overall_status, rule_evaluations_json{soft_select}
        FROM thesis_evaluations
        WHERE ticker = ?
        ORDER BY evaluated_at DESC
        LIMIT 1
        """,
        (ticker.upper(),),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return ("unknown", [], [], None)
    overall = _coerce_status(str(row["overall_status"]))
    evaluated_at = _parse_datetime(row["evaluated_at"])
    payload = json.loads(row["rule_evaluations_json"])
    evaluations = [_parse_evaluation(item) for item in payload]
    soft_evaluations: list[SoftRuleEvaluation] = []
    if has_soft_col:
        soft_raw = row["soft_rule_results_json"]
        if soft_raw:
            try:
                soft_payload = json.loads(soft_raw)
            except (TypeError, ValueError):
                soft_payload = []
            soft_evaluations = [
                _parse_soft_evaluation(item)
                for item in soft_payload
                if isinstance(item, dict)
            ]
    return (overall, evaluations, soft_evaluations, evaluated_at)


def _parse_soft_evaluation(raw: dict[str, object]) -> SoftRuleEvaluation:
    """Parse one persisted soft-rule result row. Status defaults to green for
    forward-compatibility: any unknown status string is treated as 'not fired'
    so a brief generated against a newer schema never silently shows a YELLOW
    banner without explicit opt-in."""
    status_raw = str(raw.get("status", "green")).lower()
    status: Literal["green", "yellow"] = "yellow" if status_raw == "yellow" else "green"
    details_raw = raw.get("details")
    details: dict[str, object] = (
        details_raw if isinstance(details_raw, dict) else {}
    )
    return SoftRuleEvaluation(
        rule_name=str(raw.get("rule_name", "")),
        status=status,
        evidence=str(raw.get("evidence", "")),
        details=details,
    )


def _coerce_status(s: str) -> _BreachStatusLiteral:
    s = s.lower()
    if s in ("ok", "warn", "breach"):
        return s  # type: ignore[return-value]
    return "unknown"


def _parse_datetime(v: object) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def _parse_evaluation(raw: dict[str, object]) -> BreakRuleEvaluation:
    obs_raw = raw.get("observations") or []
    observations = [
        BreakRuleObservation(
            period_end=str(o.get("period_end", ""))[:10],
            value=float(str(o.get("value", "0"))),
            unit=str(o.get("unit", "")),
        )
        for o in obs_raw
        if isinstance(o, dict)
    ]
    return BreakRuleEvaluation(
        rule_id=str(raw.get("rule_id", "")),
        kpi_name=str(raw.get("kpi_name", "")),
        comparator=str(raw.get("comparator", "")),
        threshold=float(str(raw.get("threshold", "0"))),
        consecutive_periods=int(raw.get("consecutive_periods", 1) or 1),
        tier=_coerce_tier(raw.get("tier")),
        status=_coerce_status(str(raw.get("status", "unknown"))),  # type: ignore[arg-type]
        detail=str(raw.get("detail", "")),
        narrative=str(raw.get("narrative", "")),
        observations=observations,
    )


def _coerce_tier(v: object) -> Literal["universal", "business_model"]:
    """Map a persisted tier value to the literal enum.

    Pre-tier rows (written before this field existed) come back as None or
    missing. Treat them as business_model so the existing per-ticker overlay
    keeps rendering until a new evaluator run re-persists with explicit tiers.
    """
    if v == "universal":
        return "universal"
    return "business_model"


def _kpi_history(
    ticker: str, repo_root: Path, kpi_name: str
) -> tuple[list[tuple[str, float | None]], str | None]:
    """Return ([(period, value), ...], latest_source_excerpt or None).

    The history list is chronological (oldest → newest) so the brief's
    sparkline renders left-to-right. The latest_source_excerpt is pulled
    from the row with the most recent period_end whose source_excerpt is
    non-null — gives the reader provenance for the headline value without
    surfacing per-period quotes.
    """
    if not kpi_name:
        return [], None
    conn = open_repo_db(repo_root)
    if conn is None:
        return [], None
    if not (has_table(conn, "kpi_facts") and has_table(conn, "kpi_definitions")):
        conn.close()
        return [], None
    cursor = conn.cursor()
    # Defensive: source_excerpt may not exist on pre-0033 DBs. Detect column.
    has_excerpt_col = any(
        c["name"] == "source_excerpt"
        for c in cursor.execute("PRAGMA table_info(kpi_facts)").fetchall()
    )
    cols = "f.period_end, f.value" + (", f.source_excerpt" if has_excerpt_col else ", NULL AS source_excerpt")
    # Dedup coexisting rows for the same period (e.g. an LLM brief value plus the
    # issuer's later IR-spreadsheet restatement): keep only the latest-ingested
    # source per logical key so break-rule evaluation sees one observation per
    # quarter, matching the §3 chart loader and purge_duplicate_kpi_facts.
    cursor.execute(
        f"""
        SELECT {cols}
        FROM kpi_facts f
        JOIN kpi_definitions d ON d.id = f.kpi_definition_id
        WHERE d.ticker = ? AND d.name = ?
          AND f.source_doc_id = (
              SELECT MAX(f2.source_doc_id) FROM kpi_facts f2
              WHERE f2.ticker = f.ticker
                AND f2.kpi_definition_id = f.kpi_definition_id
                AND f2.period_end = f.period_end
                AND f2.fiscal_period_type = f.fiscal_period_type
          )
        ORDER BY f.period_end ASC
        """,
        (ticker.upper(), kpi_name),
    )
    out: list[tuple[str, float | None]] = []
    latest_excerpt: str | None = None
    for row in cursor.fetchall():
        period = str(row["period_end"])[:10]
        out.append((period, row["value"]))
        excerpt = row["source_excerpt"] if has_excerpt_col else None
        if excerpt:
            # Last non-null excerpt wins since rows are ASC by period_end —
            # the analyst's most-recent supplied quote is the most relevant
            # context for the headline value.
            latest_excerpt = excerpt
    conn.close()
    return out, latest_excerpt
