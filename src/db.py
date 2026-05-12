"""SQLite portfolio DB — companies, quarterly artifacts, FMP endpoint status.

Schema management is currently inline (CREATE TABLE IF NOT EXISTS + ALTER TABLE
in init_db()). Phase 2 of the backend redesign migrates schema management to
Alembic and removes the auto-call at module import-time.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from urllib.parse import unquote

import requests

from models.artifacts import (
    ArtifactFlags,
    ArtifactKind,
    Quarter,
    parse_tmp_artifact,
    parse_transcript_processed,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "portfolio.db")
FMP_DIR = os.path.join(DATA_DIR, "historical", "fmp")

# Tickers that share FMP files under an alternate name (canonical → file prefix)
_FMP_ALIASES: dict[str, list[str]] = {
    "GOOG": ["GOOG", "GOOGL"],
    "GOOGL": ["GOOGL", "GOOG"],
}

_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LIST_TYPES: frozenset[str] = frozenset(
    {"portfolio", "watchlist", "evaluation", "none", "etf", "index_member"}
)

# Public SQL fragments for ad-hoc callers that hand-roll SQL against
# tracked_companies. Defined once here so adding a new list_type later is a
# single-file edit. Prefer these over inline string literals.
#
# `ACTIVE_LIST_TYPES` — names the user actively analyzes (gets data refreshes,
# KPI extraction, earnings calendar, etc.). Mirrors `ANALYZED_LIST_TYPES` in
# `pipeline.queries` but as a SQL-fragment string for raw cursor callers.
#
# `BRIEFED_LIST_TYPES` — strict subset that auto-produces full briefs (portfolio
# or eval flavor). Watchlist is a holding pen and not briefed by default.
ACTIVE_LIST_TYPES: tuple[str, ...] = ("portfolio", "watchlist", "evaluation")
ACTIVE_LIST_TYPES_SQL: str = (
    "(" + ", ".join(f"'{t}'" for t in ACTIVE_LIST_TYPES) + ")"
)
BRIEFED_LIST_TYPES: tuple[str, ...] = ("portfolio", "evaluation")
BRIEFED_LIST_TYPES_SQL: str = (
    "(" + ", ".join(f"'{t}'" for t in BRIEFED_LIST_TYPES) + ")"
)


def get_connection() -> sqlite3.Connection:
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    # 30s busy_timeout: this DB is shared with sibling-branch pipelines whose
    # write transactions can briefly hold the lock. Without it, any concurrent
    # writer immediately raises OperationalError("database is locked") and
    # kills long-running pulls. 30s is well above any normal transaction.
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    """Create tables and apply inline migrations.

    Runs at module import-time (last line of this file). Phase 2 replaces this
    with `alembic upgrade head` invoked explicitly from a CLI entrypoint.
    """
    conn = get_connection()
    cursor = conn.cursor()
    _create_tracked_companies(cursor)
    _create_quarterly_artifacts(cursor)
    _create_fmp_endpoint_status(cursor)
    conn.commit()
    conn.close()


def _create_tracked_companies(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 1,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL CHECK(list_type IN (
                'portfolio', 'watchlist', 'evaluation', 'none', 'etf', 'index_member'
            )),
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, ticker)
        )
        """
    )
    _add_columns(
        cursor,
        "tracked_companies",
        [
            ("sec_validated", "BOOLEAN DEFAULT 0"),
            ("ir_url", "TEXT DEFAULT NULL"),
            ("model_url", "TEXT DEFAULT NULL"),
            ("publishes_release", "BOOLEAN DEFAULT 0"),
            ("publishes_slides", "BOOLEAN DEFAULT 0"),
            ("publishes_transcript", "BOOLEAN DEFAULT 0"),
            ("fmp_data_upto", "TEXT DEFAULT NULL"),
            ("manual_data_quarters", "TEXT DEFAULT '[]'"),
            ("fmp_data_saved", "BOOLEAN DEFAULT 0"),
            ("archived_at", "TIMESTAMP DEFAULT NULL"),
        ],
    )


