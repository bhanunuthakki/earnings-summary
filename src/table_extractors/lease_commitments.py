"""Deterministic extractor for Future Minimum Lease Payments ladder.

Reads the FMP form_10k JSON section whose title matches the pattern
'Leases - Future Minimum Lease Payments (Details)' and emits one
`lease_commitments` row per (lease_type, ladder_year) tuple, scoped to
the CURRENT period column of the disclosure (the one matching the
filing's fiscal year-end).

Designed to be a no-LLM-call hot path: GOOG FY2024 parses in <50ms.

For non-US-GAAP filers whose lease ladder lives in a differently-named
section (NVO under IFRS uses 'Operating leases'; some banks split into
multiple sub-sections), the section discovery is keyword-based: any
top-level key containing both 'Lease' and one of 'Future', 'Minimum',
'Maturities' qualifies. If no qualifying section is present, returns
status='no_data' without error — analyst follow-up via LLM fallback is
a Phase 3 enhancement.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from table_extractors.base import (
    ExtractionOutcome,
    XbrlRow,
    iter_xbrl_table,
    parse_units,
)

log = logging.getLogger(__name__)

EXTRACTOR_ID = "lease_commitments_v1"
EXTRACTOR_VERSION = "1"
TABLE_KIND = "lease_commitments_ladder"

# Section discovery: a key with both 'Lease' AND one of these tokens is
# the ladder. Title-case-insensitive.
_LADDER_TOKENS = ("future", "minimum", "maturit")

# Map raw row labels (case-insensitive substring match) to canonical
# ladder_year enum values.
_LADDER_LABEL_RULES: list[tuple[re.Pattern[str], str]] = [
    # 'Fiscal 2025', '2025', 'Year 2025' all qualify; the captured year
    # is what _classify_label converts to a Y1..Y5/Thereafter offset.
    (re.compile(r"^\s*(?:fiscal(?:\s+year)?\s+|year\s+)?(20\d\d|19\d\d)\s*$", re.I), "YEAR"),
    (re.compile(r"thereafter", re.I), "Thereafter"),
    (
        re.compile(
            r"total\s+(?:future\s+|operating\s+|finance\s+)?lease\s+payments|total\s+undiscounted",
            re.I,
        ),
        "TotalPayments",
    ),
    (re.compile(r"less\s+imputed\s+interest|imputed\s+interest", re.I), "ImputedInterest"),
    (
        re.compile(
            r"total\s+(?:operating\s+|finance\s+)?lease\s+liabilit|net\s+lease\s+liabilit|lease\s+liability\s+balance",
            re.I,
        ),
        "LeaseLiability",
    ),
]


def extract(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None,
    sec_text: object | None,  # unused for this extractor
    db_path: Path,
    repo_root: Path,
    filing_doc_id: int | None = None,
    as_of_date: date | None = None,
) -> ExtractionOutcome:
    """Parse the lease ladder section from the FMP payload and persist.

    Returns ExtractionOutcome. status='no_data' if no qualifying section
    is present (e.g. NU, NVO with different naming); status='ok' when at
    least one row was inserted; status='parse_failed' on shape surprises.
    """
    del sec_text, repo_root  # unused

    if fmp_payload is None or fiscal_year is None:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="no_data",
            notes="missing fmp_payload or fiscal_year",
        )

    section_key, section = _find_ladder_section(fmp_payload)
    if section_key is None or section is None:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="no_data",
            notes="no Future-Minimum-Lease section in payload",
        )

    # Inner section header carries currency + scale; the outer payload key
    # is truncated by FMP to ~30 chars and loses the 'USD ($) $ in Millions'
    # suffix. Fall back to the outer key if the inner header is missing.
    inner_title = _inner_section_title(section) or section_key
    units = parse_units(inner_title)

    # Find the column index that matches `fiscal_year`. Period labels look
    # like 'Dec. 31, 2024' or 'Jan. 31, 2025'. Match by trailing year.
    rows = list(iter_xbrl_table(section))
    if not rows:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="no_data",
            notes="section had no concrete rows",
        )
    period_labels = rows[0].period_labels
    col_idx = _select_period_column(period_labels, fiscal_year)
    if col_idx is None:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="no_data",
            notes=f"no period column matched fy={fiscal_year}; periods={period_labels}",
        )

    as_of = as_of_date or _infer_as_of(period_labels[col_idx], fiscal_year)

    # Walk rows. Axis 'Operating Leases' or 'Finance Leases' scopes the
    # subsequent rows. Yield typed rows.
    # When the section is operating-only or finance-only (no axis marker —
    # see VEEV's "Schedule of Maturities of Lease Liabilities" pattern),
    # fall back to inferring lease_type from the inner title or labels.
    fallback_lease_type = _infer_fallback_lease_type(inner_title, rows)
    persistable: list[dict[str, object]] = []
    for row in rows:
        lease_type = _classify_lease_type(row.axis_path) or fallback_lease_type
        if lease_type is None:
            continue
        ladder_year, calendar_year = _classify_label(row.label, fiscal_year=fiscal_year)
        if ladder_year is None:
            continue
        if col_idx >= len(row.values):
            continue
        value = row.values[col_idx]
        if value is None:
            continue
        persistable.append(
            {
                "ticker": ticker,
                "fiscal_year": fiscal_year,
                "as_of_date": as_of.isoformat(),
                "filing_doc_id": filing_doc_id,
                "lease_type": lease_type,
                "ladder_year": ladder_year,
                "ladder_calendar_year": calendar_year,
                "amount": float(value),
                "currency": units.currency,
                "unit": units.scale,
                "source_section_key": section_key,
            }
        )

    inserted, logged = _persist(persistable, db_path=db_path)
    return ExtractionOutcome(
        table_kind=TABLE_KIND,
        ticker=ticker,
        fiscal_year=fiscal_year,
        n_rows_proposed=len(persistable),
        n_rows_inserted=inserted,
        n_extractions_logged=logged,
        status="ok" if inserted > 0 else "no_data",
        notes=f"section={section_key!r}",
    )


def _inner_section_title(section: list[dict[str, object]]) -> str | None:
    """The first dict's first key is the inner section title FMP emits.
    Returns None when the section is empty or shaped unexpectedly."""
    if not section or not isinstance(section[0], dict):
        return None
    keys = list(section[0].keys())
    return keys[0] if keys else None


def _find_ladder_section(
    payload: dict[str, object],
) -> tuple[str | None, list[dict[str, object]] | None]:
    """Find the lease-ladder section by key keywords. Returns
    (section_key, section_body) or (None, None)."""
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        lk = key.lower()
        if "lease" not in lk:
            continue
        if not any(tok in lk for tok in _LADDER_TOKENS):
            continue
        if not isinstance(value, list):
            continue
        # Looks plausibly like a section body — return it.
        body = cast("list[dict[str, object]]", value)
        return key, body
    return None, None


def _select_period_column(period_labels: list[str], fiscal_year: int) -> int | None:
    """Return the index of the period column matching the fiscal year, or None.

    Period labels look like 'Dec. 31, 2024'. We match by the trailing
    4-digit year. When there are multiple matches (rare — typically only
    one column per year), prefer the earliest.
    """
    target = str(fiscal_year)
    for i, lbl in enumerate(period_labels):
        if target in lbl:
            return i
    return None


def _classify_lease_type(axis_path: list[str]) -> str | None:
    """Map axis_path → 'operating' | 'finance' | None."""
    for axis in axis_path:
        a = axis.lower()
        if "operating" in a and "lease" in a:
            return "operating"
        if ("finance" in a or "capital" in a) and "lease" in a:
            return "finance"
    return None


def _infer_fallback_lease_type(inner_title: str, rows: list[XbrlRow]) -> str | None:
    """When the section has no Operating/Finance axis marker (VEEV pattern),
    infer lease_type from the section title or the row labels.

    Returns 'operating' or 'finance' when an unambiguous signal exists;
    None when the section appears mixed (which we'd rather skip than mis-bucket).
    """
    title_lower = inner_title.lower()
    has_operating_title = "operating lease" in title_lower
    has_finance_title = "finance lease" in title_lower or "capital lease" in title_lower
    if has_operating_title and not has_finance_title:
        return "operating"
    if has_finance_title and not has_operating_title:
        return "finance"

    # Fall back to label scan: a section is "operating-only" when every
    # lease-mentioning label says operating (or none mention either).
    saw_op = False
    saw_fin = False
    for row in rows:
        ll = row.label.lower()
        if "operating lease" in ll:
            saw_op = True
        if "finance lease" in ll or "capital lease" in ll:
            saw_fin = True
    if saw_op and not saw_fin:
        return "operating"
    if saw_fin and not saw_op:
        return "finance"
    return None


def _classify_label(label: str, *, fiscal_year: int) -> tuple[str | None, int | None]:
    """Map a row label to (ladder_year, calendar_year).

    Returns (None, None) if the label doesn't match any known ladder row.
    For pure-year labels ('2025'), the ladder_year is 'Y1'..'Y5' based
    on offset from `fiscal_year`; ladder rows beyond Y5 are treated as
    'Thereafter' (defensive — shouldn't happen since the section
    explicitly emits a 'Thereafter' row).
    """
    s = label.strip()
    for rx, target in _LADDER_LABEL_RULES:
        m = rx.search(s)
        if m is None:
            continue
        if target == "YEAR":
            try:
                year = int(m.group(1))
            except (ValueError, IndexError):
                return None, None
            offset = year - fiscal_year
            if 1 <= offset <= 5:
                return f"Y{offset}", year
            # Year row outside the Y1..Y5 window — treat as Thereafter
            return "Thereafter", year
        return target, None
    return None, None


def _infer_as_of(period_label: str, fiscal_year: int) -> date:
    """Parse a 'Dec. 31, 2024'-style label into a date. Falls back to
    fiscal_year-12-31 when the format is unrecognized."""
    # Try common formats
    for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(period_label, fmt).date()
        except ValueError:
            continue
    return date(fiscal_year, 12, 31)


def _persist(rows: list[dict[str, object]], *, db_path: Path) -> tuple[int, int]:
    """Insert rows into lease_commitments + log to extractions. Returns
    (n_inserted, n_extractions_logged)."""
    if not rows or not db_path.exists():
        return 0, 0
    inserted = 0
    logged = 0
    conn = sqlite3.connect(str(db_path))
    try:
        for row in rows:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO lease_commitments(
                        ticker, fiscal_year, as_of_date, filing_doc_id,
                        lease_type, ladder_year, ladder_calendar_year,
                        amount, currency, unit, source_section_key, extracted_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["ticker"],
                        row["fiscal_year"],
                        row["as_of_date"],
                        row.get("filing_doc_id"),
                        row["lease_type"],
                        row["ladder_year"],
                        row.get("ladder_calendar_year"),
                        row["amount"],
                        row["currency"],
                        row["unit"],
                        row.get("source_section_key"),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted += 1
                lc_id = int(cur.lastrowid or 0)
                # Log to extractions for audit / supersession chain.
                if _extractions_table_present(conn) and row.get("filing_doc_id") is not None:
                    conn.execute(
                        """
                        INSERT INTO extractions(
                            source_doc_id, extraction_kind, extractor_id,
                            extractor_version, payload_json, confidence,
                            extracted_at, links_to_json
                        ) VALUES (?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(cast("int", row["filing_doc_id"])),
                            "table_row",
                            EXTRACTOR_ID,
                            EXTRACTOR_VERSION,
                            json.dumps(row, default=str),
                            1.0,
                            datetime.now(UTC).isoformat(),
                            json.dumps({"table": "lease_commitments", "id": lc_id}),
                        ),
                    )
                    logged += 1
            except sqlite3.IntegrityError:
                # Unique-constraint violation — already extracted. Skip.
                continue
        conn.commit()
    finally:
        conn.close()
    return inserted, logged


def _extractions_table_present(conn: sqlite3.Connection) -> bool:
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='extractions'"
    ).fetchone()
    return r is not None
