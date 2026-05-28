"""Index FMP JSON files on disk into the `documents` table for one ticker.

Bridges the gap between `execution/save_fmp_data.py` (writes JSON files to
`data/historical/fmp/`) and `pipeline.quarterly_refresh.refresh_ticker` (which
queries `documents` rows). Until this module existed, the only way to populate
`documents` rows from FMP files was via one-shot Alembic migrations
(0003 + 0010 + 0015), which are not safe to re-run after schema progresses past
them.

This is the per-ticker, idempotent equivalent. Called from `execution/onboard_ticker.py`
after the FMP fetch completes, so newly-onboarded tickers see their files
extracted by the same DAG that runs in the cron.

Classification rules mirror the migration sequence:
- 0003: stem prefix → doc_type via `_FMP_DOC_TYPE_MAP`; unknown → `fmp_other`.
- 0010: `*_form_10k_{YEAR}.json` → `fmp_10k_json`,
        `*_historical_(ratings|grades).json` → `fmp_grades`.
- 0015: `*_form_10q_{YEAR}_{Q}.json` → `fmp_10q_json`.

Idempotent: INSERT OR IGNORE on sha256 means re-running with the same files is
a no-op; partial re-fetches add only the changed/new files.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import cast

_PERIOD_SUFFIXES = ("annual", "quarterly", "ttm")
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_FORM_10K_RX = re.compile(r"^[A-Z]+_form_10k_\d{4}\.json$")
_FORM_10Q_RX = re.compile(r"^[A-Z]+_form_10q_\d{4}_Q[1-4]\.json$")
_GRADES_SUFFIXES = ("_historical_ratings.json", "_historical_grades.json")

# Stem-prefix → doc_type. Lifted verbatim from
# alembic/versions/0003_backfill_documents_from_fmp_files.py
_FMP_DOC_TYPE_MAP: dict[str, str] = {
    "income_statement": "fmp_income_statement",
    "balance_sheet": "fmp_balance_sheet",
    "cash_flow": "fmp_cashflow",
    "as_reported_income": "fmp_as_reported_income",
    "as_reported_balance": "fmp_as_reported_balance",
    "as_reported_cashflow": "fmp_as_reported_cashflow",
    "as_reported_financial": "fmp_as_reported_financial",
    "income_growth": "fmp_financial_growth",
    "balance_growth": "fmp_financial_growth",
    "cashflow_growth": "fmp_financial_growth",
    "financial_growth": "fmp_financial_growth",
    "product_segments": "fmp_segment_product",
    "geo_segments": "fmp_segment_geographic",
    "key_metrics": "fmp_key_metrics",
    "financial_ratios": "fmp_financial_ratios",
    "enterprise_values": "fmp_enterprise_values",
    "owner_earnings": "fmp_owner_earnings",
    "dcf_basic": "fmp_dcf",
    "dcf_levered": "fmp_dcf_levered",
    "profile": "fmp_profile",
    "earnings_calendar": "fmp_earnings_calendar",
    "peers": "fmp_peers",
    "company_executives": "fmp_executives",
    "historical_employee_count": "fmp_historical_employees",
    "historical_market_cap": "fmp_historical_market_cap",
    "historical_rating": "fmp_grades",
    "price_target_consensus": "fmp_price_target_consensus",
    "price_chart_10y_div_adj": "fmp_historical_price",
    "financial_reports_dates": "fmp_financial_reports_dates",
    "analyst_estimates": "fmp_analyst_estimates",
    "financial_scores": "fmp_other",
    "shares_float": "fmp_other",
    "ratings_snapshot": "fmp_grades",
    "grades_summary": "fmp_grades",
    "grades_consensus": "fmp_grades",
    "price_target_summary": "fmp_price_target_consensus",
}


def _classify_filename(fname: str) -> str:
    """Return the doc_type for a `{TICKER}_*.json` filename. Mirrors 0003+0010+0015."""
    if _FORM_10K_RX.match(fname):
        return "fmp_10k_json"
    if _FORM_10Q_RX.match(fname):
        return "fmp_10q_json"
    for suffix in _GRADES_SUFFIXES:
        if fname.endswith(suffix):
            return "fmp_grades"
    stem = fname[:-len(".json")] if fname.endswith(".json") else fname
    parts = stem.split("_", 1)
    if len(parts) < 2:
        return "fmp_other"
    rest = parts[1]
    for p in _PERIOD_SUFFIXES:
        if rest.endswith(f"_{p}"):
            rest = rest[: -len(p) - 1]
            break
    return _FMP_DOC_TYPE_MAP.get(rest, "fmp_other")


def _max_date_in_records(path: Path) -> datetime | None:
    """Scan FMP JSON for max `date` or `fillingDate`; return as datetime or None."""
    try:
        with open(path, encoding="utf-8") as f:
            raw: object = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, list):
        return None
    records = cast(list[object], raw)
    max_iso: str | None = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec_obj = cast(dict[str, object], rec)
        for key in ("date", "fillingDate"):
            v = rec_obj.get(key)
            if isinstance(v, str) and _DATE_RX.match(v):
                d = v[:10]
                if max_iso is None or d > max_iso:
                    max_iso = d
                break
    if max_iso is None:
        return None
    return datetime.fromisoformat(max_iso)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def set_fiscal_year_end_from_fmp(
    conn: sqlite3.Connection,
    ticker: str,
    project_root: Path,
) -> str | None:
    """Read the FMP annual income statement for `ticker` and write its FYE
    (MM-DD) to `tracked_companies.fiscal_year_end`. Returns the value written,
    or None if the cache file is missing/empty (column left untouched).
    """
    upper = ticker.upper()
    path = project_root / "data" / "historical" / "fmp" / f"{upper}_income_statement_annual.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        raw: object = json.load(f)
    if not isinstance(raw, list):
        return None
    records = cast(list[object], raw)
    latest_date: str | None = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec_obj = cast(dict[str, object], rec)
        if rec_obj.get("period") != "FY":
            continue
        d = rec_obj.get("date")
        if not isinstance(d, str) or len(d) < 10:
            continue
        if latest_date is None or d > latest_date:
            latest_date = d
    if latest_date is None:
        return None
    fye = latest_date[5:10]  # "YYYY-MM-DD" → "MM-DD"
    conn.execute(
        "UPDATE tracked_companies SET fiscal_year_end = ? WHERE ticker = ?",
        (fye, upper),
    )
    conn.commit()
    return fye


def _truthy(value: object) -> bool:
    """Interpret an FMP boolean flag. /stable returns real JSON booleans, but
    the v3 fallback sometimes returns the strings "true"/"false" — accept both."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _first_profile_record(raw: object) -> dict[str, object] | None:
    """FMP profile responses are a single-element list; tolerate a bare dict."""
    if isinstance(raw, list):
        for item in cast(list[object], raw):
            if isinstance(item, dict):
                return cast(dict[str, object], item)
        return None
    if isinstance(raw, dict):
        return cast(dict[str, object], raw)
    return None