def _create_quarterly_artifacts(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS quarterly_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            year INTEGER NOT NULL,
            quarter TEXT NOT NULL,
            has_release_file    BOOLEAN DEFAULT 0,
            has_slides_file     BOOLEAN DEFAULT 0,
            has_transcript_file BOOLEAN DEFAULT 0,
            has_audio_file      BOOLEAN DEFAULT 0,
            step_audio_transcribed BOOLEAN DEFAULT 0,
            step_llm_summarized    BOOLEAN DEFAULT 0,
            step_saydo_analyzed    BOOLEAN DEFAULT 0,
            step_thesis_updated    BOOLEAN DEFAULT 0,
            UNIQUE(ticker, year, quarter)
        )
        """
    )
    _add_columns(
        cursor,
        "quarterly_artifacts",
        [
            ("step_llm_summarized", "BOOLEAN DEFAULT 0"),
        ],
    )


def _create_fmp_endpoint_status(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS fmp_endpoint_status (
            ticker         TEXT    NOT NULL,
            endpoint       TEXT    NOT NULL,
            period         TEXT    NOT NULL DEFAULT '',
            status         TEXT    NOT NULL,
            http_code      INTEGER,
            record_count   INTEGER,
            earliest_date  TEXT,
            latest_date    TEXT,
            file_path      TEXT,
            file_bytes     INTEGER,
            error_msg      TEXT,
            last_pulled    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, endpoint, period)
        )
        """
    )


def _add_columns(cursor: sqlite3.Cursor, table: str, columns: list[tuple[str, str]]) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cursor.fetchall()}
    for col_name, col_def in columns:
        if col_name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


# ---------------------------------------------------------------------------
# FMP data helpers
# ---------------------------------------------------------------------------


def _fmp_prefixes(ticker: str) -> list[str]:
    """Return FMP file prefixes for a given ticker (handles GOOG/GOOGL alias)."""
    return _FMP_ALIASES.get(ticker.upper(), [ticker.upper()])


def _fmp_latest_date(ticker: str) -> str | None:
    """Return max YYYY-MM-DD date across all FMP files for ticker, or None."""
    if not os.path.exists(FMP_DIR):
        return None
    prefixes = _fmp_prefixes(ticker)
    max_date: str | None = None

    for fname in os.listdir(FMP_DIR):
        stem = os.path.splitext(fname)[0]
        parts = stem.split("_", 1)
        if not parts or parts[0].upper() not in prefixes:
            continue
        candidate = _scan_fmp_file_for_max_date(os.path.join(FMP_DIR, fname))
        if candidate is not None and (max_date is None or candidate > max_date):
            max_date = candidate
    return max_date


def _scan_fmp_file_for_max_date(path: str) -> str | None:
    """Read one FMP JSON file; return max YYYY-MM-DD on `date` or `fillingDate`."""
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    if not isinstance(records, list):
        return None
    max_date: str | None = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key in ("date", "fillingDate"):
            v = rec.get(key)
            if isinstance(v, str) and _DATE_RX.match(v[:10]):
                d = v[:10]
                if max_date is None or d > max_date:
                    max_date = d
                break
    return max_date


def _fmp_files_exist(ticker: str) -> bool:
    if not os.path.exists(FMP_DIR):
        return False
    prefixes = _fmp_prefixes(ticker)
    for fname in os.listdir(FMP_DIR):
        if os.path.splitext(fname)[0].split("_", 1)[0].upper() in prefixes:
            return True
    return False


# ---------------------------------------------------------------------------
# SEC ticker registry + IR URL discovery
# ---------------------------------------------------------------------------

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_USER_AGENT = "RetailInvestorApp/1.0 (contact@example.com)"
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_DDG_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_DDG_RESULT_RX = re.compile(r'class="result__url" href="//duckduckgo\.com/l/\?uddg=([^"&]+)')

_sec_tickers_cache: dict[str, str] | None = None


