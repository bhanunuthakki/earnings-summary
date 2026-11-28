"""Read-only 8-quarter company coverage matrix across filings, IR docs, transcripts, commitments, and readouts."""

from __future__ import annotations

import calendar
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from provenance.selection import selected_transcripts_relation

TranscriptStatus = Literal["qa", "present", "missing"]
CommitmentStatus = Literal["extracted", "scanned_zero", "pending", "missing"]
ReadoutStatus = Literal["ok", "dirty", "missing"]
CoverageListFilter = Literal["portfolio", "evaluation", "watchlist", "all"]

_FILING_DOC_TYPES = ("sec_10q", "sec_10k", "sec_6k", "sec_20f")
_PRESS_RELEASE_DOC_TYPES = ("ir_press_release", "ir_press_release_synthesized")
_DECK_DOC_TYPES = (
    "ir_presentation",
    "ir_presentation_synthesized",
    "ir_supplement",
    "ir_investor_update",
)
_ALL_MATRIX_DOC_TYPES = (
    *_FILING_DOC_TYPES,
    *_PRESS_RELEASE_DOC_TYPES,
    *_DECK_DOC_TYPES,
)


class QuarterCoverageCell(BaseModel):
    """Coverage state for one company and fiscal quarter."""

    model_config = ConfigDict(extra="forbid")

    period_end: str
    fiscal_quarter: str
    filing: bool
    press_release: bool
    deck: bool
    transcript: TranscriptStatus
    transcript_id: int | None = None
    transcript_source: str | None = None
    commitments: CommitmentStatus
    commitments_count: int = 0
    readout: ReadoutStatus