def classify_instrument_type_from_profile(profile: dict[str, object]) -> str:
    """Map an FMP profile record to a `models.companies.InstrumentType` value.

    - ``etf``    — pooled vehicle (``isEtf`` or ``isFund``)
    - ``adr``    — depositary receipt (``isAdr``)
    - ``equity`` — everything else (the common case, incl. fresh US IPOs)

    Deliberately does NOT blanket-default to ``equity``: ADRs and ETFs exist in
    the universe (e.g. BHP/DLO/NTDOY are ``adr``; FLKR is ``etf``) and must
    classify off the profile flags rather than being forced to equity.
    """
    if _truthy(profile.get("isEtf")) or _truthy(profile.get("isFund")):
        return "etf"
    if _truthy(profile.get("isAdr")):
        return "adr"
    return "equity"


def set_instrument_type_from_fmp(
    conn: sqlite3.Connection,
    ticker: str,
    project_root: Path,
) -> str | None:
    """Classify `ticker`'s instrument_type from its cached FMP profile and write
    it to ``tracked_companies`` — but ONLY when the column is currently NULL.

    Fills the gap left by `execution/onboard_ticker.py` and raw-SQL inserts:
    unlike `db.track_company`'s curated path, they never set instrument_type,
    leaving it NULL and tripping `onboard_pending_tickers`' ``no_instrument_type``
    reason on every run. Writing only-when-NULL guarantees a hand-curated value
    (e.g. the migration-0001 backfill's NU=``adr``) is never clobbered by an FMP
    guess.

    Returns the instrument_type the column holds after the call (the value just
    written, or a pre-existing one), or None when there is no profile cache, it
    is unparseable, or the ticker has no ``tracked_companies`` row.
    """
    upper = ticker.upper()
    path = project_root / "data" / "historical" / "fmp" / f"{upper}_profile.json"
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    profile = _first_profile_record(raw)
    if profile is None:
        return None
    instrument = classify_instrument_type_from_profile(profile)
    conn.execute(
        "UPDATE tracked_companies SET instrument_type = ? "
        "WHERE ticker = ? AND instrument_type IS NULL",
        (instrument, upper),
    )
    conn.commit()
    row = conn.execute(
        "SELECT instrument_type FROM tracked_companies WHERE ticker = ? LIMIT 1",
        (upper,),
    ).fetchone()
    if row is None:
        return None
    value = row["instrument_type"] if isinstance(row, sqlite3.Row) else row[0]
    return value if isinstance(value, str) else None


def index_fmp_files_for_ticker(
    conn: sqlite3.Connection,
    ticker: str,
    project_root: Path,
) -> int:
    """Index every `data/historical/fmp/{TICKER}_*.json` into `documents`.

    Returns the count of newly-inserted rows. INSERT OR IGNORE on sha256 means
    files whose content is already in the table are skipped. Files whose
    classification can't be derived land in `fmp_other` (matching 0003 behavior).
    """
    fmp_dir = project_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return 0

    upper = ticker.upper()
    prefix = f"{upper}_"
    inserted = 0

    for path in sorted(fmp_dir.iterdir()):
        if not path.is_file() or not path.name.startswith(prefix) or path.suffix != ".json":
            continue

        doc_type = _classify_filename(path.name)
        period_end = _max_date_in_records(path)
        sha = _sha256_of(path)
        stat = path.stat()
        rel_path = str(path.relative_to(project_root)).replace("\\", "/")

        cur = conn.execute(
            "INSERT OR IGNORE INTO documents "
            "(ticker, source_type, doc_type, period_end, file_path, sha256, "
            " fetched_at, fetch_status, raw_bytes_size, source_url) "
            "VALUES (?, 'fmp', ?, ?, ?, ?, ?, 'ok', ?, NULL)",
            (
                upper,
                doc_type,
                period_end,
                rel_path,
                sha,
                datetime.fromtimestamp(stat.st_mtime),
                stat.st_size,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1

    conn.commit()
    return inserted