def _load_sec_tickers() -> dict[str, str]:
    """Fetch and cache the SEC ticker→issuer-name registry.

    Raises requests.RequestException on transient failure. Caller decides whether
    to log+downgrade or halt.
    """
    global _sec_tickers_cache
    if _sec_tickers_cache is not None:
        return _sec_tickers_cache
    headers = {"User-Agent": _SEC_USER_AGENT}
    resp = requests.get(_SEC_TICKERS_URL, headers=headers, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected SEC tickers payload type: {type(payload).__name__}")
    _sec_tickers_cache = {v["ticker"]: v["title"] for v in payload.values()}
    return _sec_tickers_cache


def _validate_sec(ticker: str, name: str) -> tuple[bool, str]:
    """Look up ticker in SEC registry; (validated, official-or-original-name)."""
    tickers = _load_sec_tickers()
    official = tickers.get(ticker.upper())
    if official:
        return True, official
    return False, name


def _find_ir_url(ticker: str, name: str) -> str | None:
    """Best-effort IR URL discovery via DuckDuckGo HTML search.

    Raises requests.RequestException on transient failure. Caller decides whether
    to log+downgrade or halt.
    """
    query = f"{name} {ticker} investor relations"
    headers = {"User-Agent": _DDG_USER_AGENT}
    resp = requests.get(_DDG_HTML_URL, params={"q": query}, headers=headers, timeout=5)
    resp.raise_for_status()
    match = _DDG_RESULT_RX.search(resp.text)
    return unquote(match.group(1)) if match else None


def _safe_validate_sec(ticker: str, name: str) -> tuple[bool, str]:
    """Wrap _validate_sec; log+downgrade on transient or schema error."""
    try:
        return _validate_sec(ticker, name)
    except requests.RequestException as e:
        sys.stderr.write(
            json.dumps({"event": "sec_validate_downgrade", "ticker": ticker, "error": str(e)})
            + "\n"
        )
        return False, name
    except (ValueError, KeyError) as e:
        sys.stderr.write(
            json.dumps({"event": "sec_validate_schema_error", "ticker": ticker, "error": str(e)})
            + "\n"
        )
        return False, name


def _safe_find_ir_url(ticker: str, name: str) -> str | None:
    """Wrap _find_ir_url; log+downgrade on transient error."""
    try:
        return _find_ir_url(ticker, name)
    except requests.RequestException as e:
        sys.stderr.write(
            json.dumps({"event": "ir_url_lookup_downgrade", "ticker": ticker, "error": str(e)})
            + "\n"
        )
        return None


# ---------------------------------------------------------------------------
# Artifact scanning
# ---------------------------------------------------------------------------


def _slot(
    artifacts: dict[tuple[int, Quarter], ArtifactFlags], year: int, quarter: Quarter
) -> ArtifactFlags:
    """Get-or-create the ArtifactFlags entry for this (year, quarter)."""
    key = (year, quarter)
    if key not in artifacts:
        artifacts[key] = ArtifactFlags()
    return artifacts[key]


def _scan_processed_dir(ticker: str, artifacts: dict[tuple[int, Quarter], ArtifactFlags]) -> None:
    """Walk transcripts/processed/, set has_transcript_file flag for matches."""
    processed_dir = os.path.join(PROJECT_ROOT, "transcripts", "processed")
    if not os.path.exists(processed_dir):
        return
    upper = ticker.upper()
    for fname in os.listdir(processed_dir):
        parsed = parse_transcript_processed(fname)
        if parsed is None or parsed.ticker != upper:
            continue
        _slot(artifacts, parsed.year, parsed.quarter).has_transcript_file = True


def _scan_tmp_dir(
    ticker: str,
    artifacts: dict[tuple[int, Quarter], ArtifactFlags],
) -> None:
    """Walk .tmp/, dispatch by ArtifactKind, set the corresponding step flag."""
    tmp_dir = os.path.join(PROJECT_ROOT, ".tmp")
    if not os.path.exists(tmp_dir):
        return
    upper = ticker.upper()
    for fname in os.listdir(tmp_dir):
        parsed = parse_tmp_artifact(fname)
        if parsed is None or parsed.ticker != upper:
            continue
        flags = _slot(artifacts, parsed.year, parsed.quarter)
        if parsed.kind is ArtifactKind.AUDIO:
            flags.has_audio_file = True
        elif parsed.kind is ArtifactKind.SAYDO:
            flags.step_saydo_analyzed = True
        elif parsed.kind is ArtifactKind.LLM_SUMMARY:
            flags.step_llm_summarized = True


def _persist_artifact_rows(
    cursor: sqlite3.Cursor,
    ticker: str,
    artifacts: dict[tuple[int, Quarter], ArtifactFlags],
) -> None:
    """Upsert quarterly_artifacts rows for ticker; OR booleans onto existing values."""
    for (year, quarter), flags in artifacts.items():
        cursor.execute(
            """
            INSERT INTO quarterly_artifacts (
                ticker, year, quarter,
                has_release_file, has_slides_file, has_transcript_file, has_audio_file,
                step_audio_transcribed, step_llm_summarized, step_saydo_analyzed,
                step_thesis_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, year, quarter) DO UPDATE SET
                has_transcript_file    = excluded.has_transcript_file    OR quarterly_artifacts.has_transcript_file,
                has_audio_file         = excluded.has_audio_file         OR quarterly_artifacts.has_audio_file,
                step_llm_summarized    = excluded.step_llm_summarized    OR quarterly_artifacts.step_llm_summarized,
                step_saydo_analyzed    = excluded.step_saydo_analyzed    OR quarterly_artifacts.step_saydo_analyzed
            """,
            (
                ticker,
                year,
                quarter.value,
                flags.has_release_file,
                flags.has_slides_file,
                flags.has_transcript_file,
                flags.has_audio_file,
                flags.step_audio_transcribed,
                flags.step_llm_summarized,
                flags.step_saydo_analyzed,
                flags.step_thesis_updated,
            ),
        )


def _update_company_fmp_state(cursor: sqlite3.Cursor, ticker: str) -> None:
    """Update tracked_companies.fmp_data_saved and fmp_data_upto for ticker."""
    latest = _fmp_latest_date(ticker)
    saved = _fmp_files_exist(ticker)
    cursor.execute(
        """
        UPDATE tracked_companies
        SET fmp_data_saved = ?,
            fmp_data_upto  = CASE WHEN ? IS NOT NULL THEN ? ELSE fmp_data_upto END
        WHERE ticker = ?
        """,
        (1 if saved else 0, latest, latest, ticker),
    )


def scan_and_sync_artifacts(ticker: str) -> None:
    """Scan project folders and sync quarterly_artifacts + tracked_companies for ticker.

    Filename parsing is regex+Pydantic (models.artifacts). Files that don't match
    canonical naming are skipped — never silently misclassified.
    """
    ticker = ticker.upper()
    conn = get_connection()
    cursor = conn.cursor()

    artifacts: dict[tuple[int, Quarter], ArtifactFlags] = {}

    _scan_processed_dir(ticker, artifacts)
    _scan_tmp_dir(ticker, artifacts)

    _persist_artifact_rows(cursor, ticker, artifacts)
    _update_company_fmp_state(cursor, ticker)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_TRACKED_LIST_TYPES_FOR_ONBOARD: frozenset[str] = frozenset(ACTIVE_LIST_TYPES)


_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _spawn_onboard_async(ticker: str) -> None:
    """Fire-and-forget `execution/onboard_ticker.py --ticker X` for a newly-added ticker.

    Detached so the child outlives the parent (Flask request handler / CLI).
    Logs go to `logs/onboard_{TICKER}_{TIMESTAMP}.log`. Spawn failures are
    swallowed to JSON stderr so a watchlist add never fails on a missing pipeline.
    """
    script = os.path.join(PROJECT_ROOT, "execution", "onboard_ticker.py")
    if not os.path.exists(script):
        sys.stderr.write(
            json.dumps({"event": "onboard_spawn_skipped", "ticker": ticker, "reason": "script missing"}) + "\n"
        )
        return

    log_dir = os.path.join(PROJECT_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(log_dir, f"onboard_{ticker}_{stamp}.log")
    cmd = [sys.executable, script, "--ticker", ticker]

    try:
        log_handle = open(log_path, "w", encoding="utf-8")
        if os.name == "nt":
            subprocess.Popen(
                cmd,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(
                cmd,
                cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        sys.stderr.write(
            json.dumps({"event": "onboard_spawned", "ticker": ticker, "log": log_path}) + "\n"
        )
    except OSError as e:
        sys.stderr.write(
            json.dumps({"event": "onboard_spawn_failed", "ticker": ticker, "error": str(e)}) + "\n"
        )


def track_company(ticker: str, name: str, list_type: str, user_id: int = 1) -> None:
    """Upsert a tracked company; SEC-validate, find IR URL, sync artifacts.

    Spawns the detached `execution/onboard_ticker.py` subprocess whenever
    the ticker *transitions into* the analytical universe — that is, a new
    add to portfolio/watchlist OR a promotion from a non-onboardable
    list_type (index_member, etf, none) into one. Re-adds within the same
    onboardable set (watchlist↔portfolio, or repeated watchlist→watchlist)
    do NOT re-spawn — the data is already there and `onboard_pending_tickers`
    handles any actual gaps.
    """
    if list_type not in _LIST_TYPES:
        raise ValueError(f"Invalid list_type {list_type!r}; expected one of {sorted(_LIST_TYPES)}")

    ticker = ticker.upper()
    is_valid, official_name = _safe_validate_sec(ticker, name)
    final_name = official_name if is_valid else name
    ir_url = _safe_find_ir_url(ticker, final_name)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT list_type FROM tracked_companies WHERE user_id = ? AND ticker = ?",
        (user_id, ticker),
    )
    prior_row = cursor.fetchone()
    prior_list_type: str | None = prior_row[0] if prior_row is not None else None
    cursor.execute(
        """
        INSERT INTO tracked_companies (user_id, ticker, name, list_type, added_at, sec_validated, ir_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, ticker) DO UPDATE SET
            list_type     = excluded.list_type,
            name          = excluded.name,
            sec_validated = excluded.sec_validated,
            ir_url        = excluded.ir_url,
            added_at      = excluded.added_at,
            archived_at   = NULL
        """,
        (user_id, ticker, final_name, list_type, datetime.datetime.now(), is_valid, ir_url),
    )
    conn.commit()
    conn.close()

    scan_and_sync_artifacts(ticker)

    became_onboardable = list_type in _TRACKED_LIST_TYPES_FOR_ONBOARD and (
        prior_list_type is None or prior_list_type not in _TRACKED_LIST_TYPES_FOR_ONBOARD
    )
    if became_onboardable:
        _spawn_onboard_async(ticker)


def remove_company(ticker: str, user_id: int = 1) -> None:
    """Hard-delete a tracked company row (and any captured FMP/transcripts stay
    on disk; only the tracking row goes). Prefer `archive_company` for ordinary
    'I'm not watching this anymore' flows — archive preserves the row + all
    captured history, lets `track_company` reactivate later, and keeps the
    cacher from re-pulling stale rows by mistake.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM tracked_companies WHERE user_id = ? AND ticker = ?",
        (user_id, ticker.upper()),
    )
    conn.commit()
    conn.close()


def archive_company(ticker: str, user_id: int = 1) -> bool:
    """Soft-delete: set archived_at = now. Returns True if a row was archived.

    Idempotent on already-archived rows (re-archiving updates the timestamp).
    The cacher's audit step filters archived rows out of the refresh queue;
    captured FMP JSON, snapshots, and endpoint-status rows are retained on
    disk and in DB, so reactivation is free.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE tracked_companies SET archived_at = ? "
        "WHERE user_id = ? AND ticker = ?",
        (datetime.datetime.now(), user_id, ticker.upper()),
    )
    archived = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return archived


def reactivate_company(ticker: str, user_id: int = 1) -> bool:
    """Clear archived_at so the cacher resumes refreshing this ticker.

    Returns True if a row was reactivated. No-op on rows that are already
    active (archived_at IS NULL).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE tracked_companies SET archived_at = NULL "
        "WHERE user_id = ? AND ticker = ? AND archived_at IS NOT NULL",
        (user_id, ticker.upper()),
    )
    reactivated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return reactivated


def get_tracked_companies(
    user_id: int = 1, *, include_archived: bool = False
) -> list[dict[str, object]]:
    """Return tracked companies for user. Excludes archived rows by default."""
    conn = get_connection()
    cursor = conn.cursor()
    sql = "SELECT * FROM tracked_companies WHERE user_id = ?"
    if not include_archived:
        sql += " AND archived_at IS NULL"
    sql += " ORDER BY list_type, ticker"
    cursor.execute(sql, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def refresh_all_fmp_dates(user_id: int = 1) -> None:
    """Re-scan FMP files for every tracked company; update fmp_data_saved/upto."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticker FROM tracked_companies WHERE user_id = ?", (user_id,))
    tickers = [r["ticker"] for r in cursor.fetchall()]
    conn.close()

    for ticker in tickers:
        conn = get_connection()
        cursor = conn.cursor()
        _update_company_fmp_state(cursor, ticker)
        conn.commit()
        conn.close()


# Inline schema management — replaced with `alembic upgrade head` in Phase 2.
init_db()