class CompanyCoverageRow(BaseModel):
    """Multi-quarter coverage summary for one tracked equity or ADR."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    name: str
    list_type: str
    fiscal_year_end: str
    latest_brief_at: str | None = None
    cells: tuple[QuarterCoverageCell, ...]


class PortfolioCoverageSummary(BaseModel):
    """Aggregate gap counts across the returned coverage matrix."""

    model_config = ConfigDict(extra="forbid")

    companies: int
    total_cells: int
    missing_filings: int
    missing_press_releases: int
    missing_decks: int
    missing_transcripts: int
    pending_commitments: int
    missing_readouts: int


class PortfolioCoverageMatrix(BaseModel):
    """Complete coverage matrix payload returned by the CLI and API."""

    model_config = ConfigDict(extra="forbid")

    generated_at: str
    quarters: int
    list_filter: str
    summary: PortfolioCoverageSummary
    rows: tuple[CompanyCoverageRow, ...]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()  # nosec B608 -- internal table identifier
    return any(str(row[1]) == column_name for row in rows)


def _parse_fye(fye_raw: object) -> tuple[int, int]:
    text = str(fye_raw or "12-31").strip()
    parts = text.split("-")
    if len(parts) == 2:
        try:
            month, day = int(parts[0]), int(parts[1])
            if 1 <= month <= 12 and 1 <= day <= 31:
                return month, day
        except ValueError:
            pass
    return 12, 31


def _quarter_end_for_month(year: int, month: int, fye_day: int) -> date:
    day = min(fye_day, calendar.monthrange(year, month)[1])
    if fye_day >= 28:
        day = calendar.monthrange(year, month)[1]
    return date(year, month, day)


def _shift_quarter_end(anchor: date, quarters_back: int, fye_day: int) -> date:
    total_months = anchor.year * 12 + (anchor.month - 1) - (quarters_back * 3)
    year, month0 = divmod(total_months, 12)
    return _quarter_end_for_month(year, month0 + 1, fye_day)


def _quarter_label(period_end: date, fye_month: int) -> str:
    months_after_fye = (period_end.month - fye_month - 1) % 12
    quarter_num = (months_after_fye // 3) + 1
    fiscal_year = period_end.year + (1 if period_end.month > fye_month else 0)
    return f"Q{quarter_num} FY{fiscal_year % 100:02d}"


def _build_quarter_schedule(
    observed_dates: Sequence[date], *, fye_raw: object, quarters: int
) -> tuple[date, ...]:
    if not observed_dates:
        return ()
    fye_month, fye_day = _parse_fye(fye_raw)
    aligned = [d for d in observed_dates if (d.month - fye_month) % 3 == 0]
    anchor_candidates = aligned or list(observed_dates)
    latest = max(anchor_candidates)
    if (latest.month - fye_month) % 3 == 0:
        anchor = _quarter_end_for_month(latest.year, latest.month, fye_day)
        return tuple(_shift_quarter_end(anchor, i, fye_day) for i in range(quarters))
    unique_desc = sorted(set(observed_dates), reverse=True)
    return tuple(unique_desc[:quarters])


def _record_anchor_date(
    observed_dates_by_ticker: dict[str, set[date]],
    fallback_dates_by_ticker: dict[str, set[date]],
    ticker: str,
    period_end_str: str,
    as_of_date: date,
) -> None:
    parsed = date.fromisoformat(period_end_str)
    fallback_dates_by_ticker[ticker].add(parsed)
    if parsed <= as_of_date:
        observed_dates_by_ticker[ticker].add(parsed)


def _load_companies(
    conn: sqlite3.Connection, *, list_type: str, tickers: Sequence[str] | None
) -> list[tuple[str, str, str, str]]:
    where = ["archived_at IS NULL", "instrument_type IN ('equity', 'adr')"]
    params: list[str] = []
    if list_type != "all":
        where.append("list_type = ?")
        params.append(list_type)
    if tickers:
        norm = [t.strip().upper() for t in tickers if t.strip()]
        if norm:
            placeholders = ",".join("?" for _ in norm)
            where.append(f"UPPER(ticker) IN ({placeholders})")
            params.extend(norm)
    rows = conn.execute(
        f"SELECT UPPER(ticker), COALESCE(name, ticker), COALESCE(list_type, ''), "  # nosec B608 -- fixed column selection with parameterized filters
        "COALESCE(fiscal_year_end, '12-31') "
        f"FROM tracked_companies WHERE {' AND '.join(where)} ORDER BY UPPER(ticker)",
        params,
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


def build_portfolio_coverage_matrix(
    conn: sqlite3.Connection,
    *,
    quarters: int = 8,
    list_type: CoverageListFilter = "portfolio",
    tickers: Sequence[str] | None = None,
) -> PortfolioCoverageMatrix:
    """Build an N-quarter document and extraction coverage matrix from the live DB."""
    if quarters < 1 or quarters > 20:
        raise ValueError("quarters must be between 1 and 20")

    companies = _load_companies(conn, list_type=list_type, tickers=tickers)
    ticker_set = tuple(t for t, _, _, _ in companies)
    if not ticker_set:
        return PortfolioCoverageMatrix(
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            quarters=quarters,
            list_filter=list_type,
            summary=PortfolioCoverageSummary(
                companies=0,
                total_cells=0,
                missing_filings=0,
                missing_press_releases=0,
                missing_decks=0,
                missing_transcripts=0,
                pending_commitments=0,
                missing_readouts=0,
            ),
            rows=(),
        )

    today_utc = datetime.now(UTC).date()
    placeholders = ",".join("?" for _ in ticker_set)
    doc_type_placeholders = ",".join("?" for _ in _ALL_MATRIX_DOC_TYPES)
    doc_types_by_key: dict[tuple[str, str], set[str]] = {}
    observed_dates_by_ticker: dict[str, set[date]] = {t: set() for t in ticker_set}
    fallback_dates_by_ticker: dict[str, set[date]] = {t: set() for t in ticker_set}

    if _table_exists(conn, "documents"):
        for r in conn.execute(
            f"SELECT UPPER(ticker), date(period_end), doc_type "  # nosec B608 -- bounded IN placeholder count from tracked_companies and static doc-type list
            f"FROM documents WHERE UPPER(ticker) IN ({placeholders}) "
            f"AND doc_type IN ({doc_type_placeholders}) "
            "AND period_end IS NOT NULL AND date(period_end) IS NOT NULL",
            (*ticker_set, *_ALL_MATRIX_DOC_TYPES),
        ).fetchall():
            t, pe_s, dt = str(r[0]), str(r[1]), str(r[2] or "")
            doc_types_by_key.setdefault((t, pe_s), set()).add(dt)
            _record_anchor_date(
                observed_dates_by_ticker, fallback_dates_by_ticker, t, pe_s, today_utc
            )

    obs_periods: set[tuple[str, str]] = set()
    if _table_exists(conn, "reported_observations"):
        for r in conn.execute(
            f"SELECT DISTINCT UPPER(ticker), date(period_end) "  # nosec B608 -- bounded IN placeholder count from tracked_companies
            f"FROM reported_observations WHERE UPPER(ticker) IN ({placeholders}) "
            "AND observation_status = 'reported' AND date(period_end) IS NOT NULL",
            ticker_set,
        ).fetchall():
            t, pe_s = str(r[0]), str(r[1])
            obs_periods.add((t, pe_s))

    tx_by_key: dict[tuple[str, str], tuple[int, str | None, bool]] = {}
    if _table_exists(conn, "transcripts"):
        tx_relation = selected_transcripts_relation(conn).sql
        has_is_current = _table_has_column(conn, "transcripts", "is_current")
        has_source = _table_has_column(conn, "transcripts", "source")
        current_clause = "AND is_current = 1 " if has_is_current else ""
        source_expr = "source" if has_source else "NULL"
        for r in conn.execute(
            f"SELECT id, UPPER(ticker), date(period_end), {source_expr}, COALESCE(has_qa_section, 0) "  # nosec B608 -- selection relation from repository owner
            f"FROM {tx_relation} WHERE UPPER(ticker) IN ({placeholders}) "
            f"{current_clause}AND date(period_end) IS NOT NULL "
            "ORDER BY date(period_end) DESC, id DESC",
            ticker_set,
        ).fetchall():
            t, pe_s = str(r[1]), str(r[2])
            key = (t, pe_s)
            if key not in tx_by_key:
                tx_by_key[key] = (
                    int(r[0]),
                    str(r[3]) if r[3] is not None else None,
                    bool(r[4]),
                )
            _record_anchor_date(
                observed_dates_by_ticker, fallback_dates_by_ticker, t, pe_s, today_utc
            )

    tx_ids = sorted({val[0] for val in tx_by_key.values()})
    commitments_by_tx: dict[int, int] = {}
    receipts_by_tx: dict[int, int] = {}
    if (
        tx_ids
        and _table_exists(conn, "management_commitments")
        and _table_exists(conn, "transcript_segments")
    ):
        tx_ph = ",".join("?" for _ in tx_ids)
        for r in conn.execute(
            f"SELECT ts.transcript_id, COUNT(*) "  # nosec B608 -- bounded integer id placeholder list
            "FROM management_commitments mc "
            "JOIN transcript_segments ts ON ts.id = mc.transcript_segment_id "
            f"WHERE ts.transcript_id IN ({tx_ph}) GROUP BY ts.transcript_id",
            tx_ids,
        ).fetchall():
            commitments_by_tx[int(r[0])] = int(r[1])
    if tx_ids and _table_exists(conn, "commitment_scan_log"):
        tx_ph = ",".join("?" for _ in tx_ids)
        for r in conn.execute(
            f"SELECT transcript_id, COUNT(*) FROM commitment_scan_log "  # nosec B608 -- bounded integer id placeholder list
            f"WHERE transcript_id IN ({tx_ph}) GROUP BY transcript_id",
            tx_ids,
        ).fetchall():
            receipts_by_tx[int(r[0])] = receipts_by_tx.get(int(r[0]), 0) + int(r[1])
    if tx_ids and _table_exists(conn, "commitment_scan_receipts"):
        tx_ph = ",".join("?" for _ in tx_ids)
        for r in conn.execute(
            f"SELECT transcript_id, COUNT(*) FROM commitment_scan_receipts "  # nosec B608 -- bounded integer id placeholder list
            f"WHERE transcript_id IN ({tx_ph}) GROUP BY transcript_id",
            tx_ids,
        ).fetchall():
            receipts_by_tx[int(r[0])] = receipts_by_tx.get(int(r[0]), 0) + int(r[1])

    readout_by_key: dict[tuple[str, str], ReadoutStatus] = {}
    if _table_exists(conn, "llm_artifacts"):
        for r in conn.execute(
            f"SELECT UPPER(ticker), date(fiscal_period), COALESCE(dirty, 0) "  # nosec B608 -- bounded IN placeholder count from tracked_companies
            f"FROM llm_artifacts WHERE UPPER(ticker) IN ({placeholders}) "
            "AND purpose = 'post_earnings_readout' AND superseded_by_id IS NULL "
            "AND fiscal_period IS NOT NULL AND date(fiscal_period) IS NOT NULL "
            "ORDER BY id DESC",
            ticker_set,
        ).fetchall():
            key = (str(r[0]), str(r[1]))
            if key not in readout_by_key:
                readout_by_key[key] = "dirty" if bool(r[2]) else "ok"
            _record_anchor_date(
                observed_dates_by_ticker,
                fallback_dates_by_ticker,
                key[0],
                key[1],
                today_utc,
            )

    latest_brief_by_ticker: dict[str, str] = {}
    if _table_exists(conn, "brief_provenance_log"):
        for r in conn.execute(
            f"SELECT UPPER(ticker), MAX(generated_at) FROM brief_provenance_log "  # nosec B608 -- bounded IN placeholder count from tracked_companies
            f"WHERE UPPER(ticker) IN ({placeholders}) GROUP BY UPPER(ticker)",
            ticker_set,
        ).fetchall():
            if r[1] is not None:
                latest_brief_by_ticker[str(r[0])] = str(r[1])

    company_rows: list[CompanyCoverageRow] = []
    missing_filings = 0
    missing_press_releases = 0
    missing_decks = 0
    missing_transcripts = 0
    pending_commitments = 0
    missing_readouts = 0
    total_cells = 0

    for ticker, name, role, fye in companies:
        fye_month, _ = _parse_fye(fye)
        schedule = _build_quarter_schedule(
            sorted(
                observed_dates_by_ticker.get(ticker) or fallback_dates_by_ticker.get(ticker, ())
            ),
            fye_raw=fye,
            quarters=quarters,
        )
        cells: list[QuarterCoverageCell] = []
        for q_end in schedule:
            pe_s = q_end.isoformat()
            key = (ticker, pe_s)
            dtypes = doc_types_by_key.get(key, set())
            has_filing = bool(dtypes.intersection(_FILING_DOC_TYPES)) or (key in obs_periods)
            has_pr = bool(dtypes.intersection(_PRESS_RELEASE_DOC_TYPES))
            has_deck = bool(dtypes.intersection(_DECK_DOC_TYPES))

            tx_entry = tx_by_key.get(key)
            tx_id: int | None = None
            tx_src: str | None = None
            if tx_entry is None:
                tx_status: TranscriptStatus = "missing"
                cmt_status: CommitmentStatus = "missing"
                cmt_count = 0
            else:
                tx_id, tx_src, has_qa = tx_entry
                tx_status = "qa" if has_qa else "present"
                cmt_count = commitments_by_tx.get(tx_id, 0)
                if cmt_count > 0:
                    cmt_status = "extracted"
                elif receipts_by_tx.get(tx_id, 0) > 0:
                    cmt_status = "scanned_zero"
                else:
                    cmt_status = "pending"

            ro_status = readout_by_key.get(key, "missing")

            total_cells += 1
            if not has_filing:
                missing_filings += 1
            if not has_pr:
                missing_press_releases += 1
            if not has_deck:
                missing_decks += 1
            if tx_status == "missing":
                missing_transcripts += 1
            if cmt_status == "pending":
                pending_commitments += 1
            if ro_status == "missing":
                missing_readouts += 1

            cells.append(
                QuarterCoverageCell(
                    period_end=pe_s,
                    fiscal_quarter=_quarter_label(q_end, fye_month),
                    filing=has_filing,
                    press_release=has_pr,
                    deck=has_deck,
                    transcript=tx_status,
                    transcript_id=tx_id,
                    transcript_source=tx_src,
                    commitments=cmt_status,
                    commitments_count=cmt_count,
                    readout=ro_status,
                )
            )
        company_rows.append(
            CompanyCoverageRow(
                ticker=ticker,
                name=name,
                list_type=role,
                fiscal_year_end=fye,
                latest_brief_at=latest_brief_by_ticker.get(ticker),
                cells=tuple(cells),
            )
        )

    return PortfolioCoverageMatrix(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        quarters=quarters,
        list_filter=list_type,
        summary=PortfolioCoverageSummary(
            companies=len(company_rows),
            total_cells=total_cells,
            missing_filings=missing_filings,
            missing_press_releases=missing_press_releases,
            missing_decks=missing_decks,
            missing_transcripts=missing_transcripts,
            pending_commitments=pending_commitments,
            missing_readouts=missing_readouts,
        ),
        rows=tuple(company_rows),
    )


def _tx_token(cell: QuarterCoverageCell) -> str:
    if cell.transcript == "qa":
        return "QA"
    if cell.transcript == "present":
        return "TXT"
    return "-"


def _cmt_token(cell: QuarterCoverageCell) -> str:
    if cell.commitments == "extracted":
        return str(cell.commitments_count)
    if cell.commitments == "scanned_zero":
        return "0*"
    if cell.commitments == "pending":
        return "pend"
    return "-"


def _ro_token(cell: QuarterCoverageCell) -> str:
    if cell.readout == "ok":
        return "ok"
    if cell.readout == "dirty":
        return "dirty"
    return "-"


def format_matrix_table(matrix: PortfolioCoverageMatrix) -> str:
    """Render an aligned plain-text coverage matrix across companies and quarters."""
    s = matrix.summary
    lines = [
        (
            f"Coverage matrix ({matrix.list_filter}, {s.companies} companies, "
            f"{s.total_cells} quarter-cells): "
            f"missing[filing={s.missing_filings}, pr={s.missing_press_releases}, "
            f"deck={s.missing_decks}, tx={s.missing_transcripts}, readout={s.missing_readouts}] "
            f"pending_commitments={s.pending_commitments}"
        ),
        f"{'Ticker':<6} {'Quarter':<8} {'PeriodEnd':<10} {'Fil':<3} {'PR':<3} {'Dk':<3} {'Tx':<4} {'Cmt':<5} {'RO':<5} BriefAt",
    ]
    for row in matrix.rows:
        brief = (row.latest_brief_at or "-")[:19]
        if not row.cells:
            lines.append(
                f"{row.ticker:<6} {'-':<8} {'-':<10} {'-':<3} {'-':<3} {'-':<3} {'-':<4} {'-':<5} {'-':<5} {brief}"
            )
            continue
        for idx, cell in enumerate(row.cells):
            lines.append(
                f"{row.ticker:<6} {cell.fiscal_quarter:<8} {cell.period_end:<10} "
                f"{'Y' if cell.filing else '-':<3} {'Y' if cell.press_release else '-':<3} "
                f"{'Y' if cell.deck else '-':<3} {_tx_token(cell):<4} "
                f"{_cmt_token(cell):<5} {_ro_token(cell):<5} {brief if idx == 0 else ''}"
            )
    return "\n".join(lines)


def format_matrix_markdown(matrix: PortfolioCoverageMatrix) -> str:
    """Render a compact Markdown summary table (one row per company across its N quarters)."""
    lines = [
        "| Ticker | FYE | Quarters Covered | Filing | PR | Deck | Tx | Cmt | Readout | Latest Brief |",
        "| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for row in matrix.rows:
        n = len(row.cells)
        fil_n = sum(1 for c in row.cells if c.filing)
        pr_n = sum(1 for c in row.cells if c.press_release)
        dk_n = sum(1 for c in row.cells if c.deck)
        tx_n = sum(1 for c in row.cells if c.transcript != "missing")
        cmt_done = sum(1 for c in row.cells if c.commitments in {"extracted", "scanned_zero"})
        ro_n = sum(1 for c in row.cells if c.readout == "ok")
        span = f"{row.cells[-1].period_end}..{row.cells[0].period_end}" if n else "-"
        brief = (row.latest_brief_at or "-")[:19]
        lines.append(
            f"| {row.ticker} | {row.fiscal_year_end} | {span} ({n}q) | "
            f"{fil_n}/{n} | {pr_n}/{n} | {dk_n}/{n} | {tx_n}/{n} | {cmt_done}/{n} | {ro_n}/{n} | {brief} |"
        )
    return "\n".join(lines)
