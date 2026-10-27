"""Read-only data accessors for the P3 workspace panels.

The audit memo flagged six "captured but not surfaced" subsystems whose
data sits in portfolio.db but never reaches the workspace HTML:

  P3-17  decision history          -> decisions (alembic 0046)
  P3-18  macro sensitivities       -> macro_sensitivities (alembic 0045)
  P3-19a customer concentrations   -> customer_concentrations (alembic 0040)
  P3-19b lease commitment ladder   -> lease_commitments (alembic 0047)
  P3-20  strategic targets         -> strategic_targets (alembic 0053)
  P3-21  Say-Do verdict history    -> management_commitments (alembic 0017)

This module is the read side: pure Pydantic-shaped accessors that return
typed rows. The renderer (workspace_html.py) consumes them in a follow-on
PR — keeping the data + render concerns separate so the accessors can
be unit-tested without spinning up the full report pipeline.

Standalone accessors preserve their optional-data contract: missing table -> [].
The composite loader separately records unavailable sources. Supplied connections
remain caller-owned, including their row factory and transaction state.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import TypeVar, cast

from advisor.store import count_memos
from macro_store import fetch_sensitivities
from models.documents import DocType, SourceType
from report.models import SayDoVerdictRow as SayDoVerdictRow
from report.sections.comp_set_context import load_comp_set_context
from report.workspace_models import (
    CustomerConcentrationRow,
    DecisionHistorySummary,
    DecisionRow,
    LeaseLadderRow,
    MacroSensitivityRow,
    PanelAvailability,
    PeerCompRow,
    StandingRuleRow,
    StandingRulesPanel,
    StrategicTargetRow,
    WorkspaceP3Panels,
)
from runtime.deterministic import table_exists as _table_exists
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from user_state.notes import AnalystNoteRow, list_notes

_ReadResult = TypeVar("_ReadResult")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path | str | None) -> sqlite3.Connection | None:
    if db_path is None:
        return None
    p = Path(db_path)
    if not p.exists():
        return None
    try:
        conn = connect_sqlite(p, role=SQLiteConnectionRole.READ_ONLY)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        log.warning({"event": "p3_open_failed", "error": str(exc)})
        return None


def _parse_dt(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _normalized_issuer_ticker(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().casefold()
    return normalized or None


def _normalized_source_kind(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().casefold()
    return normalized or None


def _parse_date(raw: object) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    dt = _parse_dt(raw)
    return dt.date() if dt is not None else None


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def load_macro_sensitivities(
    ticker: str, *, db_path: Path | str | None = None, conn: sqlite3.Connection | None = None
) -> list[MacroSensitivityRow]:
    """Admitted estimates only; legacy rate rows remain quarantined."""
    db_conn = conn if conn is not None else _open(db_path)
    if db_conn is None:
        return []
    try:
        if not _table_exists(db_conn, "macro_sensitivities"):
            return []
        return [
            MacroSensitivityRow(
                series_id=r.series_id,
                beta=r.beta,
                r_squared=r.r_squared,
                lookback_window_days=r.lookback_window_days,
                computed_at=r.computed_at,
                metric_version=r.metric_version,
                shock_unit=r.shock_unit,
                input_sha=r.input_sha,
                source_as_of=r.source_as_of,
            )
            for r in sorted(
                fetch_sensitivities(ticker=ticker, conn=db_conn),
                key=lambda r: -abs(r.beta),
            )
        ]
    finally:
        if conn is None:
            db_conn.close()


def load_strategic_targets(
    ticker: str, *, db_path: Path | str | None = None, conn: sqlite3.Connection | None = None
) -> list[StrategicTargetRow]:
    """All strategic_targets rows for `ticker`, newest extraction first."""
    db_conn = conn if conn is not None else _open(db_path)
    if db_conn is None:
        return []
    original_factory = db_conn.row_factory
    db_conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(db_conn, "strategic_targets"):
            return []
        rows = db_conn.execute(
            """
            SELECT target_kind, target_value, target_unit, target_period,
                   target_currency, narrative_excerpt, confidence, extracted_at
            FROM strategic_targets
            WHERE ticker = ?
            ORDER BY extracted_at DESC, id DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        if conn is None:
            db_conn.close()
        else:
            db_conn.row_factory = original_factory
    return [
        StrategicTargetRow(
            target_kind=str(r["target_kind"]),
            target_value=(float(r["target_value"]) if r["target_value"] is not None else None),
            target_unit=str(r["target_unit"]),
            target_period=str(r["target_period"]),
            target_currency=r["target_currency"],
            narrative_excerpt=str(r["narrative_excerpt"]),
            confidence=float(r["confidence"]),
        )
        for r in rows
    ]


def load_customer_concentrations(
    ticker: str, *, db_path: Path | str | None = None, conn: sqlite3.Connection | None = None
) -> list[CustomerConcentrationRow]:
    """All customer_concentrations rows for `ticker`, newest period first.

    Only returns rows where pct_of_revenue >= 0.05 (5%) — the audit's
    threshold for "material concentration risk". Adjust at call site if
    you want the full table.
    """
    db_conn = conn if conn is not None else _open(db_path)
    if db_conn is None:
        return []
    original_factory = db_conn.row_factory
    db_conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(db_conn, "customer_concentrations"):
            return []
        rows = db_conn.execute(
            """
            SELECT fiscal_period, fiscal_period_type, customer_label,
                   pct_of_revenue, revenue_amount, revenue_currency
            FROM customer_concentrations
            WHERE ticker = ?
              AND pct_of_revenue >= 0.05
            ORDER BY fiscal_period DESC, pct_of_revenue DESC
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        if conn is None:
            db_conn.close()
        else:
            db_conn.row_factory = original_factory
    return [
        CustomerConcentrationRow(
            fiscal_period=str(r["fiscal_period"]),
            fiscal_period_type=str(r["fiscal_period_type"]),
            customer_label=str(r["customer_label"]),
            pct_of_revenue=float(r["pct_of_revenue"]),
            revenue_amount=(
                float(r["revenue_amount"]) if r["revenue_amount"] is not None else None
            ),
            revenue_currency=r["revenue_currency"],
        )
        for r in rows
    ]


def load_lease_ladder(
    ticker: str,
    *,
    db_path: Path | str | None = None,
    lease_type: str = "operating",
    conn: sqlite3.Connection | None = None,
) -> list[LeaseLadderRow]:
    """The most recent lease maturity ladder for `ticker`.

    Selects the max fiscal_year present in the table, then returns every
    ladder_year row for that vintage so the renderer can sort them
    Y1..Y5..Thereafter without juggling vintages.
    """
    db_conn = conn if conn is not None else _open(db_path)
    if db_conn is None:
        return []
    original_factory = db_conn.row_factory
    db_conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(db_conn, "lease_commitments"):
            return []
        latest_year_row = db_conn.execute(
            """
            SELECT MAX(fiscal_year) AS fy FROM lease_commitments
            WHERE ticker = ? AND lease_type = ?
            """,
            (ticker.upper(), lease_type),
        ).fetchone()
        if latest_year_row is None or latest_year_row["fy"] is None:
            return []
        latest_fy = int(latest_year_row["fy"])
        rows = db_conn.execute(
            """
            SELECT fiscal_year, as_of_date, lease_type, ladder_year,
                   ladder_calendar_year, amount, currency, unit
            FROM lease_commitments
            WHERE ticker = ? AND lease_type = ? AND fiscal_year = ?
            ORDER BY
              CASE ladder_year
                WHEN 'Y1' THEN 1 WHEN 'Y2' THEN 2 WHEN 'Y3' THEN 3
                WHEN 'Y4' THEN 4 WHEN 'Y5' THEN 5
                WHEN 'Thereafter' THEN 6
                WHEN 'TotalPayments' THEN 7
                WHEN 'ImputedInterest' THEN 8
                WHEN 'LeaseLiability' THEN 9
                ELSE 10
              END
            """,
            (ticker.upper(), lease_type, latest_fy),
        ).fetchall()
    finally:
        if conn is None:
            db_conn.close()
        else:
            db_conn.row_factory = original_factory
    out: list[LeaseLadderRow] = []
    for r in rows:
        as_of = _parse_date(r["as_of_date"])
        if as_of is None:
            continue
        out.append(
            LeaseLadderRow(
                fiscal_year=int(r["fiscal_year"]),
                as_of_date=as_of,
                lease_type=str(r["lease_type"]),
                ladder_year=str(r["ladder_year"]),
                ladder_calendar_year=(
                    int(r["ladder_calendar_year"])
                    if r["ladder_calendar_year"] is not None
                    else None
                ),
                amount=float(r["amount"]),
                currency=str(r["currency"]),
                unit=str(r["unit"]),
            )
        )
    return out


def load_decision_history(
    ticker: str,
    *,
    db_path: Path | str | None = None,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> DecisionHistorySummary:
    """Aggregate decision audit rows for `ticker`.

    Returns counts by recommendation_kind + conviction, an overall win-rate
    (fraction of decisions where outcome_pct > 0 OR outcome_status=='correct'),
    and the last `limit` raw rows for the time-series chart.
    """
    db_conn = conn if conn is not None else _open(db_path)
    if db_conn is None:
        return DecisionHistorySummary(
            total=0,
            by_kind={},
            by_conviction={},
            win_rate_overall=None,
            rows=[],
        )
    original_factory = db_conn.row_factory
    db_conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(db_conn, "decisions"):
            return DecisionHistorySummary(
                total=0,
                by_kind={},
                by_conviction={},
                win_rate_overall=None,
                rows=[],
            )
        rows = db_conn.execute(
            f"""
            SELECT recommendation_kind, recommendation_value, conviction,
                   made_at, outcome_pct, outcome_at, rationale_excerpt
            FROM decisions
            WHERE ticker = ?
            ORDER BY made_at DESC
            LIMIT {int(limit)}
            """,
            (ticker.upper(),),
        ).fetchall()
    finally:
        if conn is None:
            db_conn.close()
        else:
            db_conn.row_factory = original_factory
    by_kind: dict[str, int] = {}
    by_conviction: dict[str, int] = {}
    decisions: list[DecisionRow] = []
    wins = 0
    counted = 0
    for r in rows:
        made = _parse_dt(r["made_at"])
        if made is None:
            continue
        kind = str(r["recommendation_kind"])
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if r["conviction"] is not None:
            by_conviction[str(r["conviction"])] = by_conviction.get(str(r["conviction"]), 0) + 1
        outcome_pct = float(r["outcome_pct"]) if r["outcome_pct"] is not None else None
        if outcome_pct is not None:
            counted += 1
            # ADD wins are positive return; TRIM/SELL wins are NEGATIVE return.
            if kind.upper() in ("TRIM", "SELL"):
                if outcome_pct < 0:
                    wins += 1
            else:
                if outcome_pct > 0:
                    wins += 1
        decisions.append(
            DecisionRow(
                recommendation_kind=kind,
                recommendation_value=(
                    float(r["recommendation_value"])
                    if r["recommendation_value"] is not None
                    else None
                ),
                conviction=r["conviction"],
                made_at=made,
                outcome_pct=outcome_pct,
                outcome_at=_parse_dt(r["outcome_at"]),
                rationale_excerpt=r["rationale_excerpt"],
            )
        )
    win_rate = (wins / counted) if counted else None
    return DecisionHistorySummary(
        total=len(decisions),
        by_kind=by_kind,
        by_conviction=by_conviction,
        win_rate_overall=win_rate,
        rows=decisions,
    )


def _read_json(path: Path) -> object | None:
    import json as _json

    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError):
        return None


def _profile_fields(
    fmp_dir: Path, ticker: str
) -> tuple[str | None, str | None, str | None, float | None]:
    """(companyName, sector, industry, marketCap) from a cached FMP profile.

    Tolerates both the stable-API key (``marketCap``) and the legacy v3 one
    (``mktCap``).
    """
    raw = _read_json(fmp_dir / f"{ticker}_profile.json")
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        return None, None, None, None
    rec = cast("dict[str, object]", raw[0])
    name = str(rec["companyName"]) if rec.get("companyName") is not None else None
    sector = str(rec["sector"]) if rec.get("sector") else None
    industry = str(rec["industry"]) if rec.get("industry") else None
    market_cap: float | None = None
    for key in ("marketCap", "mktCap"):
        mc = rec.get(key)
        if isinstance(mc, (int, float)):
            market_cap = float(mc)
            break
    return name, sector, industry, market_cap


def _fmp_peer_pool(fmp_dir: Path, ticker: str) -> list[tuple[str, str | None, float | None]]:
    """The raw FMP peer candidates as (symbol, name, market_cap).

    Payload shapes shifted across FMP versions; tolerate the three known
    ones. The dict shape carries companyName + mktCap per peer — kept as
    fallbacks so candidates OUTSIDE the cached-profile universe (the common
    case for foreign rivals like ITUB) can still be name-matched and
    scale-scored.
    """
    peers_raw = _read_json(fmp_dir / f"{ticker}_peers.json")
    if not isinstance(peers_raw, list) or not peers_raw:
        return []
    pool = cast("list[object]", peers_raw)
    if isinstance(pool[0], dict):
        first = cast("dict[str, object]", pool[0])
        if "peersList" in first:
            raw = first.get("peersList")
            if isinstance(raw, list):
                return [(str(p).upper(), None, None) for p in cast("list[object]", raw)]
        if "symbol" in first:
            out: list[tuple[str, str | None, float | None]] = []
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                rec = cast("dict[str, object]", entry)
                if rec.get("symbol") is None:
                    continue
                name = str(rec["companyName"]) if rec.get("companyName") is not None else None
                cap_raw = rec.get("mktCap", rec.get("marketCap"))
                cap = float(cap_raw) if isinstance(cap_raw, (int, float)) else None
                out.append((str(rec["symbol"]).upper(), name, cap))
            return out
        return []
    return [(str(p).upper(), None, None) for p in pool]


def _watchlist_names(
    ticker: str, repo_root: Path, *, captured: dict[str, object] | None = None
) -> list[str]:
    """The owner's competitive_watchlist (prose rival names) for `ticker`,
    from the thesis JSON when one exists. Best-effort."""
    raw = (
        captured
        if captured is not None
        else _read_json(Path(repo_root) / "micro_thesis" / "holdings" / f"{ticker}.json")
    )
    if not isinstance(raw, dict):
        return []
    wl = cast("dict[str, object]", raw).get("competitive_watchlist")
    if not isinstance(wl, list):
        return []
    return [str(n) for n in cast("list[object]", wl) if isinstance(n, str) and n.strip()]


def _peer_curation(
    ticker: str, repo_root: Path, *, captured: dict[str, object] | None = None
) -> tuple[list[str], dict[str, object] | None]:
    """The owner's `curate_peers` artifacts from the thesis JSON (S5):

    - ``peer_exclude`` — rivals to drop from the shown set (ticker or name),
      the one thing the +3 ``competitive_watchlist`` pin list can't express.
    - ``peers_section_override`` — the re-evaluable hide-unless-quality
      condition modelling "remove this section UNLESS you show better peers".

    Best-effort: ``([], None)`` on any miss."""
    raw = (
        captured
        if captured is not None
        else _read_json(Path(repo_root) / "micro_thesis" / "holdings" / f"{ticker}.json")
    )
    if not isinstance(raw, dict):
        return [], None
    payload = cast("dict[str, object]", raw)
    excl_raw = payload.get("peer_exclude")
    exclude = (
        [str(n) for n in cast("list[object]", excl_raw) if isinstance(n, str) and n.strip()]
        if isinstance(excl_raw, list)
        else []
    )
    ov_raw = payload.get("peers_section_override")
    override = cast("dict[str, object]", ov_raw) if isinstance(ov_raw, dict) else None
    return exclude, override


# Weights for the LLM business-model-comparables generator (S-peer / fund-grade
# follow-on; directives/peer_selection_llm.md). THESIS_PEER_WEIGHT dominates the
# FMP screen's max (industry 2 + tracked 2 + scale 1 = 5) so an LLM-vouched name
# always outranks an FMP-only one; corroboration (the LLM name ALSO surfaced by
# the FMP screen) ranks it higher still. Owner curation rides on top unchanged:
# competitive_watchlist still pins (+3) and injects, peer_exclude still drops
# (it runs in the same scoring loop, so it drops LLM-injected names too), and
# peers_section_override still gates the whole panel.
THESIS_PEER_WEIGHT = 6.0
THESIS_CORROBORATION = 1.0


def _llm_peer_suggestions(ticker: str, repo_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The cached LLM business-model comparables for `ticker`, read straight
    from data/peer_selection/<T>.json (no heavy import on the render path).

    Returns ``(why_by_ticker, name_by_ticker)`` in LLM rank order is not
    preserved — the screen re-scores — but the `why` becomes the panel's Why
    column and the name fills in for foreign rivals outside the FMP-profile
    universe. ``({}, {})`` on any miss / malformed cache (degrade to the FMP
    screen). Generated on the --enable-llm build; absent here is the no-op that
    makes load_peer_comp behave exactly as it did pre-generator."""
    raw = _read_json(Path(repo_root) / "data" / "peer_selection" / f"{ticker.upper()}.json")
    if not isinstance(raw, dict):
        return {}, {}
    suggestions = cast("dict[str, object]", raw).get("suggestions")
    if not isinstance(suggestions, list):
        return {}, {}
    why_by: dict[str, str] = {}
    name_by: dict[str, str] = {}
    for entry in cast("list[object]", suggestions):
        if not isinstance(entry, dict):
            continue
        rec = cast("dict[str, object]", entry)
        tok = rec.get("ticker")
        if not isinstance(tok, str) or not _looks_like_ticker(tok.strip().upper()):
            continue
        sym = tok.strip().upper()
        why = rec.get("why")
        name = rec.get("name")
        if isinstance(why, str) and why.strip():
            why_by[sym] = why.strip()
        if isinstance(name, str) and name.strip():
            name_by[sym] = name.strip()
        else:
            why_by.setdefault(sym, "")
    return why_by, name_by


# A bare exchange-ticker-shaped token: leading letter, ≤7 chars total, no
# spaces. Distinguishes a pinned ticker ("HOOD") from a prose rival name
# ("Itau Unibanco") so only genuine tickers get injected into the FMP pool.
_TICKER_RX = re.compile(r"[A-Z][A-Z0-9.\-]{0,6}")


def _looks_like_ticker(token: str) -> bool:
    return bool(_TICKER_RX.fullmatch(token))


def _has_peer_metrics(row: PeerCompRow) -> bool:
    """True when a peer carries at least one computed TTM multiple — a real
    comp, not a wall-of-em-dashes row."""
    return any(
        v is not None
        for v in (row.market_cap_usd, row.revenue_ttm_usd, row.net_margin_ttm, row.roic_ttm)
    )


def evaluate_peers_override(
    override: dict[str, object] | None, rows: list[PeerCompRow]
) -> tuple[bool, dict[str, object]]:
    """Re-evaluate a persisted peers-section override against freshly scored
    rows (S5 — the owner's "remove this section UNLESS you show better peers /
    computed multiples", modelled as an actionable, re-checkable condition
    rather than verbatim-logged text).

    A peer counts as *quality* when it satisfies the override's requirements:
    ``require_named`` (the owner vouched for it via the watchlist) and/or
    ``require_metrics`` (it carries at least one computed TTM multiple). The
    section is hidden only while fewer than ``min_quality_peers`` qualify; the
    moment enough credible rivals are pinned the condition flips and the panel
    returns — no manual un-hide, the directive's "act on the condition".

    Returns ``(hide, detail)``. ``hide`` is always False for a missing /
    malformed override or any ``action`` other than ``"hide"``.
    """
    if not isinstance(override, dict) or override.get("action") != "hide":
        return False, {"evaluated": False}
    require_named = bool(override.get("require_named", True))
    require_metrics = bool(override.get("require_metrics", True))
    raw_min = override.get("min_quality_peers", 2)
    min_quality = max(1, int(raw_min) if isinstance(raw_min, (int, float)) else 2)

    quality = sum(
        1
        for r in rows
        if (not require_named or "named rival" in r.match_reasons)
        and (not require_metrics or _has_peer_metrics(r))
    )
    hide = quality < min_quality
    return hide, {
        "evaluated": True,
        "quality_peers": quality,
        "min_quality_peers": min_quality,
        "require_named": require_named,
        "require_metrics": require_metrics,
        "satisfied": not hide,
    }


def _normalize_name(name: str) -> str:
    """Accent-stripped, lowercased, alnum-only — for rival-name matching."""
    import unicodedata

    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in ascii_only.lower() if ch.isalnum() or ch == " ").strip()


def _is_named_rival(company_name: str | None, watchlist: list[str]) -> bool:
    if not company_name or not watchlist:
        return False
    norm_company = _normalize_name(company_name)
    for rival in watchlist:
        norm_rival = _normalize_name(rival)
        if norm_rival and (norm_rival in norm_company or norm_company in norm_rival):
            return True
    return False


def load_peer_comp(
    ticker: str,
    *,
    repo_root: Path,
    max_peers: int = 6,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[PeerCompRow]:
    """Read a frozen governed set and source-bound metrics; never resolve or add members."""
    from compute.comparable_set_reader import read_frozen_comparable_set
    from report.render_clock import render_today
    from report.sections.peer_projection import project_peer, read_peer_owner_context

    connection = conn if conn is not None else _open(db_path)
    if connection is None:
        raise ValueError("canonical_peer_database_unavailable")
    try:
        as_of = render_today()
        selection = read_frozen_comparable_set(connection, ticker, as_of=as_of)
        if selection is None:
            raise ValueError("canonical_peer_membership_unavailable")
        owner_context, owner_receipt = read_peer_owner_context(repo_root, ticker)
        exclusions, override = _peer_curation(ticker, repo_root, captured=owner_context)
        excluded = {item.strip().upper() for item in exclusions}
        excluded_names = {_normalize_name(item) for item in exclusions}
        watchlist = _watchlist_names(ticker, repo_root, captured=owner_context)
        named = {item.strip().upper() for item in watchlist}
        rows: list[PeerCompRow] = []
        if max_peers <= 0:
            return rows
        for peer, reason in selection.members:
            if peer == ticker.upper() or peer in excluded:
                continue
            row = project_peer(
                connection,
                repo_root / "data" / "historical" / "fmp",
                peer,
                reason,
                selection,
                as_of=as_of,
                named=peer in named or reason == "pinned_override",
            )
            if _is_named_rival(row.peer_name, watchlist) and "named rival" not in row.match_reasons:
                row = replace(row, match_reasons=(*row.match_reasons, "named rival"))
            if row.source_evidence is not None:
                row = replace(
                    row, source_evidence={**row.source_evidence, "owner_context": owner_receipt}
                )
            if row.peer_name and _normalize_name(row.peer_name) in excluded_names:
                continue
            rows.append(row)
            if len(rows) >= max(0, max_peers):
                break
        hide, _detail = evaluate_peers_override(override, rows)
        return [] if hide else rows
    finally:
        if conn is None:
            connection.close()


def load_peer_comp_legacy_shadow(
    ticker: str,
    *,
    repo_root: Path,
    max_peers: int = 6,
    db_path: Path | str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[PeerCompRow]:
    """Scored comparable selection for the eval snapshot (P4.2; tightened PR7).

    The raw FMP peer list is a sector/market-cap screen whose alphabetical
    head is frequently wrong (the owner flagged NU → Barclays, NOW → Applied
    Materials). The pool usually *contains* the right names, so instead of
    taking the first ``max_peers`` we score every candidate:

      +3  named in the owner's competitive_watchlist (thesis JSON)
      +2  same FMP industry as the target
      +2  a TRACKED company (portfolio/evaluation) — relevance signal, and
          its metrics resolve from our own cached data
      +1  same FMP sector
      +1  market cap within 0.2x – 5x of the target

    and keep the top ``max_peers`` that score >= 1. PR7 additionally drops
    candidates that pass the screen but carry NO resolvable metrics at all
    (cap, revenue, margin, ROIC all absent) — those rows rendered as a wall
    of em-dashes (the owner's "shit peers" UBER table: six semicap/SaaS names
    with every cell blank). A named rival survives even metric-less (it's
    informative by itself). Empty result → the renderer hides the panel.

    S5 (steerable peers) layers the owner's `curate_peers` directives on top of
    the screen:
      - a pinned bare TICKER absent from the FMP pool is INJECTED so an
        explicit pin always shows (the screen routinely omits a deliberately
        chosen rival); name pins keep working via the +3 watchlist match;
      - ``peer_exclude`` drops a rival from the shown set by ticker or name;
      - ``peers_section_override`` is re-evaluated here so the "remove unless
        better peers" condition hides — and later un-hides — the whole panel
        on its own as the pinned set crosses the quality bar.
    """
    fmp_dir = Path(repo_root) / "data" / "historical" / "fmp"
    ticker = ticker.upper()
    pool = _fmp_peer_pool(fmp_dir, ticker)

    # The raw FMP screen (before pin / LLM injection) — used for corroboration:
    # an LLM-vouched name that ALSO surfaced here ranks higher.
    fmp_screen_tickers = {p[0] for p in pool}
    # LLM business-model comparables (data/peer_selection/<T>.json, written on
    # the --enable-llm build). The generator that fixes "shit peers"; absent
    # here, scoring is exactly the pre-generator FMP screen.
    llm_why, llm_names = _llm_peer_suggestions(ticker, repo_root)
    llm_tickers = set(llm_why)

    _, target_sector, target_industry, target_cap = _profile_fields(fmp_dir, ticker)
    watchlist = _watchlist_names(ticker, repo_root)
    peer_exclude, override = _peer_curation(ticker, repo_root)
    tracked = _tracked_tickers(db_path, conn=conn)

    # Inject pinned tickers the FMP screen omitted (resolve cached metrics if we
    # have them). Only ticker-shaped, already-uppercase watchlist entries are
    # treated as injectable pins — a prose name ("Itau Unibanco") still scores
    # purely via the name match below, never as a fabricated pool ticker.
    pool_tickers = {p[0] for p in pool}
    pinned_tickers: set[str] = set()
    for entry in watchlist:
        tok = entry.strip()
        if tok and tok == tok.upper() and tok != ticker and _looks_like_ticker(tok):
            pinned_tickers.add(tok)
            if tok not in pool_tickers:
                pool.append((tok, None, None))
                pool_tickers.add(tok)

    # Seed the pool with LLM business-model comparables the FMP screen omitted
    # (same mechanism as pins). Their fetched profile/metrics resolve via the
    # cached FMP files written alongside the suggestion set; the self-ticker is
    # guarded and peer_exclude still drops them in the scoring loop below.
    for sym in llm_tickers:
        if sym != ticker and sym not in pool_tickers:
            pool.append((sym, llm_names.get(sym), None))
            pool_tickers.add(sym)

    if not pool:
        return []

    exclude_tickers = {e.strip().upper() for e in peer_exclude}
    exclude_names = {_normalize_name(e) for e in peer_exclude if e.strip()}

    scored: list[tuple[float, int, str, str | None, float | None, tuple[str, ...], bool]] = []
    for idx, (peer, pool_name, pool_cap) in enumerate(pool):
        if peer == ticker:
            continue
        prof_name, peer_sector, peer_industry, prof_cap = _profile_fields(fmp_dir, peer)
        # Foreign/untracked rivals usually have no cached profile — fall back
        # to the identity the peers payload itself carries.
        peer_name = prof_name or pool_name
        peer_cap = prof_cap if prof_cap is not None else pool_cap
        # Owner exclusion drops a rival from the shown set by ticker or name,
        # however well it would otherwise score.
        if peer in exclude_tickers or (peer_name and _normalize_name(peer_name) in exclude_names):
            continue
        score = 0.0
        reasons: list[str] = []
        named = _is_named_rival(peer_name, watchlist) or peer in pinned_tickers
        if named:
            score += 3
            reasons.append("named rival")
        if target_industry and peer_industry == target_industry:
            score += 2
            reasons.append("same industry")
        elif target_sector and peer_sector == target_sector:
            score += 1
            reasons.append("same sector")
        if peer in tracked:
            score += 2
            reasons.append("tracked")
        if target_cap and peer_cap and 0.2 <= peer_cap / target_cap <= 5.0:
            score += 1
            reasons.append("similar scale")
        # LLM business-model comparability dominates the FMP screen: an
        # LLM-vouched name leads, and one corroborated by the FMP screen leads
        # further. The one-line `why` becomes the panel's Why column.
        if peer in llm_tickers:
            score += THESIS_PEER_WEIGHT
            if peer in fmp_screen_tickers:
                score += THESIS_CORROBORATION
            reasons.append("thesis peer")
            why = llm_why.get(peer)
            if why:
                reasons.append(why)
        if score < 1:
            continue
        # Tiebreak: original FMP order (idx) keeps the sort deterministic.
        scored.append((score, idx, peer, peer_name, peer_cap, tuple(reasons), named))

    scored.sort(key=lambda t: (-t[0], t[1]))

    out: list[PeerCompRow] = []
    for _score, _idx, peer, peer_name, peer_cap, row_reasons, named in scored:
        if len(out) >= max_peers:
            break
        row = PeerCompRow(
            peer_ticker=peer,
            peer_name=peer_name,
            market_cap_usd=peer_cap,
            revenue_ttm_usd=_peer_revenue_ttm(fmp_dir, peer),
            net_margin_ttm=_peer_net_margin_ttm(fmp_dir, peer),
            roic_ttm=_peer_roic_ttm(fmp_dir, peer),
            match_reasons=row_reasons,
        )
        has_metrics = any(
            v is not None
            for v in (row.market_cap_usd, row.revenue_ttm_usd, row.net_margin_ttm, row.roic_ttm)
        )
        # An all-dash row says nothing — hide-don't-stub. This applies to LLM
        # thesis peers too (directive acceptance criterion: suggested peers
        # render WITH computed multiples, never as em-dash walls); a suggestion
        # whose fundamentals never resolve — foreign-only listing, delisted
        # symbol — yields its seat to the next-scored name with real numbers.
        # Only an owner-pinned named rival is informative enough metric-less.
        if not has_metrics and not named:
            continue
        out.append(row)

    # The owner's "remove this section unless better peers" condition is
    # re-evaluated every build: hide the whole panel while too few credible
    # comps qualify, return it the moment enough are pinned (S5).
    hide, _detail = evaluate_peers_override(override, out)
    if hide:
        return []
    return out


def _tracked_tickers(
    db_path: Path | str | None, *, conn: sqlite3.Connection | None = None
) -> set[str]:
    """Tracked peers from explicit database authority; no checkout fallback."""
    db_conn = conn if conn is not None else (_open(db_path) if db_path is not None else None)
    if db_conn is None:
        return set()
    try:
        rows = db_conn.execute(
            "SELECT ticker FROM tracked_companies "
            "WHERE archived_at IS NULL AND list_type IN ('portfolio', 'evaluation')"
        ).fetchall()
        return {str(r[0]).upper() for r in rows}
    except sqlite3.Error:
        if conn is not None:
            raise
        return set()
    finally:
        if conn is None:
            db_conn.close()


def _first_record(path: Path) -> dict[str, object] | None:
    """First record of a list-of-dicts FMP JSON; None on any other shape."""
    raw = _read_json(path)
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        return cast("dict[str, object]", raw[0])
    return None


def _peer_revenue_ttm(fmp_dir: Path, peer: str) -> float | None:
    """TTM revenue: sum of the 4 newest quarterly income-statement rows
    (stable shape); falls back to the legacy v3 ``revenueTTM`` key."""
    inc = _read_json(fmp_dir / f"{peer}_income_statement_quarterly.json")
    if isinstance(inc, list) and inc:
        vals: list[float] = []
        for entry in cast("list[object]", inc)[:4]:
            if not isinstance(entry, dict):
                continue
            rev = cast("dict[str, object]", entry).get("revenue")
            if isinstance(rev, (int, float)):
                vals.append(float(rev))
        if vals:
            return sum(vals)
    ttm = _first_record(fmp_dir / f"{peer}_key_metrics_ttm.json")
    if ttm is not None:
        v = ttm.get("revenueTTM")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _peer_net_margin_ttm(fmp_dir: Path, peer: str) -> float | None:
    """Net margin TTM: stable ``financial_ratios_ttm.netProfitMarginTTM``,
    legacy v3 ``key_metrics_ttm.netIncomePerRevenueTTM`` as fallback."""
    ratios = _first_record(fmp_dir / f"{peer}_financial_ratios_ttm.json")
    if ratios is not None:
        v = ratios.get("netProfitMarginTTM")
        if isinstance(v, (int, float)):
            return float(v)
    ttm = _first_record(fmp_dir / f"{peer}_key_metrics_ttm.json")
    if ttm is not None:
        v = ttm.get("netIncomePerRevenueTTM")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _peer_roic_ttm(fmp_dir: Path, peer: str) -> float | None:
    """ROIC TTM: stable ``returnOnInvestedCapitalTTM``, legacy ``roicTTM``."""
    ttm = _first_record(fmp_dir / f"{peer}_key_metrics_ttm.json")
    if ttm is not None:
        for key in ("returnOnInvestedCapitalTTM", "roicTTM"):
            v = ttm.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    return None


def load_saydo_verdicts(
    ticker: str,
    *,
    db_path: Path | str | None = None,
    limit: int = 30,
    conn: sqlite3.Connection | None = None,
) -> list[SayDoVerdictRow]:
    """All management_commitments rows for `ticker` with their outcomes.

    The renderer can group by period_made to produce the per-quarter
    say-vs-do grading overlay that P3-21 calls for.
    """
    db_conn = conn if conn is not None else _open(db_path)
    if db_conn is None:
        return []
    original_factory = db_conn.row_factory
    db_conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(db_conn, "management_commitments"):
            return []
        table_columns = {
            table: {str(row[1]) for row in db_conn.execute(f"PRAGMA table_info({table})")}
            for table in ("transcript_segments", "transcripts", "documents")
            if _table_exists(db_conn, table)
        }
        identity_schema_available = (
            {"id", "transcript_id"} <= table_columns.get("transcript_segments", set())
            and {"id", "ticker", "document_id", "source_url"}
            <= table_columns.get("transcripts", set())
            and {"id", "ticker", "source_type", "doc_type"} <= table_columns.get("documents", set())
        )
        transcript_join = (
            "LEFT JOIN transcript_segments ts "
            "ON ts.id = mc.transcript_segment_id "
            "LEFT JOIN transcripts t ON t.id = ts.transcript_id "
            "LEFT JOIN documents d ON d.id = t.document_id"
            if identity_schema_available
            else ""
        )
        transcript_projection = (
            "mc.ticker AS commitment_ticker, t.ticker AS transcript_ticker, "
            "t.document_id AS transcript_document_id, d.id AS existing_document_id, "
            "d.ticker AS document_ticker, d.source_type AS document_source_type, "
            "d.doc_type AS document_type, t.source_url AS source_url"
            if identity_schema_available
            else "NULL AS commitment_ticker, NULL AS transcript_ticker, "
            "NULL AS transcript_document_id, NULL AS existing_document_id, "
            "NULL AS document_ticker, NULL AS document_source_type, "
            "NULL AS document_type, NULL AS source_url"
        )
        rows = db_conn.execute(
            f"""
            SELECT mc.period_made, mc.period_target, mc.transcript_segment_id, mc.kpi_name,
                   mc.comparator, mc.target_value, mc.unit, mc.narrative,
                   mc.realized_value, mc.outcome, mc.evaluated_at, {transcript_projection}
            FROM management_commitments mc
            {transcript_join}
            WHERE UPPER(TRIM(mc.ticker)) = ?
            ORDER BY mc.period_made DESC
            LIMIT {int(limit)}
            """,
            (ticker.strip().upper(),),
        ).fetchall()
    finally:
        if conn is None:
            db_conn.close()
        else:
            db_conn.row_factory = original_factory
    out: list[SayDoVerdictRow] = []
    for r in rows:
        period_made = _parse_dt(r["period_made"])
        period_target = _parse_dt(r["period_target"])
        if period_made is None or period_target is None:
            continue
        transcript_ticker = _normalized_issuer_ticker(r["transcript_ticker"])
        commitment_ticker = _normalized_issuer_ticker(r["commitment_ticker"])
        document_ticker = _normalized_issuer_ticker(r["document_ticker"])
        document_type = _normalized_source_kind(r["document_type"])
        source_type = _normalized_source_kind(r["document_source_type"])
        document_kind_matches = (source_type, document_type) in {
            (SourceType.IR_DOC.value, DocType.IR_TRANSCRIPT.value),
            (SourceType.TRANSCRIPT_AUDIO.value, DocType.EARNINGS_CALL_TRANSCRIPT.value),
        }
        source_identity_matches = (
            r["transcript_document_id"] is not None
            and r["existing_document_id"] is not None
            and commitment_ticker is not None
            and commitment_ticker == transcript_ticker == document_ticker
            and document_kind_matches
        )
        source_document_id = int(r["existing_document_id"]) if source_identity_matches else None
        out.append(
            SayDoVerdictRow(
                period_made=period_made,
                period_target=period_target,
                kpi_name=str(r["kpi_name"]),
                comparator=str(r["comparator"]),
                target_value=float(r["target_value"]),
                unit=str(r["unit"]),
                narrative=str(r["narrative"]),
                realized_value=(
                    float(r["realized_value"]) if r["realized_value"] is not None else None
                ),
                outcome=r["outcome"],
                evaluated_at=_parse_dt(r["evaluated_at"]),
                source_ref=f"transcript_segment:{int(r['transcript_segment_id'])}",
                source_document_id=source_document_id,
                source_url=(
                    str(r["source_url"])
                    if source_document_id is not None and r["source_url"] is not None
                    else None
                ),
            )
        )
    return out


def _load_open_notes(ticker: str, conn: sqlite3.Connection) -> list[AnalystNoteRow]:
    rows = list_notes(ticker=ticker, status="open", limit=12, conn=conn)
    kind_rank = {"watch": 0, "question": 1}
    return sorted(rows, key=lambda note: kind_rank.get(note.kind, 9))


def load_graded_sell_base_rate(ticker: str, db_path: Path) -> str | None:
    """The graded-sells base-rate line for the Position-tab coaching block, or
    None when unavailable.

    ``advisor.position_review.graded_sell_record`` is being built on a
    parallel branch (not yet merged as of this PR) — a guarded import so this
    renderer never hard-depends on a module that may not exist yet. Any
    failure (missing module, missing DB, missing table) degrades to None and
    the caller skips the line silently (spec: "do not fail if it's missing")."""
    if not db_path.exists():
        return None
    try:
        from advisor.position_review import graded_sell_record
    except ImportError:
        return None
    try:
        # The record is deliberately portfolio-wide (it grades the OWNER's
        # sell/trim pattern, not this name) — ``ticker`` stays in this loader's
        # signature for the per-ticker P3 call shape but doesn't scope the SQL.
        del ticker
        result = cast("object", graded_sell_record(db_path))
    except sqlite3.Error:
        return None
    return cast("str | None", result)


# ---------------------------------------------------------------------------
# Standing rules (Monthly Red Team PR10 — surface wiring)
# ---------------------------------------------------------------------------
#
# The per-ticker brief's Position tab historically rendered zero
# ``position_sizing_intent`` content — the FLKR exit ladder, RBRK/BKNG target
# weights and the DRAFT add-rungs were visible to the Risk panel, /review and
# Ask but invisible in the brief. This loader reads the ticker's full intent
# history (the same table + row shape ``position_guard`` reads) and derives
# the coverage line via ``position_guard.evaluate_downside_trigger`` /
# ``evaluate_add_trigger`` — the naked-position gate's OWN detection logic,
# never re-derived.

# The DRAFT provenance prefixes the red-team write paths stamp on narratives:
# PR9's data pass writes "[draft, pending owner review] ..." and
# redteam.response._accept writes "[red-team accepted, pending owner edit] ..."
# — both are machine-proposed rows awaiting the owner, rendered as a quiet
# DRAFT chip (the bracketed prefix is stripped from the displayed narrative —
# the chip IS the prefix) rather than blended in with owner-authored rules.
_DRAFT_NARRATIVE_RE = re.compile(
    r"^\s*\[\s*(?:draft\b|red-team accepted\b)[^\]]*\]\s*", re.IGNORECASE
)


def _parse_intent_dt(raw: object) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    # Naive-UTC convention (reference_naive_utc): strip any aware offset so
    # pre-migration rows compare/render like the naive rows.
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def load_standing_rules(
    ticker: str, db_path: Path | None, repo_root: Path, *, conn: sqlite3.Connection | None = None
) -> StandingRulesPanel | None:
    """The ticker's ``position_sizing_intent`` history as a render-ready
    panel, or ``None`` when there are no rows on file (hide-don't-stub) or the
    DB/table is absent — the same best-effort degrade contract as every other
    loader in this module."""
    if conn is None and (db_path is None or not db_path.exists()):
        return None
    try:
        db_conn = (
            conn
            if conn is not None
            else connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        )
    except sqlite3.Error:
        return None
    original_factory = db_conn.row_factory
    db_conn.row_factory = sqlite3.Row
    try:
        raw_rows = db_conn.execute(
            "SELECT intent_kind, intent_value, narrative, updated_at "
            "FROM position_sizing_intent WHERE ticker = ? "
            "ORDER BY updated_at DESC, id DESC",
            (ticker.upper().strip(),),
        ).fetchall()
    except sqlite3.Error:
        if conn is not None:
            raise
        return None
    finally:
        if conn is None:
            db_conn.close()
        else:
            db_conn.row_factory = original_factory
    if not raw_rows:
        return None

    rows: list[StandingRuleRow] = []
    for r in raw_rows:
        narrative = str(r["narrative"]) if r["narrative"] is not None else ""
        is_draft = bool(_DRAFT_NARRATIVE_RE.match(narrative))
        if is_draft:
            # The DRAFT chip carries the provenance — strip the raw prefix
            # from the displayed narrative so it isn't said twice.
            narrative = _DRAFT_NARRATIVE_RE.sub("", narrative, count=1)
        updated_at = _parse_intent_dt(r["updated_at"]) or datetime(1970, 1, 1)
        rows.append(
            StandingRuleRow(
                intent_kind=str(r["intent_kind"]),
                intent_value=(float(r["intent_value"]) if r["intent_value"] is not None else None),
                narrative=narrative,
                is_draft=is_draft,
                updated_at=updated_at,
            )
        )

    # Coverage line — reuse position_guard's exact detection (never
    # re-derived). Guarded import so this renderer never hard-depends on the
    # guard module's transitive imports; a failure degrades the coverage line
    # to absent, never the whole panel.
    downside_passed: bool | None = None
    add_passed: bool | None = None
    add_is_draft = False
    try:
        from position_guard import IntentRow, evaluate_add_trigger, evaluate_downside_trigger

        intents = [
            IntentRow(intent_kind=row.intent_kind, narrative=row.narrative or None) for row in rows
        ]
        downside_passed = evaluate_downside_trigger(
            intents, repo_root=repo_root, ticker=ticker
        ).passed
        add_passed = evaluate_add_trigger(intents).passed
        if add_passed:
            # The add check passed — draft iff NO non-draft row alone passes
            # it, i.e. the pre-commitment currently rests on machine-proposed
            # rows awaiting owner review.
            non_draft = [
                IntentRow(intent_kind=row.intent_kind, narrative=row.narrative or None)
                for row in rows
                if not row.is_draft
            ]
            add_is_draft = not evaluate_add_trigger(non_draft).passed
    except ImportError:
        pass
    return StandingRulesPanel(
        rows=rows,
        downside_passed=downside_passed,
        add_passed=add_passed,
        add_is_draft=add_is_draft,
    )


def load_workspace_p3_panels(
    ticker: str,
    repo_root: Path,
    *,
    db_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> WorkspaceP3Panels:
    """Read the optional panels through one connection with explicit availability.

    A supplied connection remains caller-owned. Optional data failures do not
    erase successfully read panels or become evidence of an empty history.
    """
    if db_path is None and conn is None:
        return WorkspaceP3Panels.empty()
    try:
        db_conn = (
            conn
            if conn is not None
            else connect_sqlite(str(db_path), role=SQLiteConnectionRole.READ_ONLY)
        )
    except (sqlite3.Error, OSError):
        log.warning({"event": "workspace_database_unavailable"})
        return WorkspaceP3Panels.empty(PanelAvailability.UNAVAILABLE)
    original_factory = db_conn.row_factory
    db_conn.row_factory = sqlite3.Row
    try:
        try:
            tables = {
                str(row[0])
                for row in db_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
        except sqlite3.Error:
            log.warning({"event": "workspace_database_unavailable"})
            return WorkspaceP3Panels.empty(PanelAvailability.UNAVAILABLE)
        availability: dict[str, PanelAvailability] = {}

        def read(
            name: str,
            table: str,
            loader: Callable[[], _ReadResult],
            fallback: _ReadResult,
            has_data: Callable[[_ReadResult], bool] = bool,
        ) -> _ReadResult:
            if table not in tables:
                availability[name] = PanelAvailability.UNAVAILABLE
                log.warning({"event": "workspace_panel_unavailable", "panel": name})
                return fallback
            try:
                result = loader()
            except (sqlite3.Error, OSError, ValueError, KeyError):
                availability[name] = PanelAvailability.UNAVAILABLE
                log.warning({"event": "workspace_panel_unavailable", "panel": name})
                return fallback
            availability[name] = (
                PanelAvailability.PRESENT if has_data(result) else PanelAvailability.EMPTY
            )
            return result

        empty = WorkspaceP3Panels.empty()
        panels = WorkspaceP3Panels(
            macro_sensitivities=read(
                "macro_sensitivities",
                "macro_sensitivities",
                lambda: load_macro_sensitivities(ticker, conn=db_conn),
                [],
            ),
            strategic_targets=read(
                "strategic_targets",
                "strategic_targets",
                lambda: load_strategic_targets(ticker, conn=db_conn),
                [],
            ),
            customer_concentrations=read(
                "customer_concentrations",
                "customer_concentrations",
                lambda: load_customer_concentrations(ticker, conn=db_conn),
                [],
            ),
            lease_ladder=read(
                "lease_ladder",
                "lease_commitments",
                lambda: load_lease_ladder(ticker, conn=db_conn),
                [],
            ),
            decision_history=read(
                "decision_history",
                "decisions",
                lambda: load_decision_history(ticker, conn=db_conn),
                empty.decision_history,
                lambda history: history.total > 0,
            ),
            peer_comp=read(
                "peer_comp",
                "tracked_companies",
                lambda: load_peer_comp(ticker, repo_root=repo_root, conn=db_conn),
                [],
            ),
            open_notes=read(
                "open_notes", "analyst_notes", lambda: _load_open_notes(ticker, db_conn), []
            ),
            position_review_count=read(
                "position_review_count",
                "advisor_memos",
                lambda: count_memos(kind="position_review", ticker=ticker, conn=db_conn),
                None,
            ),
            standing_rules=read(
                "standing_rules",
                "position_sizing_intent",
                lambda: load_standing_rules(ticker, db_path, repo_root, conn=db_conn),
                None,
            ),
            comp_set_context=read(
                "comp_set_context",
                "comparable_sets",
                lambda: load_comp_set_context(ticker, repo_root=repo_root, conn=db_conn),
                None,
            ),
        )
        return replace(panels, availability=availability)
    finally:
        if conn is None:
            db_conn.close()
        else:
            db_conn.row_factory = original_factory
